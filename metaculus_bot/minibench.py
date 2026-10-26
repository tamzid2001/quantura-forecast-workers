"""Read-only round coverage; never generates forecasts or publishes comments."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import json
import os
from pathlib import Path

from .api import Metaculus, date, utcnow
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


def audit(api, now=None):
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
        for q in questions.values():
            if q.id_of_question in own:
                counts["submitted"] += 1
            elif q.close_time and q.close_time <= now:
                counts["closed_without_forecast"] += 1
            elif q.open_time and q.open_time > now:
                counts["upcoming"] += 1
            elif str(q.state.value) == "open":
                counts["open_unsubmitted"] += 1
            else:
                counts["other_unsubmitted"] += 1
        report["rounds"].append({"id": project["id"], "name": project.get("name"),
            "total_questions": len(questions), "parse_failures": failures, "counts": dict(counts),
            "coverage_pct": 100 * len(own & questions.keys()) / len(questions) if questions else None})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("artifacts/metaculus-minibench.json"))
    args = parser.parse_args()
    result = audit(Metaculus(os.environ["METACULUS_TOKEN"]))
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


if __name__ == "__main__":
    main()
