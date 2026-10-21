"""Publish a separately scoped mega-cap snapshot without waiting for the full universe."""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import math
from pathlib import Path

from scripts.quant_screener_pipeline import SCHEMA_VERSION, universe_hash, write_json
from scripts.weekly_screener import ENGINE, QUANTILE_NAMES, configuration_hash, weekly_configuration

SCOPE = "homepage_mega_caps"
COUNT = 6


def select_universe(universe: dict) -> dict:
    rows = [row for row in universe["items"] if not row.get("is_etf")
            and isinstance(row.get("market_cap"), (int, float))
            and math.isfinite(row["market_cap"]) and row["market_cap"] >= 200_000_000_000]
    rows = sorted(rows, key=lambda row: (-row["market_cap"], row["ticker"]))[:COUNT]
    if len(rows) != COUNT or len({row["ticker"] for row in rows}) != COUNT:
        raise ValueError("homepage_mega_cap_universe_incomplete")
    return {**universe, "scope": SCOPE, "items": rows, "universe_hash": universe_hash(rows),
            "counts": {**universe["counts"], "selected": COUNT, "scope": SCOPE}}


def public_snapshot(dataset: dict) -> dict:
    manifest = dataset.get("manifest", {})
    rows = dataset.get("items", [])
    scan_date = str(dataset.get("scan_date", ""))
    config = weekly_configuration()
    if (dataset.get("schema_version") != SCHEMA_VERSION or manifest.get("universe", {}).get("scope") != SCOPE
            or manifest.get("coverage_ok") is not True or manifest.get("successfully_processed") != COUNT
            or manifest.get("forecast_config_hash") != configuration_hash() or len(rows) != COUNT
            or len({r["ticker"] for r in rows}) != COUNT):
        raise ValueError("homepage_publication_incomplete")
    dt.date.fromisoformat(scan_date)
    for row in rows:
        models = {m["id"] for m in row.get("forecast_models", []) if m.get("status") == "completed"}
        forecasts = row.get("forecast_rows", [])
        if (row.get("status") != "success" or row.get("forecast_engine") != ENGINE
                or row.get("forecast_config") != config or models != set(config["models"])
                or row.get("forecast_provenance", {}).get("runtime", {}).get("mock")
                or str(row.get("history_cutoff_at", ""))[:10] != scan_date
                or str(row.get("actual_price_timestamp", ""))[:10] != scan_date
                or row.get("market_cap", 0) < 200_000_000_000 or len(forecasts) != 7):
            raise ValueError("homepage_forecast_invalid")
        for forecast in forecasts:
            values = [forecast.get(q) for q in QUANTILE_NAMES]
            if (str(forecast.get("date", "")) <= scan_date
                    or not all(isinstance(v, (float, int)) and math.isfinite(v) and v > 0 for v in values)
                    or values != sorted(values)):
                raise ValueError("homepage_quantiles_invalid")
    # Distinct identity and feed: this can never masquerade as full-universe coverage.
    data = {**dataset, "scan_id": f"{scan_date}-home-{manifest['universe_hash']}",
            "manifest": {**manifest, "scope": SCOPE}}
    return {"schema_version": "quantura-public-screener-v1", "feed": "stocks-home",
            "published_at": dt.datetime.now(dt.timezone.utc).isoformat(), "data": data}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["select", "publish"])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = json.loads(args.input.read_text())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "select":
        write_json(args.output, select_universe(value))
    else:
        args.output.write_bytes(gzip.compress(json.dumps(public_snapshot(value), allow_nan=False).encode(), mtime=0))


if __name__ == "__main__":
    main()
