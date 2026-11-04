"""Read-only round coverage; never generates forecasts or publishes comments."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import json
import os
from datetime import timedelta
from pathlib import Path
import re

from .api import Metaculus, date, utcnow
from .checkpoint import Checkpoint
from .sdk.client import MetaculusClient
from .sdk.questions import ConditionalQuestion

FIELDS = ("id", "name", "slug", "type", "is_ongoing", "start_date", "close_date",
          "forecasting_end_date", "bot_leaderboard_status", "user_permission", "prize_pool")


def all_questions(post):
    values = MetaculusClient()._post_json_to_questions_while_handling_groups(copy.deepcopy(post), "unpack_subquestions")
    result = []
    for q in values:
        result.extend([q.question_yes, q.question_no] if isinstance(q, ConditionalQuestion) else [q])
    return result


def audit(api, now=None, checkpoint=None):
    now = now or utcnow()
    identity = api.identity()
    if str(identity["id"]) != os.environ.get("METACULUS_BOT_ID"):
        raise RuntimeError("METACULUS_BOT_ID_MISMATCH")
    catalog = api.get("projects/minibenches/")
    if not isinstance(catalog, list):
        raise ValueError("INVALID_MINIBENCH_CATALOG")
    ordered = sorted(catalog, key=lambda p: p.get("start_date") or "", reverse=True)
    started = [p for p in ordered if not date(p.get("start_date")) or date(p["start_date"]) <= now]
    upcoming = [p for p in ordered if date(p.get("start_date")) and date(p["start_date"]) > now]
    recent = started[:2] + upcoming[-2:]
    report = {"at": now.isoformat(), "bot_id": identity["id"], "live_submissions": 0,
              "catalog": [{key: p.get(key) for key in FIELDS} for p in recent], "rounds": []}
    # Comments stay private. Export only question IDs/counts, never note text,
    # probabilities, source excerpts or the account's private reasoning.
    comments = api.comments(None, identity['id'])
    if upcoming:
        next_round = min(upcoming, key=lambda p: date(p["start_date"]))
        report["next_round"] = {"id": next_round["id"], "start_date": next_round["start_date"],
                                "source": "published_catalog"}
    elif started and date(started[0].get("start_date")):
        # Operational estimate only; actual activation always uses fresh API
        # dates, permissions and open-question state, never this estimate.
        start = date(started[0]["start_date"])
        cycles = max(1, int((now-start).total_seconds() // (14*86400)) + 1)
        report["next_round"] = {"id": None, "start_date": (start+timedelta(days=14*cycles)).isoformat(),
                                "source": "14_day_cadence_estimate"}
    print(json.dumps({"event": "metaculus_minibench_catalog", "catalog": report["catalog"]}))
    for project in started[:2]:
        # No status filter: include closed/resolved questions to expose missed
        # windows rather than misleadingly reporting only today's open subset.
        posts, offset = [], 0
        while True:
            data = api.get("posts/", tournaments=project["id"], limit=100, offset=offset,
                           include_description="true", order_by="-published_time")
            page = data["results"]; posts.extend(page)
            if len(page) < 100 or not data.get("next"):
                break
            offset += 100
            if offset >= 1000:
                raise ValueError("MINIBENCH_ROUND_PAGE_LIMIT")
        questions, failures = {}, 0
        for post in posts:
            try:
                for q in all_questions(post):
                    questions[q.id_of_question] = q
            except (ValueError, KeyError, TypeError):
                failures += 1
        own = api.own_forecasts(list(questions))
        counts = Counter()
        missed = []
        urgent = []
        for q in questions.values():
            if q.id_of_question in own:
                counts["submitted"] += 1
            elif q.close_time and q.close_time <= now:
                counts["closed_without_forecast"] += 1
                missed.append(q.id_of_question)
            elif q.open_time and q.open_time > now:
                counts["upcoming"] += 1
            elif str(q.state.value) == "open":
                counts["open_unsubmitted"] += 1
                if q.close_time and q.close_time <= now + timedelta(minutes=45):
                    urgent.append(q.id_of_question)
            else:
                counts["other_unsubmitted"] += 1
        own_ids = own & questions.keys()
        notes = private_reasoning_coverage(comments, identity['id'], own_ids)
        report["rounds"].append({"id": project["id"], "name": project.get("name"),
            "total_questions": len(questions), "parse_failures": failures, "counts": dict(counts),
            "submitted_question_ids": sorted(own_ids),
            "closed_without_forecast_question_ids": sorted(missed),
            "missed_question_diagnostics": [pending_diagnostic(i, checkpoint) for i in sorted(missed)],
            "private_reasoning": notes,
            "urgent_unsubmitted_question_ids": sorted(urgent),
            "coverage_pct": 100 * len(own & questions.keys()) / len(questions) if questions else None})
    return report


def pending_diagnostic(question_id, checkpoint):
    record = checkpoint.get(question_id) if checkpoint else None
    answer = (record or {}).get('answer', {})
    code = answer.get('generation_error') or ''
    return {'question_id': question_id, 'saved_generation_available': bool(record),
            'abstained': bool(answer.get('abstain')),
            'generation_error': code if isinstance(code, str) and re.fullmatch(r'[A-Z0-9_]{1,100}', code) else None,
            'private_note_write_state': (record or {}).get('note_write_state')}


def private_reasoning_coverage(comments, author_id, question_ids):
    verified, attested = set(), set()
    for comment in comments:
        if comment.get('is_private') is not True or (comment.get('author') or {}).get('id') != author_id:
            continue
        text = comment.get('text') or ''
        match = re.search(r'<!-- QUANTURA_BOT_NOTE_V2 (\{[^\n]*\}) -->', text)
        if not match or not text.startswith('Automated forecast ('):
            continue
        try:
            note = json.loads(match[1])
        except (ValueError, TypeError):
            continue
        qid = note.get('question_id')
        reasoning = text.split('\n\n', 1)[-1].split('<!--', 1)[0].strip()
        if qid not in question_ids or not reasoning or not re.fullmatch(r'[a-f0-9]{64}', note.get('answer_sha256', '')):
            continue
        verified.add(qid)
        if note.get('free_endpoint_policy') is True and note.get('provider_service') and note.get('model'):
            attested.add(qid)
    return {'submitted_with_private_reasoning': len(verified),
            'submitted_missing_verified_private_reasoning_question_ids': sorted(set(question_ids)-verified),
            'free_endpoint_attested': len(attested),
            'legacy_free_endpoint_metadata_unavailable': len(verified-attested)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("artifacts/metaculus-minibench.json"))
    parser.add_argument('--checkpoint', type=Path, help='Optional encrypted worker checkpoint for read-only failure diagnostics')
    args = parser.parse_args()
    api = Metaculus(os.environ['METACULUS_TOKEN'])
    result = (audit(api, checkpoint=Checkpoint(args.checkpoint, os.environ['METACULUS_TOKEN']))
              if args.checkpoint else audit(api))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write("### MiniBench coverage (authoritative account history)\n\n")
            summary.write("Round | Questions | Submitted | Closed without forecast | Open unanswered\n---|---:|---:|---:|---:\n")
            for row in result["rounds"]:
                counts = row["counts"]
                summary.write(f"{row['id']} | {row['total_questions']} | {counts.get('submitted', 0)} | "
                              f"{counts.get('closed_without_forecast', 0)} | {counts.get('open_unsubmitted', 0)}\n")
    print(json.dumps({"event": "metaculus_minibench_coverage", **result}))
    for row in result["rounds"]:
        if row.get('closed_without_forecast_question_ids'):
            print('::warning::MiniBench round '+str(row['id'])+' has '+str(len(row['closed_without_forecast_question_ids']))+' closed questions without a forecast.')
        missing = row.get('private_reasoning', {}).get('submitted_missing_verified_private_reasoning_question_ids', [])
        if missing:
            print('::warning::MiniBench round '+str(row['id'])+' has '+str(len(missing))+' submitted forecasts without a verified private reasoning comment.')
        if row["urgent_unsubmitted_question_ids"]:
            print("::warning::MiniBench has " + str(len(row["urgent_unsubmitted_question_ids"])) +
                  " unanswered questions closing within 45 minutes; inspect the forecast worker now.")


if __name__ == "__main__":
    main()
