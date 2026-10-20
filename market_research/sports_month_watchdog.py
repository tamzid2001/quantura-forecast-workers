"""Recover one approved retrospective study from GitHub artifacts, never trades.

No external broker, database, storage bucket or customer API is accessible here.
The repository variable SPORTS_MONTH_BACKTEST_ENABLED is an explicit stop gate.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import gzip
import io
import json
from pathlib import Path
import re
import subprocess
import time
import zipfile

REPOSITORY = "tamzid2001/quantura-forecast-workers"
WORKFLOW = "sports-month-backtest.yml"
SCHEMA = "quantura-sports-month-v1"
ACTIVE = {"queued", "in_progress", "requested", "waiting", "pending"}
MAX_ZIP = 64*1024*1024
MAX_CATALOG = 512*1024*1024


def github(path, body=None, binary=False):
    if not path.startswith(("actions/", "actions?")):
        raise ValueError("UNAPPROVED_GITHUB_PATH")
    command = ["gh", "api", f"repos/{REPOSITORY}/{path}"]
    payload = None
    if body is not None:
        if path != f"actions/workflows/{WORKFLOW}/dispatches":
            raise ValueError("UNAPPROVED_DISPATCH")
        command += ["--method", "POST", "--input", "-"]
        payload = json.dumps(body).encode()
    result = subprocess.run(command, input=payload, capture_output=True, timeout=90, check=False)
    if result.returncode:
        raise RuntimeError("GITHUB_WATCHDOG_REQUEST_FAILED")
    if len(result.stdout) > MAX_ZIP:
        raise ValueError("GITHUB_RESPONSE_TOO_LARGE")
    if body is not None:
        return None
    return result.stdout if binary else json.loads(result.stdout)


def validate_campaign(config):
    if (config.get("schema") != "quantura-sports-month-campaign-v1"
            or config.get("repository") != REPOSITORY or config.get("workflow") != WORKFLOW
            or type(config.get("root_run_id")) is not int or config["root_run_id"] <= 0
            or not re.fullmatch(r"[0-9a-f]{40}", config.get("code_sha", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", config.get("runtime_fingerprint", ""))):
        raise ValueError("UNAPPROVED_RESEARCH_CAMPAIGN")
    first, last = date.fromisoformat(config["first"]), date.fromisoformat(config["last"])
    if first > last or (last-first).days > 365:
        raise ValueError("INVALID_STUDY_DATES")


def runs(client=github):
    result = []
    for page in range(1, 11):
        data = client(f"actions/workflows/{WORKFLOW}/runs?branch=main&per_page=100&page={page}")
        batch = data.get("workflow_runs")
        if not isinstance(batch, list):
            raise ValueError("INVALID_GITHUB_RUN_LIST")
        result.extend(batch)
        if len(batch) < 100:
            return sorted(result, key=lambda r: int(r["id"]), reverse=True)
    raise ValueError("RUN_DISCOVERY_INCOMPLETE")


def active(runs_list):
    for run in runs_list:
        if run.get("status") in ACTIVE:
            return run
        if run.get("status") != "completed":
            raise ValueError("UNKNOWN_RUN_STATUS")
    return None


def validate_run(run, config):
    if (run.get("head_branch") != "main" or run.get("path") != ".github/workflows/"+WORKFLOW
            or run.get("event") != "workflow_dispatch" or int(run["id"]) < config["root_run_id"]
            or run.get("repository", {}).get("full_name") != REPOSITORY):
        raise ValueError("UNTRUSTED_RESEARCH_RUN")


def artifacts(run_id, client=github):
    data = client(f"actions/runs/{int(run_id)}/artifacts?per_page=100")
    values = data.get("artifacts")
    if not isinstance(values, list) or data.get("total_count", len(values)) > len(values):
        raise ValueError("ARTIFACT_DISCOVERY_INCOMPLETE")
    result = {}
    for item in values:
        name = item.get("name")
        if item.get("expired"):
            continue
        if name in result:
            raise ValueError("DUPLICATE_CHECKPOINT_ARTIFACT")
        result[name] = item
    return result


def artifact_json(artifact, member, compressed=False, client=github):
    if artifact.get("size_in_bytes", 0) > MAX_ZIP:
        raise ValueError("CHECKPOINT_ARCHIVE_TOO_LARGE")
    raw = client(f"actions/artifacts/{int(artifact['id'])}/zip", binary=True)
    if len(raw) > MAX_ZIP:
        raise ValueError("CHECKPOINT_ARCHIVE_TOO_LARGE")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or member not in names:
            raise ValueError("INVALID_CHECKPOINT_MEMBERS")
        info = archive.getinfo(member)
        if info.file_size > (MAX_ZIP if compressed else 4*1024*1024):
            raise ValueError("CHECKPOINT_MEMBER_TOO_LARGE")
        packed = archive.read(info)
    if compressed:
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
            packed = stream.read(MAX_CATALOG+1)
        if len(packed) > MAX_CATALOG:
            raise ValueError("CHECKPOINT_JSON_TOO_LARGE")
    return json.loads(packed)


def read_report(available, config, client=github):
    artifact = available.get("sports-month-report")
    if artifact is None:
        return None
    report = artifact_json(artifact, "report.json", client=client)
    if (report.get("schema") != SCHEMA or (report.get("first"), report.get("last")) != (config["first"], config["last"])
            or type(report.get("done")) is not bool or type(report.get("progress")) is not int
            or report["progress"] < 0 or report.get("mode") != "retrospective_paper_only"):
        raise ValueError("INVALID_RESEARCH_REPORT")
    # A large series can span several catalog passes. Count processed events,
    # including an unfinished page, instead of only completed series indexes.
    # This fixes liveness measurement without changing the pinned model code.
    if "catalog_counts" in report:
        counts, statuses = report["catalog_counts"], report.get("statuses", {})
        values = [counts.get("poly_events", 0), counts.get("kalshi_events", 0),
                  statuses.get("complete", 0), statuses.get("excluded", 0)]
        if any(type(v) is not int or v < 0 for v in values):
            raise ValueError("INVALID_RESEARCH_PROGRESS")
        report = {**report, "progress": sum(values), "progress_basis": "catalog_events_plus_finished_outcomes"}
    return report


def inspect(config, enabled, client=github, now=None):
    """Plan a bounded recovery. Uncertain provenance or repeated failure stops it."""
    validate_campaign(config)
    now = int(time.time() if now is None else now)
    base = {"schema": "quantura-sports-month-watchdog-v1", "checked_at": now,
            "repository": REPOSITORY, "first": config["first"], "last": config["last"],
            "mode": "retrospective_paper_only", "dispatched": False}
    if not enabled:
        return {**base, "action": "none", "reason": "campaign_disabled"}
    listed = runs(client)
    running = active(listed)
    if running:
        return {**base, "action": "none", "reason": "run_active", "run_id": running["id"], "run_url": running.get("html_url")}
    relevant = [r for r in listed if int(r["id"]) >= config["root_run_id"]]
    if not relevant:
        raise ValueError("APPROVED_STUDY_RUN_NOT_FOUND")
    if relevant[0].get("conclusion") in ("cancelled", "action_required"):
        return {**base, "action": "none", "reason": "operator_resume_required", "run_id": relevant[0]["id"]}
    if len(relevant) >= 3 and all(r.get("conclusion") in ("failure", "timed_out") for r in relevant[:3]):
        raise RuntimeError("REPEATED_WORKFLOW_FAILURES_OPERATOR_REVIEW_REQUIRED")
    reports = []
    chosen = None
    # Only a checkpoint from a single validated run is used. Never merge shards
    # from unrelated runs or silently initialize a fresh historical campaign.
    for run in relevant[:12]:
        validate_run(run, config)
        available = artifacts(run["id"], client)
        report = read_report(available, config, client)
        if report:
            reports.append(report)
        if chosen is None and report and "sports-month-catalog" in available:
            catalog = artifact_json(available["sports-month-catalog"], "catalog.json.gz", True, client)
            if (catalog.get("schema") != SCHEMA or catalog.get("runtime_fingerprint") != config["runtime_fingerprint"]
                    or (catalog.get("first"), catalog.get("last")) != (config["first"], config["last"])):
                raise ValueError("CHECKPOINT_POLICY_OR_DATES_CHANGED")
            if report["done"]:
                if not catalog.get("done") or not all(f"sports-month-shard-{i}" in available for i in range(4)):
                    raise ValueError("COMPLETION_CHECKPOINT_INCOMPLETE")
                return {**base, "action": "none", "reason": "study_complete", "run_id": run["id"], "progress": report["progress"]}
            if not catalog.get("done") or all(f"sports-month-shard-{i}" in available for i in range(4)):
                chosen = run, report
        if chosen and len(reports) >= 3:
            break
    if len(reports) >= 3 and len({r["progress"] for r in reports[:3]}) == 1:
        raise RuntimeError("NO_PROGRESS_OPERATOR_REVIEW_REQUIRED")
    if chosen is None:
        raise ValueError("NO_COMPLETE_RESUME_CHECKPOINT")
    run, report = chosen
    updated = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00")).timestamp()
    if now-updated < 120:
        return {**base, "action": "none", "reason": "handoff_cooldown", "run_id": run["id"]}
    stagnant = 0
    for previous in reports[1:]:
        if previous["progress"] != report["progress"]:
            break
        stagnant += 1
    return {**base, "action": "resume", "reason": "unfinished_checkpoint_without_active_run", "run_id": run["id"],
            "progress": report["progress"], "body": {"ref": "main", "inputs": {
                "first": config["first"], "last": config["last"], "resume_run_id": str(run["id"]),
                "code_sha": config["code_sha"], "iteration": "0", "previous_progress": str(report["progress"]),
                "stagnant": str(stagnant)}}}


def recover(config, enabled, client=github, now=None):
    plan = inspect(config, enabled, client, now)
    if plan["action"] != "resume":
        return plan
    # Recheck immediately before the only mutation. Study concurrency serializes
    # a remaining dispatch race with the worker's own automatic handoff.
    running = active(runs(client))
    if running:
        plan.pop("body", None)
        return {**plan, "action": "none", "reason": "successor_appeared", "run_id": running["id"]}
    client(f"actions/workflows/{WORKFLOW}/dispatches", body=plan.pop("body"))
    return {**plan, "dispatched": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enabled", choices=("true", "false"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(Path(__file__).with_name("sports_month_campaign.json").read_text())
    try:
        result = recover(config, args.enabled == "true")
    except (RuntimeError, ValueError, KeyError, OSError, EOFError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        result = {"schema": "quantura-sports-month-watchdog-v1", "action": "operator_review",
                  "mode": "retrospective_paper_only", "dispatched": False,
                  "error_code": str(error) if re.fullmatch(r"[A-Z0-9_]{1,100}", str(error)) else type(error).__name__}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2)+"\n")
        print(json.dumps(result), flush=True)
        raise SystemExit(1) from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
