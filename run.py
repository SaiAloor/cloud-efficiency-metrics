"""Run the whole pipeline: data checks, metrics, opportunities, anomalies, report.

    python run.py              # use the files in data/ (simulates them if missing)
    python run.py --simulate   # regenerate the simulated data first

Exit code 1 means a data check failed and no results were written.
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from efficiency import anomalies, metrics, opportunities, report
from efficiency.load import DATA, load
from efficiency.simulate import simulate


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--simulate", action="store_true", help="regenerate the simulated data")
    args = ap.parse_args(argv)
    if args.simulate or not (DATA / "billing_focus.csv.gz").exists():
        simulate()

    ds = load()
    for c in ds.checks:
        print(f"[{'ok' if c.passed else c.level.upper()}] {c.name}: {c.detail}")
    if ds.failed:
        print("A data check failed, so no results were written.", file=sys.stderr)
        return 1

    opps = opportunities.find(ds)
    summary = opportunities.summarize(opps)
    planted = pd.read_csv(DATA / "planted_issues.csv") if (DATA / "planted_issues.csv").exists() else None
    score = opportunities.score_against_planted(opps, planted) if planted is not None else pd.DataFrame()
    t = metrics.tree(ds, opps)
    report.build(
        ds, t["company"], t["platforms"], t["teams"], t["platform_units"], summary, opps, score,
        metrics.monthly(ds), metrics.ai_productivity(ds),
        raw_flags=anomalies.detect(ds, normalize=False), flags=anomalies.detect(ds),
        daily=anomalies.daily(ds), matrix=metrics.team_by_platform(ds),
    )
    c = t["company"]
    print(f"{c['window']}: cost ${c['cost']:,.0f}, efficiency {c['efficiency']:.1%} "
          f"(target {c['target_efficiency']:.1%}), expected savings ${c['expected_savings']:,.0f} a month")
    return 0


if __name__ == "__main__":
    sys.exit(main())
