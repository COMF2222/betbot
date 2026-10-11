import argparse
from pathlib import Path

from betbot.experiments import run_experiments


def main():
    parser = argparse.ArgumentParser(description="Offline model experiments; no live model changes")
    parser.add_argument("--input", type=Path, default=Path("data/detailed-history.json"))
    parser.add_argument("--baseline", type=Path, default=Path("artifacts/draft-comparison/classic"))
    parser.add_argument(
        "--plan", type=Path, default=Path("config/model-experiments-2026-10-11.json")
    )
    parser.add_argument("--output", type=Path, default=Path("artifacts/model-research-2026-10-11"))
    args = parser.parse_args()
    run_experiments(
        args.input,
        args.baseline,
        args.plan,
        args.output,
        progress=lambda text: print(text, flush=True),
    )


if __name__ == "__main__":
    main()
