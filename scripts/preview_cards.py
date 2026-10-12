"""Render reviewable PNGs without publishing or changing the forecast ledger."""

import argparse
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image

from betbot.demo import demo_prediction
from betbot.domain import Prediction
from betbot.graphics import forecast_image
from betbot.presentation import MOSCOW
from betbot.reports import demo_report, public_report_payload, render_report, totals
from betbot.storage import Store


class ReadOnlyLedger:
    get = Store.get
    published_between = Store.published_between
    quote_snapshots = Store.quote_snapshots

    def __init__(self, path):
        self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--db", type=Path, default=Path("data/betbot.sqlite3"))
    parser.add_argument("--assets", type=Path, default=Path("data/heroes"))
    parser.add_argument("--heroes", type=Path, default=Path("data/heroes.json"))
    parser.add_argument(
        "--date", default=(datetime.now(MOSCOW) - timedelta(days=1)).date().isoformat()
    )
    parser.add_argument("--output", type=Path, default=Path("artifacts/cards-v3"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    heroes = (
        {int(k): v for k, v in json.loads(args.heroes.read_text(encoding="utf-8")).items()}
        if args.heroes.exists()
        else {}
    )
    p = (
        Prediction.from_dict(json.loads(args.input.read_text(encoding="utf-8")))
        if args.input
        else demo_prediction()
    )
    (args.output / "forecast.png").write_bytes(
        forecast_image(p, heroes, args.assets, demo=not args.input)
    )
    report = demo_report()
    if args.report:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    elif args.db.exists():
        ledger = ReadOnlyLedger(args.db)
        try:
            start = datetime.fromisoformat(args.date).replace(tzinfo=MOSCOW)
            report = public_report_payload(
                ledger,
                start.astimezone(UTC).isoformat(),
                (start + timedelta(days=1)).astimezone(UTC).isoformat(),
            )
        finally:
            ledger.db.close()
    (args.output / "report.png").write_bytes(render_report(report))
    stress = replace(
        p,
        radiant_probability=0.995,
        draft_probability=0.5,
        draft=replace(
            p.draft,
            radiant_name="Очень длинное название киберспортивной команды",
            dire_name="Ещё одна команда с длинным названием",
        ),
    )
    (args.output / "long-names.png").write_bytes(
        forecast_image(stress, heroes, args.assets, demo=True)
    )
    empty = {**demo_report(), **totals([]), "entries": [], "cumulative": totals([])}
    (args.output / "empty-report.png").write_bytes(render_report(empty))
    for name in ("forecast", "report"):
        with Image.open(args.output / f"{name}.png") as image:
            image.resize(
                (390, round(image.height * 390 / image.width)), Image.Resampling.LANCZOS
            ).save(args.output / f"{name}-mobile.png")
    print(f"Карточки сохранены: {args.output.resolve()}")


if __name__ == "__main__":
    main()
