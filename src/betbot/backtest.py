import calendar
import csv
import hashlib
import heapq
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

from .domain import timestamp, utcnow
from .model import History, Model, fit, historical_draft
from .presentation import MOSCOW


def months_before(value: datetime, months: int) -> datetime:
    if not 1 <= months <= 12:
        raise ValueError("Период проверки должен быть от 1 до 12 месяцев")
    index = value.year * 12 + value.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    return value.replace(
        year=year,
        month=month,
        day=min(value.day, calendar.monthrange(year, month)[1]),
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def metrics(rows: list[dict]) -> dict:
    if not rows:
        return {
            "count": 0,
            "wins": 0,
            "accuracy": None,
            "brier": None,
            "elo_accuracy": None,
            "elo_brier": None,
        }
    y = np.asarray([int(r["radiant_win"]) for r in rows])
    p = np.asarray([r["radiant_probability"] for r in rows])
    baseline = np.asarray([r["elo_probability"] for r in rows])
    return {
        "count": len(rows),
        "wins": int(((p >= 0.5) == y).sum()),
        "accuracy": float(((p >= 0.5) == y).mean()),
        "brier": float(((p - y) ** 2).mean()),
        "elo_accuracy": float(((baseline >= 0.5) == y).mean()),
        "elo_brier": float(((baseline - y) ** 2).mean()),
    }


def score_event(row: dict, scope: dict | None) -> bool:
    if not scope:
        return True
    day = timestamp(int(row["start_time"])).astimezone(MOSCOW).date().isoformat()
    return any(
        int(event["league_id"]) == int(row["leagueid"])
        and event["start_date"] <= day <= event["end_date"]
        for event in scope.get("events", [])
    )


def evaluate(
    model: Model,
    test_rows: list[dict],
    output: Path,
    online_history: bool = True,
    leagues: dict | None = None,
    threshold: float = 0.55,
    scope: dict | None = None,
) -> dict:
    """The predictor receives Draft only. Current outcome is read after its prediction.

    Online mode learns only ratings from already finished earlier games; model weights
    and calibration stay frozen. No current winner, duration or score is in Draft.
    """
    output.mkdir(parents=True, exist_ok=True)
    rows = sorted(test_rows, key=lambda r: (int(r["start_time"]), int(r["match_id"])))
    history = History(model.raw["history"])
    weights_hash = hashlib.sha256(
        json.dumps(model.raw["models"], sort_keys=True).encode()
    ).hexdigest()
    completions = []
    results = []
    history_as_of = model.raw["history_cutoff"]
    with (
        (output / "blind-inputs.jsonl").open("w", encoding="utf-8") as inputs_file,
        (output / "predictions.jsonl").open("w", encoding="utf-8") as predictions_file,
        (output / "outcomes.jsonl").open("w", encoding="utf-8") as outcomes_file,
        (output / "analyses.jsonl").open("w", encoding="utf-8") as analyses_file,
    ):
        for i, row in enumerate(rows):
            draft = historical_draft(row)
            if not draft:
                continue
            if online_history:
                while completions and completions[0][0] < int(row["start_time"]):
                    available, completed_i = heapq.heappop(completions)
                    previous = rows[completed_i]
                    history.update(historical_draft(previous), previous["radiant_win"], previous)
                    history_as_of = timestamp(available).isoformat()
            if not score_event(row, scope):
                # Unscored earlier games can still provide causal rating history.
                if online_history:
                    heapq.heappush(
                        completions, (int(row["start_time"]) + int(row["duration"]) + 60, i)
                    )
                continue
            # Only state from games already finished at this observation time is supplied.
            baseline_features = history.features(draft)
            blind_input = draft.to_dict()
            assert "radiant_win" not in blind_input and "duration" not in blind_input
            inputs_file.write(json.dumps(blind_input, ensure_ascii=False) + "\n")
            prediction = model.predict(draft, history=history, history_as_of=history_as_of)
            analyses_file.write(
                json.dumps(
                    {
                        "match_id": draft.match_id,
                        "history_as_of": history_as_of,
                        "analysis": prediction.analysis,
                        "features": prediction.features,
                        "reasons": prediction.reasons,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            prediction_row = {
                "match_id": draft.match_id,
                "league_id": draft.league_id,
                "series_id": draft.series_id,
                "observed_at": draft.observed_at.isoformat(),
                "radiant_team_id": draft.radiant_id,
                "dire_team_id": draft.dire_id,
                "radiant_probability": prediction.radiant_probability,
                "draft_probability": prediction.draft_probability,
                "favorite_probability": prediction.favorite_probability,
                "favorite": prediction.favorite,
                "model_version": prediction.model_version,
                "role_coverage": prediction.analysis.get("role_coverage", 0),
                "elo_probability": 1 / (1 + 10 ** (-baseline_features.get("team_elo", 0))),
                "signal": prediction.favorite_probability >= threshold,
                "history_mode": "previous_finished_games"
                if online_history
                else "frozen_before_test",
            }
            # Persist the forecast independently before opening the current target.
            predictions_file.write(json.dumps(prediction_row, ensure_ascii=False) + "\n")
            predictions_file.flush()
            actual = bool(row["radiant_win"])
            outcomes_file.write(
                json.dumps(
                    {
                        "match_id": draft.match_id,
                        "radiant_win": actual,
                        "duration": int(row["duration"]),
                    }
                )
                + "\n"
            )
            result = {
                **prediction_row,
                "radiant_win": actual,
                "correct": (prediction.favorite == "radiant") == actual,
            }
            results.append(result)
            if online_history:
                heapq.heappush(completions, (int(row["start_time"]) + int(row["duration"]) + 60, i))
    assert (
        weights_hash
        == hashlib.sha256(json.dumps(model.raw["models"], sort_keys=True).encode()).hexdigest()
    )
    by_league = defaultdict(list)
    for row in results:
        by_league[str(row["league_id"])].append(row)
    league_metrics = []
    for league, selected in by_league.items():
        league_metrics.append(
            {
                "league_id": int(league),
                "name": (leagues or {}).get(league, f"Турнир #{league}"),
                **metrics(selected),
            }
        )
    confidence = []
    for lower, upper in (
        (0.5, 0.55),
        (0.55, 0.6),
        (0.6, 0.65),
        (0.65, 0.7),
        (0.7, 0.8),
        (0.8, 1.01),
    ):
        selected = [r for r in results if lower <= r["favorite_probability"] < upper]
        confidence.append({"from": lower, "to": min(1, upper), **metrics(selected)})
    signals = [r for r in results if r["signal"]]
    report = {
        "evaluated_at": utcnow().isoformat(),
        "model_version": model.raw["version"],
        "feature_set": model.raw.get("feature_set", "classic"),
        "model_training_history_cutoff": model.raw["history_cutoff"],
        "frozen_weights_sha256": weights_hash,
        "history_mode": "previous_finished_games" if online_history else "frozen_before_test",
        "test_first_start": results[0]["observed_at"] if results else None,
        "test_last_start": results[-1]["observed_at"] if results else None,
        "all_maps": metrics(results),
        "signal_threshold": threshold,
        "signals": metrics(signals),
        "signal_coverage": len(signals) / len(results) if results else 0,
        "by_confidence": confidence,
        "by_league": sorted(league_metrics, key=lambda r: r["count"], reverse=True),
        "limitations": [
            "Ретроспективная проверка; часть периода пересекается с ранее изученной выборкой разработки",
            "Исход текущей карты не передаётся модели; веса фиксированы до периода теста",
            "В online-режиме обновляются рейтинги только после завершения предыдущих карт",
            "История теста охватывает предыдущие карты всех доступных лиг; live-бот обновляет наблюдаемые карты выбранных турниров и получает остальную историю при переобучении",
            "Данные покрывают доступные OpenDota карты, а не гарантированно все карты турниров",
            "Доходность по коэффициентам не измерялась",
            "Пороги уверенности показаны для анализа, не подобраны по этому тесту",
        ],
        "evaluation_scope": scope,
        "mean_role_coverage": sum(r["role_coverage"] for r in results) / len(results)
        if results
        else 0,
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (output / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        if results:
            writer = csv.DictWriter(handle, fieldnames=list(results[0]))
            writer.writeheader()
            writer.writerows(results)
    return report


def run_backtest(
    rows: list[dict],
    output: Path,
    months: int = 4,
    online_history: bool = True,
    leagues: dict | None = None,
    threshold: float = 0.55,
    end: datetime | None = None,
    scope: dict | None = None,
    feature_set: str = "classic",
) -> dict:
    now = end or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=MOSCOW)
    start = months_before(now.astimezone(MOSCOW), months)
    unique = {str(r.get("match_id")): r for r in rows if historical_draft(r)}
    rows = sorted(unique.values(), key=lambda r: (int(r["start_time"]), int(r["match_id"])))
    before, test = [], []
    cutoff = int(start.timestamp())
    end_timestamp = int(now.timestamp())
    # A series spanning the boundary belongs to neither side of the comparison.
    series_sides = defaultdict(set)
    for row in rows:
        key = f"series:{row['series_id']}" if row.get("series_id") else f"match:{row['match_id']}"
        series_sides[key].add(int(row["start_time"]) >= cutoff)
    for row in rows:
        key = f"series:{row['series_id']}" if row.get("series_id") else f"match:{row['match_id']}"
        if len(series_sides[key]) > 1:
            continue
        finish = int(row["start_time"]) + int(row["duration"]) + 60
        if finish < cutoff:
            before.append(row)
        elif cutoff <= int(row["start_time"]) < end_timestamp and finish <= end_timestamp:
            test.append(row)
    if not test:
        raise ValueError("Нет полных карт за выбранный период")
    if not before or int(before[0]["start_time"]) >= cutoff:
        raise ValueError("Нужна история до начала тестового периода: увеличьте --limit")
    output.mkdir(parents=True, exist_ok=True)
    (output / "dataset.json").write_text(
        json.dumps({"source": "OpenDota", "rows": rows}, ensure_ascii=False), encoding="utf-8"
    )
    # This artifact contains no test-period labels. It is distinct from the deployment model.
    model_path = output / "frozen-model.json"
    fit(before, model_path, feature_set)
    report = evaluate(Model(model_path), test, output, online_history, leagues, threshold, scope)
    if not report["all_maps"]["count"]:
        raise ValueError("В выбранном списке турниров нет полных карт за этот период")
    report.update(
        {
            "months": months,
            "requested_start": start.isoformat(),
            "requested_end": now.astimezone(MOSCOW).isoformat(),
            "history_maps_before_period": len(before),
        }
    )
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "report.md").write_text(markdown_report(report), encoding="utf-8")
    return report


def markdown_report(report: dict) -> str:
    all_maps, signals = report["all_maps"], report["signals"]
    start = timestamp(report["requested_start"]).astimezone(MOSCOW)
    end = timestamp(report["requested_end"]).astimezone(MOSCOW)

    def accuracy(value):
        return f"{value:.1%}" if value is not None else "—"

    lines = [
        "# Историческая проверка Betbot",
        "",
        f"Период: {start:%d.%m.%Y}–{end:%d.%m.%Y}, {report['months']} календарных месяца, МСК.",
        "",
        f"До начала периода: {report['history_maps_before_period']} карт. Проверено: {all_maps['count']} карт из {len(report['by_league'])} турниров/лиг.",
        "",
        "| Срез | Карт | Верных | Точность |",
        "| --- | ---: | ---: | ---: |",
        f"| Все карты | {all_maps['count']} | {all_maps['wins']} | {accuracy(all_maps['accuracy'])} |",
        f"| Сигнал ≥ {report['signal_threshold']:.0%} | {signals['count']} | {signals['wins']} | {accuracy(signals['accuracy'])} |",
        "",
        f"Покрытие сигналами: {report['signal_coverage']:.1%}. Brier модели: {all_maps['brier']:.4f}; базового Elo: {all_maps['elo_brier']:.4f}. Точность Elo: {accuracy(all_maps['elo_accuracy'])}.",
        "",
        "## Точность и объём по уверенности",
        "",
        "| Вероятность фаворита | Карт | Верных | Точность |",
        "| --- | ---: | ---: | ---: |",
    ]
    scope = report.get("evaluation_scope")
    if scope:
        lines[0] = "# Историческая проверка Betbot · Tier 1"
        lines[4:4] = [
            f"Классификация: {scope['classification']}. Источник: [Liquipedia]({scope['source']}).",
            "",
            "В итоговом проценте только основные события выбранных турниров. Предыдущие завершённые игры других лиг могут обновлять рейтинги, но не входят в этот процент.",
            "",
        ]
    for row in report["by_confidence"]:
        lines.append(
            f"| {row['from']:.0%}–{row['to']:.0%} | {row['count']} | {row['wins']} | {accuracy(row['accuracy'])} |"
        )
    lines.extend(
        [
            "",
            "## По турнирам и лигам",
            "",
            "| Турнир | ID | Карт | Верных | Точность |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in report["by_league"]:
        name = row["name"].replace("|", "/").replace("\n", " ")
        lines.append(
            f"| {name} | {row['league_id']} | {row['count']} | {row['wins']} | {accuracy(row['accuracy'])} |"
        )
    if scope:
        lines.extend(["", "## Состав выборки", ""])
        lines.extend(
            f"- {event['name']} · OpenDota ID {event['league_id']} · {event['start_date']}–{event['end_date']} · [источник классификации]({event['source']})"
            for event in scope["events"]
        )
        lines.extend(["", "Исключения по выбранной классификации:", ""])
        lines.extend(
            f"- {event['name']}: {event['reason']} · [источник]({event['source']})"
            for event in scope.get("excluded", [])
        )
        lines.extend(
            [
                "",
                "Региональные и открытые квалификации исключены. Незавершённые события учитываются только по уже сыгранным картам.",
            ]
        )
    lines.extend(
        [
            "",
            "## Как исключён результат текущей карты",
            "",
            "Предиктор получает только Draft: героев, account ID игроков, команды, турнир, сторону и момент наблюдения. Победитель, длительность, убийства, золото и счёт текущей карты в этот объект не входят. Исходы лежат в отдельном файле outcomes.jsonl; прогноз сохраняется в predictions.jsonl до сравнения с исходом.",
            "",
            f"Веса и калибровка модели фиксированы. Последний результат в её исходной истории доступен: {report['model_training_history_cutoff']}. SHA256 весов: `{report['frozen_weights_sha256']}`.",
            "",
            "Рейтинги обновляются по результатам только уже завершённых предыдущих карт, с буфером 60 секунд. Параллельно идущая карта не влияет на прогноз до своего завершения."
            if report["history_mode"] == "previous_finished_games"
            else "Рейтинги и история полностью заморожены до начала периода; результаты всего тестового периода не используются даже для обновления рейтингов.",
            "",
            f"Набор признаков: {report.get('feature_set', 'classic')}. Среднее покрытие предполагаемых ролей: {report.get('mean_role_coverage', 0):.1f} из 10 игроков. Подробные признаки и справочные наблюдения каждой карты: analyses.jsonl.",
            "",
            "Карты одной серии не пересекают границу обучения и теста. Исходная модель обучается и калибруется только на картах до тестового периода.",
            "",
            "## Ограничения",
            "",
        ]
    )
    lines.extend("- " + value for value in report["limitations"])
    lines.extend(
        [
            "",
            "## Следующие улучшения",
            "",
            "1. Проверить более однородную выборку крупных турниров и отдельно малые лиги; сравнивать качество на новой временной выборке.",
            "2. Сравнить classic, roster и detailed: подробные признаки уже реализованы, но включать их в рабочую оценку следует только после улучшения на более раннем отложенном периоде. Роли оцениваются по прошлым играм; крупный патч — по дате его выхода.",
            "3. Выбирать порог публикации только на калибровочном периоде и показывать одновременно точность и долю покрытых карт.",
            "4. Собирать следующие реальные прогнозы без изменения уже опубликованных процентов. Это даст новую независимую проверку после разработки.",
            "",
        ]
    )
    return "\n".join(lines)
