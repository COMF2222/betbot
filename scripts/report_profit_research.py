"""Evaluate frozen entry rules on an exported ledger; never open a live Store."""

import argparse
import hashlib
import json
from datetime import timedelta
from pathlib import Path

from betbot.domain import Prediction, timestamp
from betbot.profit_lab import PROTOCOL, PROTOCOL_HASH, cluster_key, decisions, summarize


def evaluate(data):
    entries = []
    integrity = []
    for row in data["forecasts"]:
        p = Prediction.from_dict(json.loads(row["payload"]))
        when = timestamp(row["created_at"])
        result = json.loads(row["result"]) if row["result"] else {}
        shadow = data.get("shadows", {}).get(p.draft.match_id)
        if shadow:
            recorded = timestamp(shadow.get("recorded_at"))
            confirmed = timestamp(result.get("confirmed_at"))
            if (
                not recorded
                or recorded > when + timedelta(seconds=30)
                or confirmed
                and recorded >= confirmed
            ):
                integrity.append({"id": row["id"], "issue": "late_shadow_excluded"})
                shadow = None
        entries.append(
            {
                "forecast_id": row["id"],
                "created_at": when.isoformat(),
                "cluster": cluster_key(p),
                "timing": "early"
                if p.timing_status == "early" and not p.late_analysis
                else "delayed_or_unverified",
                "decisions": decisions(p, when, shadow, shadow_as_of=when + timedelta(seconds=30)),
                "radiant_win": result.get("radiant_win"),
            }
        )
    return {
        "phase": "historical_research_not_holdout",
        "exported_at": data["exported_at"],
        "research_through_forecast_id": data["research_through_forecast_id"],
        "protocol_hash": PROTOCOL_HASH,
        "protocol": PROTOCOL,
        "integrity_notes": integrity,
        "all": summarize(entries),
        "early": summarize([entry for entry in entries if entry["timing"] == "early"]),
        "delayed": summarize([entry for entry in entries if entry["timing"] != "early"]),
        "entries": entries,
    }


def markdown(data):
    lines = [
        "# Поиск преимущества над ценой · 15 октября 2026",
        "",
        "Цель — положительная доходность отбора при одинаковой условной ставке, а не уменьшение убытка снижением сумм. "
        "Правила фиксированы до расчёта: 8 вариантов, исходная парная цена покупки Polymarket, минимум 1.30. "
        "Расходы 2% и 5% — проверочные допущения, не измеренные комиссии или исполнение. "
        "Все результаты ниже исследовательские: эта история уже обсуждалась и не является независимым контролем.",
        "",
        f"Исходных прогнозов: {data['all']['observations']}. Подтверждённо ранних: {data['early']['observations']}. "
        f"Поздних или без подтверждённого времени: {data['delayed']['observations']}.",
        "",
        "| Отбор | Взято / рассчитано | Победы | ROI без расходов | ROI при 2% | ROI при 5% | Группы серий |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]

    def percent(value):
        return f"{value:+.1%}" if value is not None else "—"

    for row in data["all"]["policies"]:
        lines.append(
            f"| {row['name']} | {row['selected']} / {row['settled']} | {row['wins']} | {percent(row['gross_roi'])} | {percent(row['roi'])} | {percent(row['stress_5pct_roi'])} | {row['clusters']} |"
        )
    early_priced = data["early"]["policies"][0]["selected"]
    lines.extend(
        [
            "",
            f"Из ранних прогнозов у контроля пригодная исходная пара цен покупки есть на {early_priced} картах. "
            "Результаты поздних карт нельзя представлять как прибыль после драфта до начала игры: рынок мог уже учитывать события карты.",
            "",
            "Вместо подбора самого удачного порога на этой истории включён отдельный будущий реестр. "
            "Каждый новый прогноз получает неизменяемый выбор всех восьми вариантов до результата. "
            "Публичный фаворит, коэффициенты и действующий виртуальный банк не меняются. "
            "Личные результаты доступны в разделе «Доходность · исследование».",
            "",
            "Кандидат может быть вынесен на отдельное решение только на будущих подтверждённо ранних картах: "
            "минимум 100 рассчитанных выборов и 30 групп серий, положительная нижняя граница группового bootstrap-интервала "
            "с поправкой на восемь вариантов, плюс положительный ROI при проверочных расходах 5%. "
            "Это необходимые условия просмотра, не гарантия достаточной мощности или будущего заработка. "
            "Автоматического переключения модели или банка нет.",
            "",
            "Комиссии зависят от рынка; фактические параметры и объём исполнения на исторических снимках не сохранены. "
            "Поэтому большой виртуальный плюс по лучшей цене одной заявки ещё не является доказанным доходом с реального объёма. "
            "[Правила комиссий Polymarket](https://docs.polymarket.com/trading/fees).",
            "",
            f"Протокол: `{data['protocol_hash']}`. Исторический предел: прогноз #{data['research_through_forecast_id']}. "
            "Будущий запуск фиксирует собственную границу при установке, без добавления старых исходов задним числом.",
        ]
    )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    data = evaluate(json.loads(raw))
    data["source_sha256"] = hashlib.sha256(raw).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    args.output.with_suffix(".md").write_text(markdown(data), encoding="utf-8")
    print(
        json.dumps(
            {
                "observations": data["all"]["observations"],
                "protocol": PROTOCOL_HASH,
                "report": str(args.output.with_suffix(".md")),
            }
        )
    )


if __name__ == "__main__":
    main()
