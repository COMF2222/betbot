"""Fit an experimental candidate for future evaluation without touching the active model."""

import argparse
import hashlib
import json
from pathlib import Path

from betbot.config import Settings
from betbot.model import fit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/detailed-history.json"))
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/glicko-candidate/model.json")
    )
    args = parser.parse_args()
    active = Settings().model_path.resolve()
    if args.output.resolve() == active:
        raise ValueError("Use a separate experimental output, not the active model path")
    before = hashlib.sha256(active.read_bytes()).hexdigest() if active.exists() else None
    raw = json.loads(args.input.read_text(encoding="utf-8-sig"))
    report = fit(
        raw.get("rows", []) if isinstance(raw, dict) else raw,
        args.output,
        feature_set="glicko_team",
    )
    assert not report["publication_ready"]
    artifact = json.loads(args.output.read_text(encoding="utf-8"))
    after = hashlib.sha256(active.read_bytes()).hexdigest() if active.exists() else None
    if after != before:
        raise ValueError("The active model changed during candidate preparation")
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "version": artifact["version"],
                "history_cutoff": artifact["history_cutoff"],
                "rows": report["rows"],
                "publication_ready": False,
                "active_model_unchanged": True,
                "note": "For future evaluation only; do not score June-October with this newly fitted artifact",
            }
        )
    )


if __name__ == "__main__":
    main()
