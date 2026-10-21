"""Read-only retrospective smoke check. This is not a leakage-free accuracy benchmark."""
from __future__ import annotations

import argparse
import json
import os
from datetime import timedelta
from pathlib import Path

from .api import Metaculus, date, utcnow
from .llm import free_llm_factory
from .questions import payload
from .sdk.client import MetaculusClient


LIMITATION = ("Retrospective readiness check, generated now on already resolved questions. "
              "Only question context was supplied; no current linked pages or outcomes were sent to the model. "
              "Training-data contamination and post-close question edits cannot be excluded. "
              "These Brier scores do not establish out-of-sample forecasting skill. "
              "Official peer scores require the complete scoring inputs and are not estimated from a community median.")


def community_baseline(raw, close):
    latest = ((raw.get("aggregations") or {}).get("recency_weighted") or {}).get("latest") or {}
    centers = latest.get("centers") or []
    stamp = date(latest.get("start_time"))
    if not centers or stamp is None or stamp >= close:
        return None
    probability = float(centers[0])
    return probability if 0 <= probability <= 1 else None


def run(count=5, output=Path("artifacts/metaculus-readiness.json")):
    api = Metaculus(os.environ["METACULUS_TOKEN"])
    identity = api.identity()
    if str(identity["id"]) != os.environ.get("METACULUS_BOT_ID"):
        raise RuntimeError("METACULUS_BOT_ID_MISMATCH")
    projects = api.get("projects/minibenches/")
    projects = sorted(projects, key=lambda p: p.get("start_date") or "", reverse=True)
    selected, seen = [], set()
    for project in projects[:6]:
        data = api.get("posts/", tournaments=project["id"], statuses="resolved", limit=100,
                       include_description="true", order_by="-published_time")
        for post in data["results"]:
            raw = post.get("question") or {}
            if raw.get("type") != "binary" or raw.get("resolution") not in {"yes", "no"} or raw.get("id") in seen:
                continue
            question = MetaculusClient()._post_json_to_questions_while_handling_groups(post, "unpack_subquestions")[0]
            if question.close_time is None or question.close_time >= utcnow():
                continue
            seen.add(question.id_of_question)
            selected.append((question, raw))
            if len(selected) == count:
                break
        if len(selected) == count:
            break
    client = free_llm_factory(os.environ.get("OPENROUTER_API_KEY", ""))
    rows = []
    for question, raw in selected:
        client.as_of = question.close_time - timedelta(minutes=1)
        answer = client.forecast(question, [])
        row = {"question_id": question.id_of_question, "title": question.question_text, "url": question.page_url,
               "close_time": question.close_time.isoformat(), "as_of": client.as_of.isoformat(),
               "outcome": 1 if raw["resolution"] == "yes" else 0,
               "model": answer.get("model", client.model["id"]), "abstained": bool(answer.get("abstain")),
               "reasoning": answer.get("reasoning"), "cost_usd": answer.get("cost_usd")}
        if not row["abstained"]:
            probability = payload(question, answer)["probability_yes"]
            row.update(probability_yes=probability, brier=(probability-row["outcome"])**2)
        baseline = community_baseline(raw, question.close_time)
        if baseline is not None:
            row.update(community_probability=baseline, community_brier=(baseline-row["outcome"])**2)
        rows.append(row)
    scored = [row for row in rows if "brier" in row]
    result = {"bot_id": identity["id"], "at": utcnow().isoformat(), "mode": "read_only_retrospective",
              "status": "complete" if len(rows) == count else "insufficient_usable_resolutions",
              "requested_questions": count, "evaluated_questions": len(rows), "scored_questions": len(scored),
              "mean_brier": sum(row["brier"] for row in scored)/len(scored) if scored else None,
              "official_peer_score": None, "limitations": LIMITATION, "questions": rows, "live_submissions": 0}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2)+"\n")
    lines = ["# Quantura Metaculus readiness check", "", LIMITATION, "", f"Live submissions: **0**. Evaluated: **{len(rows)}**; scored: **{len(scored)}**.",
             f"Mean Brier: **{result['mean_brier']}** (lower is better; not an official peer score).", "",
             "| Question | P(Yes) | Outcome | Brier | Community Brier |", "|---|---:|---|---:|---:|"]
    for row in rows:
        title = row["title"].replace("|", " ").replace("\n", " ")
        lines.append(f"| [{title}]({row['url']}) | {row.get('probability_yes', 'abstained')} | {row['outcome']} | {row.get('brier', '—')} | {row.get('community_brier', 'unavailable')} |")
    output.with_suffix(".md").write_text("\n".join(lines)+"\n")
    print(json.dumps({key:value for key,value in result.items() if key != "questions"}))
    if len(rows) != count:
        raise SystemExit(2)


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--count",type=int,default=5)
    parser.add_argument("--output",type=Path,default=Path("artifacts/metaculus-readiness.json"))
    args=parser.parse_args()
    if not 1 <= args.count <= 5:
        parser.error("Readiness check is limited to five questions")
    run(args.count,args.output)
