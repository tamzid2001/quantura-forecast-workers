"""Reproduce the observed-cohort study without model downloads or broker access."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .data import load_inputs
from .study import analyze, markdown


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--draws", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20261027)
    args = parser.parse_args(argv)
    if args.inputs.resolve() == args.output.resolve():
        parser.error("input and output directories must differ")
    trades, sources = load_inputs(args.inputs)
    report = analyze(trades, sources, args.draws, args.seed)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "research.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    (args.output / "REPORT.md").write_text(markdown(report))
    print(json.dumps({"schema": report["schema"], "configuration_sha256": report["configuration_sha256"],
                      "cohorts": {k: {"settled": v["baseline"]["settled"],
                                      "net_pnl_usd": v["baseline"]["net_pnl_usd"],
                                      "training_selected_filter": v["training_selected_filter"]}
                                  for k, v in report["cohorts"].items()}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
