"""Fetch verified public worker receipts and preserve a prospective experiment.

Runs with GitHub Actions read permission only. No exchange client, model SDK,
credential export, email, broker or cloud deployment is invoked.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

from .data import load_inputs, timestamp
from .prospective import assess, initialize, restore
from .study import analyze, markdown

REPOSITORY = "tamzid2001/quantura-forecast-workers"
WORKFLOW = ".github/workflows/sports-strategy-research.yml"
SOURCES = {
    "paper": ("quantura-games-p01-paper-stats", ".github/workflows/games-p01-paper.yml",
              {"stats.json", "paper-ledger.csv", "comparison-stats.json", "comparison-ledger.csv", "comparison-summary.md"}),
    "limit": ("quantura-games-p01-limit-paper-stats", ".github/workflows/games-p01-limit-paper.yml",
              {"stats.json", "limit-ledger.csv"}),
    "month": ("sports-month-report", ".github/workflows/sports-month-backtest.yml",
              {"REPORT.md", "report.json", "ledger.csv"})}


def gh(path, binary=False):
    result = subprocess.run(["gh", "api", f"repos/{REPOSITORY}/{path}"],
                            capture_output=True, timeout=90, check=False)
    if result.returncode:
        raise RuntimeError("GITHUB_RESEARCH_READ_FAILED:" + path.split("?")[0])
    if len(result.stdout) > 8 * 1024 * 1024:
        raise ValueError("RESEARCH_RESPONSE_TOO_LARGE")
    return result.stdout if binary else json.loads(result.stdout)


def unpack(blob, allowed):
    if len(blob) > 8 * 1024 * 1024:
        raise ValueError("RESEARCH_ARCHIVE_TOO_LARGE")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        infos = archive.infolist()
        if (not infos or len(infos) > 10 or sum(i.file_size for i in infos) > 32 * 1024 * 1024
                or len({i.filename for i in infos}) != len(infos)):
            raise ValueError("INVALID_RESEARCH_ARCHIVE")
        for info in infos:
            name = PurePosixPath(info.filename)
            if (info.is_dir() or name.is_absolute() or ".." in name.parts
                    or str(name) not in allowed or info.file_size > 16 * 1024 * 1024):
                raise ValueError("INVALID_RESEARCH_ARCHIVE_MEMBER")
        return {i.filename: archive.read(i) for i in infos}


def latest(name, workflow, checkpoint=False):
    data = gh("actions/artifacts?" + urlencode({"name": name, "per_page": 20}))
    artifacts = sorted(data.get("artifacts", []), key=lambda a: (a["created_at"], a["id"]), reverse=True)
    for artifact in artifacts:
        if (artifact.get("expired") or artifact.get("name") != name
                or artifact.get("workflow_run", {}).get("head_branch") != "main"):
            continue
        run = gh(f"actions/runs/{artifact['workflow_run']['id']}")
        if (run.get("repository", {}).get("full_name") != REPOSITORY or run.get("head_branch") != "main"
                or run.get("event") not in {"schedule", "workflow_dispatch"} or run.get("path") != workflow):
            raise ValueError("UNTRUSTED_RESEARCH_INPUT_PROVENANCE")
        if run.get("status") == "completed" and (run.get("conclusion") == "success"
                                                 or (checkpoint and run.get("conclusion") in {"failure", "cancelled"})):
            return artifact
    return None


def collect(directory):
    root = Path(directory)
    receipts = {}
    for source, (name, workflow, allowed) in SOURCES.items():
        artifact = latest(name, workflow)
        if artifact is None:
            receipts[source] = {"status": "unavailable"}
            target = root / source
            target.mkdir(parents=True, exist_ok=True)
            (target / "artifact.json").write_text(json.dumps(receipts[source]) + "\n")
            continue
        blob = gh(f"actions/artifacts/{artifact['id']}/zip", True)
        members = unpack(blob, allowed)
        required = {"paper": {"stats.json", "comparison-ledger.csv"},
                    "limit": {"stats.json", "limit-ledger.csv"},
                    "month": {"report.json", "ledger.csv"}}[source]
        if not required <= set(members):
            raise ValueError("REQUIRED_RESEARCH_INPUT_MEMBER_MISSING")
        target = root / source
        target.mkdir(parents=True, exist_ok=True)
        for name, body in members.items():
            (target / name).write_bytes(body)
        receipt = {**artifact, "archive_sha256": hashlib.sha256(blob).hexdigest()}
        (target / "artifact.json").write_text(json.dumps(receipt, indent=2) + "\n")
        receipts[source] = receipt
    return receipts


def registry(now):
    artifact = latest("quantura-sports-research-registry-v1", WORKFLOW, checkpoint=True)
    if artifact:
        body = unpack(gh(f"actions/artifacts/{artifact['id']}/zip", True), {"registry.json"})
        return restore(json.loads(body["registry.json"]), now)
    completed = gh("actions/workflows/sports-strategy-research.yml/runs?status=completed&per_page=1")
    if completed.get("workflow_runs"):
        raise RuntimeError("RESEARCH_REGISTRY_MISSING_CANNOT_SILENTLY_RESTART")
    return initialize(now)


def health(sources, now):
    result = {}
    for name, source in sources.items():
        warnings = []
        if source.get("status") != "available":
            warnings.append("SOURCE_UNAVAILABLE")
        as_of = source.get("as_of")
        age = None if as_of is None else now - timestamp(as_of)
        if age is not None and age < -60:
            warnings.append("SOURCE_TIMESTAMP_IN_FUTURE")
        max_age = 8 * 3600 if name == "limit" else 6 * 3600
        if age is not None and age > max_age:
            warnings.append("SOURCE_STATISTICS_STALE")
        if name == "paper" and source.get("next_hour") and now - timestamp(source["next_hour"]) > 2 * 3600:
            warnings.append("HOURLY_REPLAY_NOT_CAUGHT_UP")
        if name == "month" and source.get("study_complete") is False:
            warnings.append("HISTORICAL_REPLAY_PARTIAL")
        result[name] = {"age_seconds": age, "warnings": warnings}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--draws", type=int, default=2000)
    args = parser.parse_args(argv)
    now = time.time()
    args.output.mkdir(parents=True, exist_ok=True)
    state = registry(now)
    # Save registration before fetching/analyzing data so later failures cannot
    # silently shift an already-created prospective evaluation window.
    (args.output / "registry.json").write_text(json.dumps(state, indent=2, allow_nan=False) + "\n")
    collect(args.output / "inputs")
    trades, sources = load_inputs(args.output / "inputs")
    report = analyze(trades, sources, args.draws)
    report["source_health"] = health(sources, now)
    report["generated_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
    prospective = assess(state, trades, sources, now, args.draws)
    (args.output / "research.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    (args.output / "prospective.json").write_text(json.dumps(prospective, indent=2, allow_nan=False) + "\n")
    text = markdown(report)
    text += "\n## Frozen prospective paper experiment\n\n"
    text += f"Registration: {state['created_at']}. Starts: {state['starts_at']}. Ends: {state['ends_at']}. Earliest review: {state['evaluate_after']}.\n\n"
    text += "The finite filter family is frozen for 60 days. Daily views are descriptive. Evaluation is deferred until the fixed deadline and settlement grace period. No workflow can automatically enable live orders.\n\n"
    text += "Source health: `" + json.dumps(report["source_health"], sort_keys=True) + "`\n"
    (args.output / "REPORT.md").write_text(text)
    print(json.dumps({"mode": "research_only", "live_orders": 0,
                      "registration_sha256": state["design"]["sha256"],
                      "source_health": report["source_health"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
