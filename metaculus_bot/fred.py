"""FRED resolution-source observations and bounded economic research context.

Credentials stay in the worker environment. A suggested series is evidence,
not permission to replace a question's stated resolution source.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

API = "https://api.stlouisfed.org/fred/"
SERIES_URL = "https://fred.stlouisfed.org/series/"
TOPICS = {
    "unemployment": ("UNRATE", "Civilian unemployment rate", "Percent", "1MS"),
    "inflation": ("CPIAUCSL", "Consumer Price Index level (not inflation rate)", "Index 1982-1984=100", "1MS"),
    "gdp": ("GDP", "Nominal Gross Domestic Product level", "Billions of Dollars", "1QS-JAN"),
    "federal funds": ("FEDFUNDS", "Effective federal funds rate", "Percent", "1MS"),
    "treasury yield": ("DGS10", "10-year Treasury constant maturity yield", "Percent", "1D"),
    "payroll": ("PAYEMS", "All employees, total nonfarm", "Thousands of Persons", "1MS"),
    "recession": ("USREC", "NBER recession indicator (published with lag)", "0 or 1", "1MS"),
}
DISCOVERY_TERMS = ("housing starts", "retail sales", "industrial production", "personal consumption",
                   "mortgage rate", "oil price", "money supply", "trade balance", "consumer sentiment", "job openings")


class FredError(RuntimeError):
    """Safe error codes only: requests exceptions can contain the key-bearing URL."""


def get(path: str, params: dict, session=None) -> dict:
    key = os.environ.get("FRED_API_KEY", "")
    if not re.fullmatch(r"[a-z0-9]{32}", key):
        raise FredError("FRED_NOT_CONFIGURED")
    if path not in {"series", "series/search", "series/observations"}:
        raise FredError("FRED_PATH_UNSUPPORTED")
    client = session or requests.Session()
    if session is None:
        client.trust_env = False
    try:
        with client.get(API + path, params={**params, "file_type": "json", "api_key": key},
                        headers={"Accept": "application/json"}, timeout=(5, 15),
                        allow_redirects=False, stream=True) as response:
            if response.status_code == 429:
                raise FredError("FRED_RATE_LIMITED")
            if response.status_code != 200:
                raise FredError("FRED_UPSTREAM_UNAVAILABLE")
            raw = bytearray()
            for chunk in response.iter_content(65536):
                raw.extend(chunk)
                if len(raw) > 4_000_000:
                    raise FredError("FRED_RESPONSE_TOO_LARGE")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("error_code"):
                raise FredError("FRED_INVALID_RESPONSE")
            return data
    except (requests.RequestException, ValueError, TypeError):
        raise FredError("FRED_UPSTREAM_UNAVAILABLE") from None
    finally:
        if session is None:
            client.close()


def candidates(question) -> list[str]:
    criteria = "\n".join(filter(None, [question.resolution_criteria, question.fine_print, question.background_info]))
    exact = re.findall(r"https://fred\.stlouisfed\.org/series/([A-Za-z0-9_.-]{1,100})(?=[\s/?#)\]<>\"']|$)", criteria)
    text = question.question_text.lower()
    return list(dict.fromkeys(exact + [v[0] for k, v in TOPICS.items() if k in text]))[:3]


def series_metadata(data: dict, series: str) -> dict:
    rows = data.get("seriess")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("id") != series:
        raise FredError("FRED_SERIES_MISMATCH")
    return rows[0]


def history(spec: dict, *, now=None, fetch=get) -> tuple[list[dict], dict]:
    series = spec.get("series_id", "")
    if not isinstance(series, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", series):
        raise FredError("FRED_INVALID_SERIES")
    clock = now or datetime.now(timezone.utc)
    today = clock.astimezone(ZoneInfo("America/Chicago")).date().isoformat()
    vintage = spec.get("vintage_date", today)
    try:
        parsed = datetime.strptime(vintage, "%Y-%m-%d").date().isoformat()
    except (ValueError, TypeError):
        raise FredError("FRED_INVALID_VINTAGE") from None
    if parsed != vintage or vintage > today:
        raise FredError("FRED_INVALID_VINTAGE")
    params = {"series_id": series, "realtime_start": vintage, "realtime_end": vintage}
    info = series_metadata(fetch("series", params), series)
    if spec.get("unit") != info.get("units"):
        raise FredError("FRED_RESOLUTION_UNITS_MISMATCH")
    expected = {"D": "1D", "M": "1MS", "Q": "1QS-JAN", "A": "1YS-JAN"}.get(info.get("frequency_short"))
    if not expected or spec.get("frequency") != expected:
        raise FredError("FRED_RESOLUTION_FREQUENCY_MISMATCH")
    payload = fetch("series/observations", {**params, "observation_end": today, "sort_order": "desc", "limit": 5000, "offset": 0, "output_type": 1})
    rows, seen = [], set()
    observations = payload.get("observations")
    if not isinstance(observations, list) or len(observations) > 5000:
        raise FredError("FRED_INVALID_RESPONSE")
    for row in observations:
        if not isinstance(row, dict):
            raise FredError("FRED_INVALID_OBSERVATION")
        try:
            day = datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError):
            raise FredError("FRED_INVALID_OBSERVATION") from None
        if day.date().isoformat() != row["date"] or row["date"] in seen or day.date() > clock.date():
            raise FredError("FRED_INVALID_OBSERVATION")
        seen.add(row["date"])
        value = row.get("value")
        if value in (".", "", None):
            continue
        try:
            number = float(value)
        except (ValueError, TypeError):
            raise FredError("FRED_INVALID_OBSERVATION") from None
        if not math.isfinite(number):
            raise FredError("FRED_INVALID_OBSERVATION")
        rows.append({"timestamp": day.isoformat(), "target": number})
    rows.sort(key=lambda r: r["timestamp"])
    rows = rows[-500:]
    if not rows:
        raise FredError("FRED_HISTORY_EMPTY")
    receipt = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return rows, {"url": SERIES_URL + series, "data_url": SERIES_URL + series,
                  "sha256": hashlib.sha256(receipt).hexdigest(), "last_observation": rows[-1]["timestamp"],
                  "observed_rows": len(rows), "unit": info["units"], "frequency": expected,
                  "vintage_date": vintage, "vintage_precision": "date", "series_id": series}


def evidence(question, *, fetch=get) -> list[dict]:
    if not os.environ.get("FRED_API_KEY"):
        return []
    results = []
    selected = candidates(question)
    if not selected:
        term = next((term for term in DISCOVERY_TERMS if term in question.question_text.lower()), None)
        if term:
            try:
                found = fetch("series/search", {"search_text": term, "limit": 3, "order_by": "search_rank", "sort_order": "desc"}).get("seriess")
                if isinstance(found, list):
                    selected = list(dict.fromkeys(r["id"] for r in found if isinstance(r, dict) and isinstance(r.get("id"), str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", r["id"])))[:3]
            except FredError:
                return []
    for series in selected:
        try:
            info = series_metadata(fetch("series", {"series_id": series}), series)
            observed = fetch("series/observations", {"series_id": series, "sort_order": "desc", "limit": 12, "output_type": 1})
            observations = observed.get("observations")
            if not isinstance(observations, list) or any(not isinstance(r, dict) for r in observations):
                raise FredError("FRED_INVALID_RESPONSE")
            # This is context in the source's native units, never a silent level-to-growth transform.
            excerpt = json.dumps({"series_id": series, "title": info.get("title"), "units": info.get("units"),
                                  "frequency": info.get("frequency"), "seasonal_adjustment": info.get("seasonal_adjustment"),
                                  "observations": [{"date": r.get("date"), "value": r.get("value")} for r in observations[:12]],
                                  "role": "Research context; use only if consistent with the stated resolution criteria. Observation dates are not release times. Latest revised data.",
                                  "notes": str(info.get("notes", ""))[:2500]}, sort_keys=True)
            results.append({"url": SERIES_URL + series, "sha256": hashlib.sha256(excerpt.encode()).hexdigest(), "excerpt": excerpt})
        except FredError:
            continue
    return results
