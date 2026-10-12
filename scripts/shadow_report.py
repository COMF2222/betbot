"""Read-only comparison of predictions recorded prospectively by the running bot."""

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from betbot.domain import timestamp


def scores(rows, probability):
    n = len(rows)
    wins = sum((r[probability] >= 0.5) == r["radiant_win"] for r in rows)
    if not n:
        return {"wins": 0, "maps": 0, "accuracy": None, "brier": None, "accuracy_interval_95": None}
    rate, z = wins / n, 1.959963984540054
    center = (rate + z * z / (2 * n)) / (1 + z * z / n)
    radius = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return {
        "wins": wins,
        "maps": n,
        "accuracy": rate,
        "brier": sum((r[probability] - int(r["radiant_win"])) ** 2 for r in rows) / n,
        "accuracy_interval_95": [center - radius, center + radius],
    }


def compare(database, cohort_limit=100):
    groups, exclusions = defaultdict(list), defaultdict(int)
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        shadows = {
            key.split(":", 1)[1]: json.loads(value)
            for key, value in db.execute(
                "SELECT key,value FROM settings WHERE key LIKE 'shadow_forecast:%'"
            )
        }
        forecasts = db.execute(
            "SELECT f.match_id,f.payload,m.result FROM forecasts f JOIN matches m ON m.id=f.match_id ORDER BY f.id"
        ).fetchall()
    for identity, payload, result in forecasts:
        shadow = shadows.get(identity)
        if not shadow:
            continue
        active = json.loads(payload)
        if shadow["compared_to"] != active["model_version"]:
            exclusions["active_version_changed"] += 1
            continue
        if shadow["draft_observed_at"] != active["draft"]["observed_at"]:
            exclusions["different_draft_timestamp"] += 1
            continue
        recorded, observed = (
            timestamp(shadow["recorded_at"]),
            timestamp(shadow["draft_observed_at"]),
        )
        as_of = timestamp(shadow["history_as_of"])
        if not recorded or not observed or not as_of or as_of > observed or recorded < observed:
            exclusions["invalid_history_timestamp"] += 1
            continue
        actual = json.loads(result) if result else {}
        confirmed, start = (
            timestamp(actual.get("confirmed_at")),
            timestamp(actual.get("start_time")),
        )
        end = (
            start + timedelta(seconds=int(actual["duration"]))
            if start and actual.get("duration")
            else None
        )
        if isinstance(actual.get("radiant_win"), bool):
            if not confirmed or not end or recorded >= end or recorded >= confirmed:
                exclusions["outcome_already_available_or_unverified_time"] += 1
                continue
        group = (active["model_version"], shadow["model_version"])
        groups[group].append(
            {
                "match_id": identity,
                "league_id": active["draft"]["league_id"],
                "series_id": active["draft"].get("series_id"),
                "active": active["radiant_probability"],
                "shadow": shadow["radiant_probability"],
                "threshold": shadow["threshold"],
                "radiant_win": actual.get("radiant_win"),
                "recorded_at": shadow["recorded_at"],
            }
        )
    output = []
    for (active, shadow), all_rows in groups.items():
        cohort = sorted(all_rows, key=lambda r: r["recorded_at"])[:cohort_limit]
        settled = [r for r in cohort if isinstance(r["radiant_win"], bool)]
        by_league = {}
        for league in sorted({r["league_id"] for r in settled}):
            rows = [r for r in settled if r["league_id"] == league]
            by_league[str(league)] = {
                "active": scores(rows, "active"),
                "shadow": scores(rows, "shadow"),
            }
        active_signals = [r for r in settled if max(r["active"], 1 - r["active"]) >= r["threshold"]]
        shadow_signals = [r for r in settled if max(r["shadow"], 1 - r["shadow"]) >= r["threshold"]]
        output.append(
            {
                "active_model": active,
                "shadow_model": shadow,
                "cohort_limit": cohort_limit,
                "recorded": len(all_rows),
                "cohort_recorded": len(cohort),
                "pending": len(cohort) - len(settled),
                "active": scores(settled, "active"),
                "shadow": scores(settled, "shadow"),
                "active_signals": scores(active_signals, "active"),
                "shadow_signals": scores(shadow_signals, "shadow"),
                "disagreements": sum((r["active"] >= 0.5) != (r["shadow"] >= 0.5) for r in settled),
                "by_league": by_league,
                "maps": cohort,
                "limit_reached": len(cohort) == cohort_limit and len(settled) == cohort_limit,
                "automatic_promotion": False,
                "interval_note": "Wilson intervals describe individual hit rates; maps within a series are correlated.",
            }
        )
    return {"comparisons": output, "exclusions": dict(exclusions)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/betbot.sqlite3"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cohort-limit", type=int, default=100)
    args = parser.parse_args()
    if args.cohort_limit <= 0:
        parser.error("--cohort-limit must be positive")
    result = compare(args.database, args.cohort_limit)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
