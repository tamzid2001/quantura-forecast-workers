"""Call existing maintenance schedules; credentials and response bodies stay private."""
from datetime import datetime
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

API = "https://quantura-api.vercel.app"
LEGACY = "https://quantura-legacy-api.vercel.app"
NEWSLETTER = "https://quantura-newsletter.vercel.app"
ORIGINS = {**{j: API for j in ("autopilot-reconcile", "automation-schedules", "forecast-boundary-alerts",
                              "forecast-lifecycle", "screener-close", "fiscaldata-refresh")},
           **{j: LEGACY for j in ("social-dispatch", "forecast-report", "social-planner", "newsletter-daily")},
           "newsletter-weekly": NEWSLETTER}
COUNTERS = {"checked", "processed", "delivered", "sent", "skipped", "failed", "queued", "completed", "updated", "public_writes"}


def scheduled_jobs(now):
    now = now.astimezone(ZoneInfo("America/New_York"))
    jobs = ["autopilot-reconcile", "automation-schedules", "forecast-boundary-alerts", "forecast-lifecycle"]
    if now.minute < 15:
        jobs += ["social-dispatch", "screener-close"]
    if now.minute < 15 or 30 <= now.minute < 45:
        jobs.append("forecast-report")
    if now.hour == 7 and 15 <= now.minute < 30:
        jobs.append("social-planner")
    if now.hour == 13 and now.minute < 15:
        jobs.append("newsletter-daily")
    if now.weekday() == 0 and now.hour == 9 and now.minute < 15:
        jobs.append("newsletter-weekly")
    return jobs


def invoke(job, token):
    if job not in ORIGINS or len(token) < 32:
        raise ValueError("SCHEDULER_CONFIGURATION_INVALID")
    request = Request(ORIGINS[job]+"/api/internal/cron/"+job, data=b"{}",
                      headers={"Authorization": "Bearer "+token, "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=90) as response:
            result = json.load(response)
        if result.get("ok") is not True:
            raise RuntimeError("BACKEND_JOB_REJECTED")
        counters = {k: v for k, v in result.get("result", {}).items() if k in COUNTERS and type(v) in (int, float)}
        print(json.dumps({"event": "backend_job_complete", "job": job, "ok": True, "counts": counters}), flush=True)
        return True
    except HTTPError as error:
        code = "BACKEND_HTTP_"+str(error.code)
    except (OSError, ValueError, RuntimeError, URLError):
        code = "BACKEND_JOB_UNAVAILABLE"
    print(json.dumps({"event": "backend_job_failed", "job": job, "ok": False, "code": code}), flush=True)
    return False


def main(job="scheduled"):
    if job != "scheduled" and job not in ORIGINS:
        raise ValueError("UNSUPPORTED_SCHEDULER_JOB")
    token = os.environ.get("QUANTURA_SCHEDULED_WORKER_TOKEN", "")
    if len(token) < 32:
        raise ValueError("SCHEDULER_CREDENTIAL_MISSING")
    jobs = scheduled_jobs(datetime.now(ZoneInfo("America/New_York"))) if job == "scheduled" else [job]
    results = [invoke(selected, token) for selected in jobs]
    if not all(results):
        raise SystemExit("BACKEND_SCHEDULE_RETRY_REQUIRED")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) == 2 else "scheduled")
