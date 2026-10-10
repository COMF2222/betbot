"""Compare predeclared feature sets on identical maps and temporal boundaries."""

import argparse
import json
from pathlib import Path

from betbot.backtest import run_backtest
from betbot.domain import timestamp

parser = argparse.ArgumentParser()
parser.add_argument(
    "--features", nargs="+", choices=("classic", "detailed"), default=("classic", "detailed")
)
args = parser.parse_args()
rows = json.loads(Path("data/detailed-history.json").read_text(encoding="utf-8"))["rows"]
reference = json.loads(Path("artifacts/backtest-tier1/report.json").read_text(encoding="utf-8"))
scope = json.loads(Path("config/tier1-2026.json").read_text(encoding="utf-8"))
names = {str(e["league_id"]): e["name"] for e in scope["events"]}
reports = {}
for feature_set in args.features:
    output = Path("artifacts/draft-comparison") / feature_set
    print(f"Starting {feature_set}", flush=True)
    report = run_backtest(
        rows,
        output,
        4,
        True,
        names,
        0.55,
        timestamp(reference["requested_end"]),
        scope,
        feature_set,
    )
    reports[feature_set] = report
    print(
        json.dumps(
            {
                "features": feature_set,
                "all_maps": report["all_maps"],
                "signals": report["signals"],
                "coverage": report["signal_coverage"],
                "roles": report["mean_role_coverage"],
            }
        ),
        flush=True,
    )
for feature_set in ("classic", "detailed"):
    path = Path("artifacts/draft-comparison") / feature_set / "report.json"
    if path.exists():
        reports[feature_set] = json.loads(path.read_text(encoding="utf-8"))
Path("artifacts/draft-comparison/comparison.json").write_text(
    json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
)
