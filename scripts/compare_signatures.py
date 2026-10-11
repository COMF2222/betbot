"""Compare signatures with the saved four-month Tier 1 baseline, without promotion.

Run from the repository: uv run python scripts/compare_signatures.py
Requires the existing detailed history and artifacts/draft-comparison/classic.
"""

import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

from betbot.backtest import evaluate, markdown_report
from betbot.model import Model, fit, historical_draft


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "artifacts/signature-comparison"
    output.mkdir(parents=True, exist_ok=True)
    base = root / "artifacts/draft-comparison/classic"
    baseline = json.loads((base / "report.json").read_text(encoding="utf-8"))
    validation = json.loads((base / "model-report.json").read_text(encoding="utf-8"))
    raw = json.loads((root / "data/detailed-history.json").read_text(encoding="utf-8"))
    rows = raw["rows"] if isinstance(raw, dict) else raw
    rows = sorted(
        {str(r["match_id"]): r for r in rows if historical_draft(r)}.values(),
        key=lambda r: (int(r["start_time"]), int(r["match_id"])),
    )
    cutoff = int(datetime.fromisoformat(baseline["requested_start"]).timestamp())
    end = int(datetime.fromisoformat(baseline["requested_end"]).timestamp())

    def series(row):
        return f"series:{row['series_id']}" if row.get("series_id") else f"match:{row['match_id']}"

    partitions = defaultdict(set)
    for row in rows:
        partitions[series(row)].add(int(row["start_time"]) >= cutoff)
    before, period = [], []
    for row in rows:
        if len(partitions[series(row)]) > 1:
            continue
        finish = int(row["start_time"]) + int(row["duration"]) + 60
        if finish < cutoff:
            before.append(row)
        elif cutoff <= int(row["start_time"]) < end and finish <= end:
            period.append(row)
    assert len(before) == baseline["history_maps_before_period"]
    print(json.dumps({"prior_maps": len(before), "period_maps": len(period)}), flush=True)
    # The model, features, and hyperparameters are fixed before scoring this period.
    validation_new = fit(before, output / "frozen-model.json", "signatures")
    print(
        json.dumps(
            {
                "prior_validation": {
                    k: validation_new[k] for k in ("accuracy", "brier", "publication_ready")
                }
            }
        ),
        flush=True,
    )
    report = evaluate(
        Model(output / "frozen-model.json"), period, output, scope=baseline["evaluation_scope"]
    )
    report.update(
        {
            k: baseline[k]
            for k in ("months", "requested_start", "requested_end", "history_maps_before_period")
        }
    )
    (output / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output / "report.md").write_text(markdown_report(report), encoding="utf-8")

    def results(path):
        with path.open(encoding="utf-8-sig") as stream:
            return {row["match_id"]: row for row in csv.DictReader(stream)}

    original, updated = results(base / "results.csv"), results(output / "results.csv")
    assert set(original) == set(updated)
    blocks = defaultdict(list)
    for identity, row in updated.items():
        old = original[identity]
        actual = int(row["radiant_win"] == "True")
        p, previous = float(row["radiant_probability"]), float(old["radiant_probability"])
        group = row["series_id"] if row["series_id"] not in ("", "0") else identity
        blocks[group].append(
            (
                (p - actual) ** 2 - (previous - actual) ** 2,
                int((p >= 0.5) == actual) - int((previous >= 0.5) == actual),
            )
        )
    samples = [np.asarray(block) for block in blocks.values()]
    rng = np.random.default_rng(42)
    draws = [
        np.concatenate([samples[i] for i in rng.integers(0, len(samples), len(samples))]).mean(
            axis=0
        )
        for _ in range(3000)
    ]
    interval = np.quantile(draws, [0.025, 0.975], axis=0)
    evidence = bool(
        validation_new["publication_ready"]
        and validation_new["brier"] < validation["brier"]
        and report["all_maps"]["accuracy"] > baseline["all_maps"]["accuracy"]
        and interval[1, 0] < 0
        and interval[0, 1] >= 0
    )
    comparison = {
        "baseline_tier1": baseline["all_maps"],
        "candidate_tier1": report["all_maps"],
        "baseline_validation": {k: validation[k] for k in ("accuracy", "brier")},
        "candidate_validation": {k: validation_new[k] for k in ("accuracy", "brier")},
        "series_blocks": len(blocks),
        "brier_difference_95_interval": interval[:, 0].tolist(),
        "accuracy_difference_95_interval": interval[:, 1].tolist(),
        "improvement_supported": evidence,
        # This period has already been examined. A fresh prospective test is required.
        "promotion_recommended": False,
        "promoted": False,
    }
    (output / "comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    print(json.dumps(comparison), flush=True)


if __name__ == "__main__":
    main()
