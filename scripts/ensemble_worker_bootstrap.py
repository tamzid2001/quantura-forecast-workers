"""Claim a private forecast before installing ML dependencies; never log inputs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-file", required=True)
    parser.add_argument("--notify-failure", action="store_true")
    args = parser.parse_args()
    job_id = os.environ.get("FORECAST_JOB_ID", "")
    token = os.environ.get("QUANTURA_ENSEMBLE_WORKER_TOKEN", "")
    base = os.environ.get("QUANTURA_WORKER_API_BASE", "https://quantura.studio").rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,220}", job_id) or len(token) < 32 or base != "https://quantura.studio":
        print("WORKER_CONFIGURATION_INVALID", file=sys.stderr)
        return 1
    path = Path(args.job_file)

    def post(action, body):
        request = urllib.request.Request(
            f"{base}/api/internal/ensemble-forecasts/{job_id}/{action}",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)

    try:
        if args.notify_failure:
            post("fail", {"code": "WORKER_STARTUP_FAILED", "retryable": True, "only_if_queued": not path.exists()})
            return 0
        job = post("claim", {"worker_repository": os.environ.get("GITHUB_REPOSITORY"),
                             "worker_run_id": os.environ.get("GITHUB_RUN_ID")})["data"]
        if job.get("forecast_id") != job_id:
            raise ValueError("WORKER_JOB_MISMATCH")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(job, handle)
        post("progress", {"phase": "environment_setup", "completed_models": 0,
                          "total_models": sum(bool(m.get("enabled")) and float(m.get("weight", 0)) > 0
                                              for m in job["request"]["models"].values())})
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
                handle.write("claimed=true\n")
        print(json.dumps({"event": "forecast_claimed", "forecast_id": job_id, "phase": "environment_setup"}))
        return 0
    except urllib.error.HTTPError as exc:
        # A canceled, completed, or previously claimed job must never execute.
        if exc.code == 409 and not path.exists() and not args.notify_failure:
            print(json.dumps({"event": "forecast_not_claimable", "forecast_id": job_id}))
            return 0
        print(f"WORKER_CALLBACK_HTTP_{exc.code}", file=sys.stderr)
    except Exception as exc:
        # Error strings and response bodies may contain private request details.
        print(f"WORKER_BOOTSTRAP_FAILED:{type(exc).__name__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
