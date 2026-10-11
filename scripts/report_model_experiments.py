"""Build a readable journal from completed, independently preregistered runs."""

import json
from pathlib import Path

ROUNDS = [
    (
        "Архитектура и признаки",
        "model-research-2026-10-11",
        "model-experiments-2026-10-11.json",
        "4e69a85",
    ),
    (
        "Обучение для Tier 1",
        "model-research-tier1-2026-10-11",
        "model-experiments-tier1-2026-10-11.json",
        "b2b16e1",
    ),
    (
        "Прямые контрпики и связки",
        "model-research-counters-2026-10-11",
        "model-experiments-counters-2026-10-11.json",
        "2ba8c4d",
    ),
    (
        "Свежие веса и переобучение",
        "model-research-freshness-2026-10-11",
        "model-experiments-freshness-2026-10-11.json",
        "a822c46",
    ),
    (
        "Результаты предыдущих карт серии",
        "model-research-series-2026-10-11",
        "model-experiments-series-2026-10-11.json",
        "e15fa66",
    ),
    (
        "Точность победителя и калибровка",
        "model-research-calibration-2026-10-11",
        "model-experiments-calibration-2026-10-11.json",
        "e15fa66",
    ),
    (
        "Состав выборки и отдельные рейтинги Tier 1",
        "model-research-data-quality-2026-10-11",
        "model-experiments-data-quality-2026-10-11.json",
        "b7e3861",
    ),
]
LABELS = {
    "baseline": "Базовая версия",
    "stronger_regularization": "Сильнее сглаживать редкие признаки",
    "recent_training": "Больший вес свежих карт",
    "balanced_sides": "Баланс сторон при обучении",
    "role_interactions": "Опыт коров, форма и взаимодействия",
    "nonlinear_shallow": "Неглубокие деревья решений",
    "nonlinear_deeper": "Более глубокие деревья решений",
    "tier1_calibration": "Калибровка только по ранним Tier 1",
    "tier1_weight_4": "Вес Tier 1 ×4",
    "tier1_weight_12_calibrated": "Вес Tier 1 ×12 и отдельная калибровка",
    "tier1_only": "Обучение только на ранних Tier 1",
    "tier1_role_interactions": "Взаимодействия с акцентом на Tier 1",
    "tier1_nonlinear": "Деревья с акцентом на Tier 1",
    "direct_counters": "Отдельные веса пар противников",
    "direct_counters_shrunk": "Веса контрпиков с сильным сглаживанием",
    "role_counters_and_lanes": "Отдельные мид-матчапы и пары на линиях",
    "role_counters_tier_weighted": "Матчапы и линии с весом Tier 1 ×4",
    "fixed_global_target_blend": "Фиксированное среднее двух моделей",
    "more_recent_weights_70": "Обучение 70%, калибровка 10%",
    "more_recent_weights_75": "Обучение 75%, калибровка 5%",
    "rolling_training_90_days": "Обучение только за последние 90 дней",
    "monthly_refit_80": "Ежемесячное переобучение, разделение 80/20",
    "monthly_refit_90": "Ежемесячное переобучение, разделение 90/10",
    "series_score": "Счёт серии и предыдущий победитель",
    "series_rosters_surprise": "Состав, повтор героев и неожиданность предыдущих исходов",
    "series_recent_weights": "Контекст серии и более свежие веса",
    "series_nonlinear": "Деревья с контекстом серии",
    "fresh_75_accuracy_recheck": "Обучение 75%, калибровка 5% — повторная проверка",
    "fresh_75_raw": "Свежие веса без калибровки",
    "fresh_75_score_calibration": "Калибровка только оценки драфта",
    "fresh_75_team_calibration": "Свежие веса, калибровка по драфту и рейтингу команды",
    "fresh_75_player_calibration": "Свежие веса, калибровка по драфту и рейтингу игроков",
    "exclude_small_leagues_training": "Обучение без Destiny, Space и Ultras",
    "exclude_small_leagues_both": "Обучение и калибровка без Destiny, Space и Ultras",
    "tier1_independent_ratings": "Отдельные рейтинги по основным событиям Tier 1",
    "tier1_ratings_clean_training": "Отдельные рейтинги Tier 1 и менее концентрированная выборка",
    "tier1_only_stronger_shrinkage": "Обучение только на Tier 1 с более сильным сглаживанием",
}


def main():
    root = Path(__file__).resolve().parents[1]
    runs = []
    for title, directory, plan_name, commit in ROUNDS:
        folder = root / "artifacts" / directory
        runs.append(
            {
                "title": title,
                "directory": directory,
                "plan_path": plan_name,
                "preregistration_commit": commit,
                "protocol": json.loads((folder / "protocol.json").read_text(encoding="utf-8")),
                "results": json.loads((folder / "comparison.json").read_text(encoding="utf-8")),
            }
        )
    path = root / "docs/model-research-2026-10-11.json"
    path.write_text(
        json.dumps({"runs": runs, "live_model_changed": False}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    attempts = sum(len(run["results"]["development"]) - 1 for run in runs)
    repeated = sum(
        bool(candidate.get("previously_examined"))
        for run in runs
        for candidate in run["protocol"]["plan"]["candidates"]
        if candidate["name"] != "baseline"
    )
    count = attempts - repeated
    finals = sum(run["results"]["tier1"] is not None for run in runs)
    lines = [
        "# Журнал экспериментов модели · 11 октября 2026",
        "",
        f"Проверено {count} новых уникальных вариантов в {len(runs)} сериях; дополнительно {repeated} прежний вариант повторно проверен по новому критерию. Подтверждённого роста точности нет; рабочая модель не заменена.",
        "",
        f"Кандидаты из {finals} серий, выбранные на ранних данных, проверены на одинаковых 442 картах основных турниров Tier 1 за 10 июня — 10 октября. Базовая версия дала 285/442 (64,48%).",
        "",
        "| Вариант | Верных / карт | Точность | Brier ↓ | Решение |",
        "| --- | ---: | ---: | ---: | --- |",
        "| Базовая версия | 285 / 442 | 64,48% | 0,223424 | Сохранить |",
    ]
    for run in runs:
        result = run["results"]
        final = result["tier1"]
        if final:
            candidate = final["candidate"]
            lines.append(
                f"| {LABELS[result['selected_candidate']]} | {candidate['wins']} / {candidate['count']} | {candidate['accuracy']:.2%} | {candidate['brier']:.6f} | Не переносить в рабочую модель |"
            )
    lines += [
        "",
        "Четырёхмесячный период уже использовался в разработке. Это ретроспективное сравнение, а не новый независимый тест будущей точности. Доходность не измерена. 70% на всех картах эти проверки не показали.",
        "",
        "## Как устроена проверка",
        "",
        "Каждый план и его параметры сохранены в Git до запуска своей серии. Отбор использует два более ранних периода; июнь–октябрь не используется для подбора параметров внутри серии. На четырёхмесячную проверку проходит только выбранный кандидат, а не все варианты с последующим выбором самого удачного результата.",
        "",
        "Основной критерий отбора — меньшая ошибка вероятностей Brier в обоих ранних периодах, в среднем улучшение не менее 0,0005. Дополнительно число верных исходов в сумме не должно уменьшаться. Это критерий исследования, а не утверждение о статистической значимости. Окончательная оценка использует парную повторную выборку по 200 сериям.",
        "",
        "В двух последних сериях основной критерий заранее изменён на долю правильно угаданных победителей: прирост минимум 0,5 процентного пункта в сумме и отсутствие регрессии точности в каждом раннем периоде. Ошибка Brier допускается выше контроля максимум на 0,002 в каждом периоде. Прежние планы, критерии и результаты не изменены. Повторное рассмотрение варианта 75/5 отмечено отдельно и не посчитано новым уникальным вариантом.",
        "",
        "Предиктор получает героев, игроков, команды, патч и статистику уже завершённых прошлых карт. Результат и экономика текущей карты не входят в признаки. Предыдущий результат становится доступным после фактического конца плюс 60 секунд. Между обучением, калибровкой и проверкой удаляются пересекающие границу серии; случайного перемешивания по времени нет.",
        "",
        "Варианты ежемесячного переобучения могут использовать уже завершённые карты предыдущего месяца проверки. Это отдельная проверяемая стратегия обновления модели, имитирующая реальную работу; результаты текущего и будущего месяца при переобучении недоступны.",
        "",
        "Базовые вероятности и точный набор ID карт в последней серии сверены с первоначальным сохранённым CSV. Тестовая базовая модель заморожена до июня (`logistic-v2-7f6fc4c35cc1`). Развёрнутая модель той же базовой архитектуры (`logistic-v2-89bc7fac69d6`) обучена на более поздних данных и в этот исторический тест не подставлялась: это создало бы утечку.",
        "",
        "[Калибровка вероятностей](https://scikit-learn.org/stable/modules/calibration.html) и [проверка моделей по времени](https://scikit-learn.org/stable/modules/cross_validation.html#time-series-split) описаны в документации scikit-learn. Для деревьев отключено внутреннее случайное разделение для early stopping.",
        "",
        "## Обучающая выборка и Tier 1",
        "",
        "До июня доступна 18 231 карта. В Destiny, Space и Ultras — 9 725 из них, то есть больше половины. Поэтому после первой серии проверены обучение и калибровка с акцентом на подтверждённые основные турниры Tier 1.",
        "",
        "Список ранних Tier 1 содержит десять проверенных турниров с ID и границами дат; квалификации и Division 2 исключены. Это подтверждённая часть списка, а не все турниры Tier 1 за год. Классификация и источники сохранены в [конфигурации](https://github.com/COMF2222/betbot/blob/main/config/tier1-training-2025-2026.json).",
    ]
    for run in runs:
        result, protocol = run["results"], run["protocol"]
        lines += [
            "",
            "## " + run["title"],
            "",
            f"План зафиксирован коммитом `{run['preregistration_commit']}`: [параметры и гипотезы](https://github.com/COMF2222/betbot/blob/{run['preregistration_commit']}/config/{run['plan_path']}).",
            "",
            "Ранняя проверка: "
            + (
                "подтверждённые основные турниры Tier 1"
                if protocol.get("development_scope")
                else "все доступные про-лиги"
            )
            + ". Ниже суммарное число верных исходов двух периодов; Brier — среднее двух периодов.",
            "",
            "Основной критерий: "
            + protocol["plan"]["primary_metric"]
            + ". Правило отбора: "
            + protocol["plan"]["selection"]
            + ".",
            "",
            "| Идея | Верных / карт | Точность | Среднее Brier ↓ | Статус |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        for name, values in result["development"].items():
            total, wins = sum(v["count"] for v in values), sum(v["wins"] for v in values)
            brier = sum(v["brier"] for v in values) / len(values)
            state = (
                "Контроль"
                if name == "baseline"
                else "Выбран для проверки за четыре месяца"
                if name == result["selected_candidate"]
                else "Не выбран; за четыре месяца не оценивался"
            )
            lines.append(
                f"| {LABELS[name]} | {wins} / {total} | {wins / total:.2%} | {brier:.6f} | {state} |"
            )
        if result["tier1"]:
            final = result["tier1"]
            brier, accuracy = (
                final["brier_difference_95_interval"],
                final["accuracy_difference_95_interval"],
            )
            lines += [
                "",
                f"95% интервал изменения Brier: {brier[0]:+.6f}…{brier[1]:+.6f}; изменения точности: {accuracy[0] * 100:+.2f}…{accuracy[1] * 100:+.2f} процентного пункта. Убедительного улучшения нет.",
            ]
        else:
            lines += [
                "",
                "Ни один кандидат не прошёл заранее заданный ранний отбор. Дополнительную проверку июня–октября для этой серии не запускали.",
            ]
    lines += [
        "",
        "## Доступность данных при публикации",
        "",
        "Проверено отдельно от точности модели: в трёх исторических ответах OpenDota есть по 24 записи пиков/банов. Поле hero_variant присутствует у всех десяти игроков, но все значения равны 0: конкретный выбранный фасет не подтверждён. Наша сохранённая обучающая выборка пока не содержит ни порядка пиков/банов, ни вариантов героев. В проверенном live-снимке Steam Top (1 турнирная карта) и Steam League (35 карт) нет ни пиков/банов, ни выбранных вариантов героев. Это конкретный снимок, а не гарантия поведения всех источников и матчей.",
        "",
        "В 9 ранее опубликованных прогнозах точный series_id был известен при публикации лишь у 2. Исторический контекст предыдущих карт серии был доступен для 9 673 из 19 995 исследуемых карт, поэтому его применение в рабочем боте требует отдельной проверки реального покрытия. Неизвестные ID не восстанавливались по будущим картам или именам команд.",
        "",
        "Снимок схем и агрегаты сохранены в [аудите входов](model-input-availability-2026-10-11.json). Поля исторических ответов подтверждаются [типами OpenDota](https://github.com/odota/core/blob/master/global.d.ts), а источник `/live` — [кодом сервиса OpenDota](https://github.com/odota/core/blob/master/svc/livegames.ts). Исторические фасеты не подставлялись в live-прогноз: сначала нужно обеспечить их получение до публикации.",
        "",
        "## Повторение и сохранённые данные",
        "",
        "Скрипт `scripts/run_model_experiments.py` запускает отдельный план. `scripts/report_model_experiments.py` формирует этот журнал. Смена плана, данных или реализации запрещена в каталоге уже выполненного эксперимента; для новой гипотезы нужен новый каталог.",
        "",
        "```powershell",
        "uv run python scripts/run_model_experiments.py --plan config/model-experiments-counters-2026-10-11.json --output artifacts/new-counter-run",
        "uv run python scripts/report_model_experiments.py",
        "```",
        "",
        "В `artifacts/model-research*/` сохранены протоколы с хешами входных данных и исходников, слепые признаки, CSV прогнозов, ранние оценки, зафиксированный выбор кандидата и итоговые сравнения. Эти объёмные файлы доступны локально; сводный JSON находится рядом с этим Markdown и сохранён в GitHub.",
        "",
        "Запуски исследования не публикуют сообщения в Telegram, не заменяют `data/model.json` и не переписывают прежние прогнозы. Числа всех неудачных и невыбранных вариантов сохранены.",
    ]
    (root / "docs/model-research-2026-10-11.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "variants": count,
                "markdown": "docs/model-research-2026-10-11.md",
                "summary": str(path),
            }
        )
    )


if __name__ == "__main__":
    main()
