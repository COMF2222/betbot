"""Reconstruct two frozen models on a read-only export of a day's published forecasts."""

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from betbot.domain import Draft, Prediction, timestamp
from betbot.model import History, Model
from betbot.timing import separate_timing

MOSCOW = ZoneInfo("Europe/Moscow")


def summarize(rows, model):
    count = len(rows)
    correct = sum(row[model]["correct"] for row in rows)
    signals = [row for row in rows if row[model]["probability"] >= 0.55]
    return {
        "correct": correct,
        "maps": count,
        "accuracy": correct / count if count else None,
        "brier": sum(row[model]["brier"] for row in rows) / count if count else None,
        "signals_correct": sum(row[model]["correct"] for row in signals),
        "signals_count": len(signals),
    }


def reconstruct(model, forecasts, finished):
    history = History(model.raw["history"])
    cutoff = timestamp(model.raw["history_cutoff"])
    pending = []
    for row in finished:
        result, draft = row["result"], Draft.from_dict(row["draft"])
        start, confirmed = (
            timestamp(result.get("start_time")),
            timestamp(result.get("confirmed_at")),
        )
        duration = result.get("duration")
        if not start or not confirmed or not duration or not draft.complete:
            continue
        finish = start + timedelta(seconds=int(duration) + 60)
        available = max(finish, confirmed)
        if finish > cutoff:
            pending.append((available, row["id"], draft, result))
    pending.sort(key=lambda row: (row[0], row[1]))
    cursor, as_of, predictions = 0, cutoff, {}
    applied = set()
    for row in sorted(forecasts, key=lambda row: row["payload"]["draft"]["observed_at"]):
        draft = Draft.from_dict(row["payload"]["draft"])
        while cursor < len(pending) and pending[cursor][0] <= draft.observed_at:
            available, identity, previous, result = pending[cursor]
            assert identity != draft.match_id, "Current outcome must not enter prediction history"
            context = dict(result.get("context") or {})
            if history.glicko_teams is not None:
                context["_result_available_at"] = available.timestamp()
            history.update(previous, result["radiant_win"], context)
            applied.add(identity)
            as_of, cursor = available, cursor + 1
        assert draft.match_id not in applied, "Current outcome must not enter prediction history"
        prediction = model.predict(draft, history=history, history_as_of=as_of.isoformat())
        result = row["result"]
        won = result["radiant_win"]
        assert isinstance(won, bool)
        predictions[draft.match_id] = {
            "favorite": prediction.favorite_name,
            "probability": prediction.favorite_probability,
            "radiant_probability": prediction.radiant_probability,
            "correct": (prediction.favorite == "radiant") == won,
            "brier": (prediction.radiant_probability - int(won)) ** 2,
            "history_as_of": prediction.history_as_of,
        }
    return predictions


def compare(snapshot_path, elo_path, glicko_path, date):
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    start = datetime.combine(datetime.fromisoformat(date).date(), time.min, MOSCOW)
    end = start + timedelta(days=1)
    forecasts = [
        row
        for row in snapshot["forecasts"]
        if row["state"] == "published" and start <= timestamp(row["created_at"]) < end
    ]
    assert forecasts and all(
        isinstance((row["result"] or {}).get("radiant_win"), bool) for row in forecasts
    ), "Day has missing outcomes"
    models = {"elo": Model(elo_path), "glicko": Model(glicko_path)}
    model_hashes = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in (("elo", elo_path), ("glicko", glicko_path))
    }
    assert model_hashes["elo"] == snapshot["model_hashes"]["model.json"]
    assert model_hashes["glicko"] == snapshot["model_hashes"]["model-shadow.json"]
    predictions = {
        name: reconstruct(model, forecasts, snapshot["finished"]) for name, model in models.items()
    }
    rows, saved_errors, shadow_errors = [], [], []
    for row in forecasts:
        p = Prediction.from_dict(row["payload"])
        winner = p.draft.radiant_name if row["result"]["radiant_win"] else p.draft.dire_name
        value = {
            "match_id": row["match_id"],
            "time_moscow": timestamp(row["created_at"]).astimezone(MOSCOW).isoformat(),
            "league": p.league_name.strip(),
            "league_id": p.draft.league_id,
            "radiant": p.draft.radiant_name.strip(),
            "dire": p.draft.dire_name.strip(),
            "map": p.draft.map_number,
            "winner": winner.strip(),
            "separate_timing": separate_timing(p),
            "published_model": p.model_version,
            "published_correct": (p.favorite == "radiant") == row["result"]["radiant_win"],
            **{name: predictions[name][row["match_id"]] for name in models},
        }
        for name, model in models.items():
            if p.model_version == model.raw["version"]:
                saved_errors.append(abs(value[name]["radiant_probability"] - p.radiant_probability))
        shadow = snapshot["shadows"].get(row["match_id"])
        if shadow:
            assert shadow["draft_observed_at"] == p.draft.observed_at.isoformat()
            assert timestamp(shadow["history_as_of"]) <= p.draft.observed_at
            assert timestamp(shadow["recorded_at"]) < timestamp(row["result"]["confirmed_at"])
            assert shadow["model_version"] == models["glicko"].raw["version"]
            shadow_errors.append(
                abs(value["glicko"]["radiant_probability"] - shadow["radiant_probability"])
            )
        rows.append(value)
    assert saved_errors and max(saved_errors) < 1e-10, (
        "Reconstruction differs from published probabilities"
    )
    assert shadow_errors and max(shadow_errors) < 1e-10, (
        "Reconstruction differs from prospective Glicko predictions"
    )
    by_league = {}
    for league in sorted({row["league"] for row in rows}):
        group = [row for row in rows if row["league"] == league]
        by_league[league] = {name: summarize(group, name) for name in models}
    outcomes = defaultdict(int)
    for row in rows:
        outcomes[f"elo_{row['elo']['correct']}_glicko_{row['glicko']['correct']}"] += 1
    return {
        "date": date,
        "timezone": "Europe/Moscow",
        "since": start.isoformat(),
        "until_exclusive": end.isoformat(),
        "sample": "All unique published forecasts, selected by created_at, including weak and delayed analyses",
        "model_versions": {name: model.raw["version"] for name, model in models.items()},
        "model_hashes": model_hashes,
        "history_cutoffs": {name: model.raw["history_cutoff"] for name, model in models.items()},
        "actual": {"correct": sum(row["published_correct"] for row in rows), "maps": len(rows)},
        "summary": {name: summarize(rows, name) for name in models},
        "by_league": by_league,
        "timing": {
            "early": sum(not row["separate_timing"] for row in rows),
            "separate": sum(row["separate_timing"] for row in rows),
        },
        "paired_outcomes": dict(outcomes),
        "verification": {
            "published_predictions_checked": len(saved_errors),
            "max_published_difference": max(saved_errors),
            "prospective_shadow_predictions_checked": len(shadow_errors),
            "max_shadow_difference": max(shadow_errors),
        },
        "maps": rows,
    }


def markdown(report):
    moment = datetime.fromisoformat(report["date"])
    actual = report["actual"]
    rows = [
        f"# Elo и Glicko: {report['date']}",
        "",
        f"Период: **{moment:%d.%m.%Y}, 00:00–23:59 МСК**. Включены все {actual['maps']} уникальных опубликованных прогнозов этого дня, включая слабые сигналы и разборы с задержкой. Дата определяется временем создания прогноза, как в ежедневном отчёте бота.",
        "",
        f"Фактически опубликовано: **{actual['correct']} попаданий из {actual['maps']}**. Это {actual['correct']} попаданий и {actual['maps'] - actual['correct']} промахов. Модель публикаций менялась в течение дня; пересчёт ниже показывает результат каждой модели на всех одинаковых картах.",
        "",
        "| Модель весь день | Попадания | Точность | Brier, меньше лучше | Сигналы ≥55% |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, label in (("elo", "Модель с Elo"), ("glicko", "Модель с Glicko")):
        value = report["summary"][name]
        rows.append(
            f"| {label} | {value['correct']}/{value['maps']} | {value['accuracy']:.1%} | {value['brier']:.6f} | {value['signals_correct']}/{value['signals_count']} |"
        )
    rows += ["", "## По турнирам", "", "| Турнир | Elo | Glicko |", "| --- | ---: | ---: |"]
    for league, values in report["by_league"].items():
        rows.append(
            f"| {league} | {values['elo']['correct']}/{values['elo']['maps']} | {values['glicko']['correct']}/{values['glicko']['maps']} |"
        )
    rows += [
        "",
        "## Все карты",
        "",
        "Время публикации московское. Галочка означает, что выбранная модель назвала победившую команду. Проценты — пересчитанные вероятности, без округления при выборе фаворита.",
        "",
        "| Время | Турнир | Карта | Победитель | Elo | Glicko |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["maps"]:
        label = f"{row['radiant']} — {row['dire']} · #{row['map']}"
        answers = [
            f"{'✅' if row[name]['correct'] else '❌'} {row[name]['favorite']} {row[name]['probability']:.1%}"
            for name in ("elo", "glicko")
        ]
        rows.append(
            f"| {row['time_moscow'][11:16]} | {row['league']} | {label} | {row['winner']} | {answers[0]} | {answers[1]} |"
        )
    verify = report["verification"]
    rows += [
        "",
        "## Проверка расчёта",
        "",
        f"Использованы замороженные модели `{report['model_versions']['elo']}` и `{report['model_versions']['glicko']}`. Обе обучены до {report['history_cutoffs']['elo']}; SHA-256 совпадают с файлами на сервере.",
        "",
        "Для каждой карты восстановлена отдельная история каждой модели. Результат прошлой карты включается только после окончания с буфером 60 секунд и подтверждения источником; доступны только результаты до времени исходного драфта. Результат оцениваемой карты не участвует в её прогнозе. Модели не обучались заново и не подбирались по результатам этого дня.",
        "",
        f"Пересчитанные вероятности совпали с {verify['published_predictions_checked']} опубликованными прогнозами: максимальная разница {verify['max_published_difference']:.3g}. Для {verify['prospective_shadow_predictions_checked']} вечерних карт Glicko также совпал с параллельными прогнозами, записанными до подтверждения результата; максимальная разница {verify['max_shadow_difference']:.3g}.",
        "",
        f"Все {report['timing']['separate']} карт относятся к отдельному учёту по времени источника; ранних прогнозов {report['timing']['early']}. Это короткий ретроспективный срез наблюдаемых турниров, не оценка будущей точности или доходности. Карты в одной серии зависимы.",
        "",
        "В колонке сигналов показан результат только при перевесе ≥55%; число карт у моделей отличается, поэтому основное сравнение — на всех одинаковых картах.",
    ]
    return "\n".join(rows) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--elo", type=Path, required=True)
    parser.add_argument("--glicko", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.snapshot, args.elo, args.glicko, args.date)
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    args.output_prefix.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    args.output_prefix.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    with args.output_prefix.with_suffix(".csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "match_id",
                "time_moscow",
                "tournament",
                "radiant",
                "dire",
                "map",
                "winner",
                "elo_favorite",
                "elo_probability",
                "elo_correct",
                "glicko_favorite",
                "glicko_probability",
                "glicko_correct",
            ]
        )
        for row in report["maps"]:
            writer.writerow(
                [
                    row["match_id"],
                    row["time_moscow"],
                    row["league"],
                    row["radiant"],
                    row["dire"],
                    row["map"],
                    row["winner"],
                    *[
                        row[name][key]
                        for name in ("elo", "glicko")
                        for key in ("favorite", "probability", "correct")
                    ],
                ]
            )
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "summary",
                    "actual",
                    "by_league",
                    "paired_outcomes",
                    "verification",
                    "timing",
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
