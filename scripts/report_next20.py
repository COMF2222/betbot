"""Publish all twenty preregistered results, including negative and unconverged fits."""

import csv
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    plan = json.loads(
        (root / "config/model-experiments-next20-2026-10-11.json").read_text(encoding="utf-8")
    )
    runs = []
    for group in plan["groups"]:
        folder = root / f"artifacts/model-research-next20-{group}-2026-10-11"
        runs.append(
            {
                "group": group,
                "protocol": json.loads((folder / "protocol.json").read_text(encoding="utf-8")),
                "result": json.loads((folder / "comparison.json").read_text(encoding="utf-8")),
            }
        )
    assert sum(len(r["result"]["development"]) - 1 for r in runs) == 20
    verification = json.loads(
        (root / "docs/glicko-runtime-verification-2026-10-11.json").read_text(encoding="utf-8")
    )
    deployment_path = root / "docs/glicko-deployment-2026-10-11.json"
    deployment = (
        json.loads(deployment_path.read_text(encoding="utf-8"))
        if deployment_path.exists()
        else None
    )
    unique = set()
    folder = root / "artifacts/model-research-next20-ratings-2026-10-11"
    for i in range(3):
        with (folder / f"development-{i}-baseline.csv").open(encoding="utf-8") as stream:
            unique.update(row["match_id"] for row in csv.DictReader(stream))
    summary = {
        "plan_commit": "6036116",
        "unique_variants": 20,
        "development_unique_maps": len(unique),
        "runs": runs,
        "runtime_verification": verification,
        "deployment": deployment,
    }
    (root / "docs/model-next20-2026-10-11.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    labels = {c["name"]: c["label"] for c in plan["candidates"]}
    titles = {
        "ratings": "Рейтинги и форма",
        "draft": "Драфт и объём истории",
        "sampling": "Обучающая выборка",
        "learners": "Способ обучения",
    }
    lines = [
        "# Ещё 20 вариантов: результат и проверка Glicko",
        "",
        "Проверены все 20 новых гипотез. Glicko команд дал **291 из 442 — 65,84%**, базовая версия — **285 из 442 — 64,48%**. Прирост составляет **6 карт, или 1,36 процентного пункта**. Порог 65% на этом историческом периоде пройден; 70% не достигнут.",
        "",
        "Сравнивались одинаковые 442 карты основных турниров Tier 1 за 10 июня — 10 октября 2026. Набор карт не уменьшали, уверенные прогнозы отдельно для этого процента не отбирали. Июнь–октябрь уже исследовался раньше: это численный ретроспективный прирост, а не новый независимый тест будущей точности.",
        "",
        "| Выбранный на ранних данных вариант | Верных / карт | Точность | Brier ↓ |",
        "| --- | ---: | ---: | ---: |",
        "| Базовая версия | 285 / 442 | 64,48% | 0,223424 |",
    ]
    for run in runs:
        result = run["result"]
        if result["tier1"]:
            m = result["tier1"]["candidate"]
            lines.append(
                f"| {labels[result['selected_candidate']]} | {m['wins']} / {m['count']} | {m['accuracy']:.2%} | {m['brier']:.6f} |"
            )
    glicko = runs[0]["result"]["tier1"]
    accuracy, brier = (
        glicko["accuracy_difference_95_interval"],
        glicko["brier_difference_95_interval"],
    )
    lines += [
        "",
        "## Что изменилось",
        "",
        "В исходной версии командный Elo менялся с постоянным шагом. Glicko хранит рейтинг и его неопределённость: после перерывов неопределённость растёт, завершённые карты её уменьшают. Неуверенный рейтинговый перевес смягчается, а новые результаты сильнее корректируют плохо известную силу команды. Пики, игроки и опыт на героях продолжают входить в модель; веса и калибровка обучаются с новым командным признаком.",
        "",
        "Использован Glicko-1 с начальными 1500/350, нижней границей RD 30 и фиксированным ростом неопределённости: RD 50 достигает 350 после 180 дней без игры. Параметры не подбирались по результатам июня–октября. [Формулы автора Glicko](https://www.glicko.net/glicko/glicko.pdf).",
        "",
        "## Насколько убедителен прирост",
        "",
        f"Парная bootstrap-проверка по {glicko['series_blocks']} сериям: 95% интервал изменения точности **{accuracy[0] * 100:+.2f}…{accuracy[1] * 100:+.2f} п.п.** включает ноль. Интервал изменения Brier **{brier[0]:+.6f}…{brier[1]:+.6f}** ниже нуля. Эти интервалы описывают данный исторический набор и не учитывают все предыдущие исследования периода; они не заменяют будущую проверку.",
        "",
        "| Турнир | Базовая версия | Glicko |",
        "| --- | ---: | ---: |",
    ]
    names = {
        19102: "BLAST Slam VIII",
        19719: "The International 2026",
        19785: "Esports World Cup 2026",
        20009: "1win Essence II",
    }
    for league in glicko["by_league"]:
        a, b = league["baseline"], league["candidate"]
        lines.append(
            f"| {names[league['league_id']]} | {a['wins']} / {a['count']} | {b['wins']} / {b['count']} |"
        )
    lines += [
        "",
        "Основной выигрыш получен на Esports World Cup. На International стало хуже. Поэтому обещать одинаковый прирост на любом следующем турнире оснований пока нет.",
        "",
        "## Как выбирали",
        "",
        "План всех двадцати вариантов зафиксирован до запусков коммитом `6036116`. Четыре группы по пять вариантов проверены на трёх ранних хронологических периодах Tier 1. Ранние сравнения содержат 318, 229 и 355 карт: суммарно 902 наблюдения, но периоды пересекаются.",
        "",
        f"Уникальных ранних карт — **{len(unique)}**. Их нельзя считать 902 независимыми матчами. Отбор требует отсутствия регрессии точности в каждом периоде, суммарного прироста от 0,01 п.п., ухудшения Brier максимум на 0,002 в каждом периоде и численной сходимости. Выбирается не более одного кандидата из каждой заранее заданной группы; итог июня–октября не меняет выбор других групп.",
        "",
        "Текущий результат, экономика и продолжительность карты в признаки не входят. История обновляется после конца предыдущей карты плюс 60 секунд. Число прошлых игр на герое, составы и рейтинги рассчитаны причинно. Масштабирование, узлы сплайнов и веса лиг/серий/патчей используют только обучение. Нейросеть не сошлась в заранее заданном лимите 500 итераций; её результат сохранён, но кандидат исключён из отбора.",
    ]
    for run in runs:
        result = run["result"]
        lines += [
            "",
            "## " + titles[run["group"]],
            "",
            "| Вариант | Верных на ранних периодах / 902 | Среднее Brier ↓ | Четыре месяца |",
            "| --- | ---: | ---: | --- |",
        ]
        for name, values in result["development"].items():
            wins = sum(v["wins"] for v in values)
            loss = sum(v["brier"] for v in values) / len(values)
            state = (
                "Контроль: 285/442"
                if name == "baseline"
                else "Не выбран; июнь–октябрь не оценивался"
            )
            if name == result["selected_candidate"]:
                m = result["tier1"]["candidate"]
                state = f"{m['wins']}/442 · {m['accuracy']:.2%}"
            if any(v.get("fit_warnings") for v in values):
                state = "Не сошёлся в заданном лимите; не выбран"
            lines.append(f"| {labels[name]} | {wins} / 902 | {loss:.6f} | {state} |")
    lines += [
        "",
        "## Проверка кода бота и модель",
        "",
        f"Обычный `Model.predict` воспроизвёл все 442 вероятности исследовательского варианта. Максимальная разница **{verification['maximum_probability_difference']:.3g}**. Замороженная модель для этого теста — `{verification['version']}`, история до **9 июня**. Проверка сохранена в [JSON](glicko-runtime-verification-2026-10-11.json).",
        "",
        "Отдельная свежая модель подготовлена по 20 000 уже завершённым картам с историей до 10 октября. Её нельзя подставлять в июнь–октябрь для повторного подсчёта 65,84%: она уже знает эти результаты. Экспериментальная версия по умолчанию не разрешает публикацию; активация требует отдельного явного одобрения и сохраняет проверку качества обучающего отчёта.",
    ]
    if deployment:
        lines += [
            "",
            f"По явному запросу пользователя Glicko активирован на сервере: `{deployment['active_version']}`. Старые прогнозы сохранены, предыдущая модель находится в `{deployment['backup']}`. Это запуск с последующим наблюдением будущих результатов, а не заявление о подтверждённой будущей точности.",
        ]
    lines += [
        "",
        "## Повторение",
        "",
        "```powershell",
        "uv run python scripts/run_model_experiments.py --plan config/model-experiments-next20-2026-10-11.json --group ratings --output artifacts/next20-ratings-repeat",
        "uv run betbot backtest --months 4 --end 2026-10-10T18:53:18.481928+03:00 --input data/detailed-history.json --league-list config/tier1-2026.json --feature-set glicko_team --output artifacts/glicko-runtime-repeat",
        "uv run python scripts/prepare_glicko_candidate.py --output artifacts/glicko-candidate-repeat/model.json",
        "```",
        "",
        "Входная история, исходники, план и сохранённые признаки имеют хеши в протоколах. Старые каталоги не перезаписываются после изменения реализации. Все CSV, включая неудачные варианты, сохранены локально в `artifacts/model-research-next20-*/`. Сводный JSON рядом с этим отчётом содержит все метрики и исходные протоколы.",
    ]
    (root / "docs/model-next20-2026-10-11.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "variants": 20,
                "glicko_accuracy": glicko["candidate"]["accuracy"],
                "markdown": "docs/model-next20-2026-10-11.md",
                "deployed": bool(deployment),
            }
        )
    )


if __name__ == "__main__":
    main()
