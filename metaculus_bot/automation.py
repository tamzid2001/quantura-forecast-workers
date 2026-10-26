"""Queue handoff and public operational heartbeat; never publish forecast contents."""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def github(path, body=None, method="PUT"):
    args = ["gh", "api", path]
    if body is not None:
        args += ["--method", method, "--input", "-"]
    result = subprocess.run(args, input=json.dumps(body).encode() if body else None,
                            capture_output=True, check=True)
    return json.loads(result.stdout) if result.stdout else {}


def queue_ids(queue, bot_id, now=None, maximum=12):
    now = now or datetime.now(timezone.utc)
    at = datetime.fromisoformat(queue["at"].replace("Z", "+00:00"))
    if str(queue.get("bot_id")) != str(bot_id) or not 0 <= (now - at).total_seconds() <= 1800:
        raise ValueError("STALE_OR_WRONG_BOT_QUEUE")
    if type(maximum) is not int or not 1 <= maximum <= 100:
        raise ValueError("INVALID_QUEUE_BATCH_LIMIT")
    rows = queue.get("questions", [])[:maximum]
    if any(type(row.get(key)) is not int or row[key] <= 0 for row in rows for key in ("question_id", "post_id")):
        raise ValueError("INVALID_QUEUE_IDENTIFIERS")
    return [row["question_id"] for row in rows], sorted({row["post_id"] for row in rows})


def restore_queue(path):
    repo = os.environ["GITHUB_REPOSITORY"]
    runs = github(f"repos/{repo}/actions/workflows/metaculus-poll.yml/runs?branch=main&per_page=10")["workflow_runs"]
    for run in runs:
        if run.get("event") not in {"schedule", "workflow_dispatch"}:
            continue
        artifacts = github(f"repos/{repo}/actions/runs/{run['id']}/artifacts")["artifacts"]
        matches = [artifact for artifact in artifacts if artifact["name"] == "metaculus-open-queue" and not artifact["expired"]]
        if not matches:
            continue
        result = subprocess.run(["gh", "api", f"repos/{repo}/actions/artifacts/{matches[0]['id']}/zip"],
                                capture_output=True, check=True)
        with zipfile.ZipFile(io.BytesIO(result.stdout)) as archive:
            members = [member for member in archive.infolist() if member.filename == "metaculus-queue.json"]
            if len(members) != 1 or members[0].file_size > 2_000_000:
                raise ValueError("INVALID_QUEUE_ARTIFACT")
            queue = json.loads(archive.read(members[0]))
        maximum = min(100, max(6, int(os.environ.get("BOT_MAX", "2")) * 3))
        ids, posts = queue_ids(queue, os.environ["METACULUS_BOT_ID"], maximum=maximum)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(queue))
        return ids, posts
    return [], []


def heartbeat(report):
    # The public history proves activity, not forecast quality. Exact pending
    # answers and private reasoning remain only in encrypted checkpoint artifacts.
    value = json.loads(report.read_text()) if report.exists() else {}
    public = {"at": datetime.now(timezone.utc).isoformat(), "bot_id": os.environ["METACULUS_BOT_ID"],
              "dry_run": os.environ.get("METACULUS_DRY_RUN", "true") != "false",
              "run_id": os.environ["GITHUB_RUN_ID"], "counts": value.get("counts", {}),
              "discovered_open_questions": value.get("discovered_open_questions", 0)}
    repo = os.environ["GITHUB_REPOSITORY"]
    endpoint = f"repos/{repo}/contents/status/metaculus.json"
    # The first run has no status file. Only a confirmed 404 permits creation.
    result = subprocess.run(["gh", "api", endpoint], capture_output=True)
    if result.returncode:
        if b"404" not in result.stderr:
            raise RuntimeError("HEARTBEAT_READ_FAILED")
        prior = {}
    else:
        prior = json.loads(result.stdout)
    body = {"message": "Record Metaculus worker activity [skip ci]", "branch": "main",
            "content": base64.b64encode((json.dumps(public, indent=2) + "\n").encode()).decode()}
    if prior.get("sha"):
        body["sha"] = prior["sha"]
    github(endpoint, body)
    print(json.dumps({"event": "metaculus_public_heartbeat", "run_id": public["run_id"], "dry_run": public["dry_run"]}))


def dispatch_decision(runs, now=None):
    now = now or datetime.now(timezone.utc)
    runs = [run for run in runs if run.get("event") in {"schedule", "workflow_dispatch"}]
    active = [r for r in runs if r.get("status") in {"queued", "in_progress", "waiting", "pending", "requested"}]
    if active:
        stale = any((now - datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))).total_seconds()
                    >= (3600 if r.get("status") == "in_progress" else 900) for r in active)
        return False, "active_worker_stalled" if stale else "active_worker", stale
    if not runs:
        return True, "no_worker", False
    latest = max(runs, key=lambda r: r["created_at"])
    if latest.get("conclusion") in {"failure", "timed_out", "cancelled", "startup_failure", "action_required"}:
        finished = datetime.fromisoformat(latest.get("updated_at", latest["created_at"]).replace("Z", "+00:00"))
        needed = (now - finished).total_seconds() >= 120
        return needed, "failed_worker_retry" if needed else "failed_worker_cooldown", False
    # A 20-minute threshold with a 15-minute external cron can skip every other
    # tick. Twelve minutes tolerates schedule jitter while preserving cadence.
    needed = (now - datetime.fromisoformat(latest["created_at"].replace("Z", "+00:00"))).total_seconds() >= 720
    return needed, "cadence_due" if needed else "recent_worker", False


def dispatch_needed(runs, now=None):
    return dispatch_decision(runs, now)[0]


def watchdog(target):
    if os.environ.get("METACULUS_BOT_ENABLED") != "true":
        print(json.dumps({"event": "metaculus_watchdog", "status": "disabled"}))
        return
    workflows = {"poll": "metaculus-poll.yml", "forecast": "metaculus-bot.yml"}
    stalled = []
    for kind in workflows if target == "both" else [target]:
        workflow = workflows[kind]
        repo = os.environ["GITHUB_REPOSITORY"]
        runs = github(f"repos/{repo}/actions/workflows/{workflow}/runs?branch=main&per_page=25")["workflow_runs"]
        needed, reason, unhealthy = dispatch_decision(runs)
        if unhealthy:
            stalled.append(kind)
        if needed:
            body = {"ref": "main"}
            if kind == "forecast":
                body["inputs"] = {"scope": "competitions", "submit": "true", "max_questions": "10"}
            github(f"repos/{repo}/actions/workflows/{workflow}/dispatches", body, "POST")
        print(json.dumps({"event": "metaculus_watchdog", "target": kind, "dispatched": needed, "reason": reason}))
    if stalled:
        raise RuntimeError("METACULUS_WORKER_STALLED:" + ",".join(stalled))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["queue", "heartbeat", "watchdog"])
    parser.add_argument("--file", type=Path)
    parser.add_argument("--target", choices=["poll", "forecast", "both"], default="both")
    args = parser.parse_args()
    if args.command == "watchdog":
        watchdog(args.target)
        return
    if args.file is None:
        parser.error("--file is required for queue and heartbeat")
    if args.command == "heartbeat":
        heartbeat(args.file)
        return
    try:
        ids, posts = restore_queue(args.file)
    except ValueError as error:
        print(json.dumps({"event": "metaculus_queue_deferred", "reason": str(error)}))
        ids, posts = [], []
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("ready=" + str(bool(ids)).lower() + "\n")
            output.write("question_ids=" + json.dumps(ids) + "\npost_ids=" + json.dumps(posts) + "\n")
    print(json.dumps({"event": "metaculus_queue_loaded", "questions": len(ids)}))


if __name__ == "__main__":
    main()
