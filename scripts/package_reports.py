"""Package the reviewed comparison and reports for Telegram's /history command."""

import json
import shutil
from pathlib import Path

from betbot.backtest import markdown_report

root = Path("artifacts/draft-comparison")
reports = {}
for name in ("classic", "detailed"):
    reports[name] = json.loads((root / name / "report.json").read_text(encoding="utf-8"))
    # Refresh presentation without refitting or touching the original predictions.
    (root / name / "report.md").write_text(markdown_report(reports[name]), encoding="utf-8")
    model_report = json.loads((root / name / "frozen-model.json").read_text(encoding="utf-8"))[
        "report"
    ]
    reports[name]["preperiod_brier"] = model_report["brier"]
classic, detailed = reports["classic"], reports["detailed"]
text = f"""# Проверка подробного анализа драфта

Одинаковая выборка: {classic["all_maps"]["count"]} карт основных событий Tier 1, {classic["requested_start"][:10]} — {classic["requested_end"][:10]}. Веса обучены и зафиксированы до начала этих четырёх месяцев. Вход текущей карты не содержит её результат, длительность или показатели экономики.

| Модель | Верных / всего | Точность всех карт | Brier | Сигналы от 55% | Покрытие |
| --- | ---: | ---: | ---: | ---: | ---: |
| Базовая classic | {classic["all_maps"]["wins"]} / {classic["all_maps"]["count"]} | {classic["all_maps"]["accuracy"]:.1%} | {classic["all_maps"]["brier"]:.5f} | {classic["signals"]["accuracy"]:.1%} | {classic["signal_coverage"]:.1%} |
| Подробная detailed | {detailed["all_maps"]["wins"]} / {detailed["all_maps"]["count"]} | {detailed["all_maps"]["accuracy"]:.1%} | {detailed["all_maps"]["brier"]:.5f} | {detailed["signals"]["accuracy"]:.1%} | {detailed["signal_coverage"]:.1%} |

**Решение: оставить classic.** Подробные признаки не улучшили общую точность. В ещё более раннем отложенном периоде до начала четырёхмесячного теста они также ухудшили Brier: {classic["preperiod_brier"]:.5f} → {detailed["preperiod_brier"]:.5f}. Более высокий процент на другом отборе сигналов не означает улучшения на одинаковых картах.

В detailed проверены поправленные на силу команд контрпики и связки, связки предполагаемых позиций 1+5 и 3+4, отдельный мид-матчап и историческая разница заработанного золота к 10-й минуте. Малые выборки сглаживаются; статистика разделена по крупным патчам. Позиции выводятся из предыдущих завершённых игр, а не из текущего реплея. Среднее покрытие предполагаемых ролей — {classic["mean_role_coverage"]:.1f} из 10 игроков.

Подробный разбор реализован и показывается справочно. Его отдельные признаки пока не меняют основную вероятность classic. В базовой модели остаются обучаемые признаки героев и их сочетаний, рейтинги текущих игроков, команд и история игроков на выбранных героях.

Gold10 означает накопленное заработанное золото, не net worth. Разница экономики к 10-й минуте не измеряет чистый эффект линии: на неё влияют перемещения, убийства и действия всей команды. Модель не объявляет пример Pangolier против Invoker выгодным без соответствующей прошлой выборки.

Ранее проверенный вариант roster дал 64,0% на всех 442 картах и тоже не заменил базовую модель. Текущие игроки учитываются по account ID, поэтому замены отражаются в их рейтингах и опыте на героях.

Ретроспективный тест пересекается с изученным периодом разработки. Он не является новым независимым доказательством. Исторических коэффициентов нет, доходность не измерена. Порог публикации по этому тесту не оптимизировался. Следующий полезный шаг — неизменяемые прогнозы на будущих картах, с фиксацией котировок и времени перед публикацией.

- [Базовый отчёт](classic/report.md)
- [Подробный эксперимент](detailed/report.md)
- [Все исходы базовой модели](classic/results.csv)
- [Входы без текущего исхода](classic/blind-inputs.jsonl)
- [Разборы карт и признаки](classic/analyses.jsonl)
- [Схема данных OpenDota](https://github.com/odota/core/blob/master/global.d.ts)
"""
(root / "report.md").write_text(text, encoding="utf-8")
destination = Path("data/reports/tier1")
destination.mkdir(parents=True, exist_ok=True)
for name in ("report.md", "report.json", "results.csv"):
    shutil.copyfile(root / "classic" / name, destination / name)
shutil.copyfile(root / "report.md", destination / "comparison.md")
old = Path("artifacts/backtest-tier1/report.json")
if old.exists():
    old.with_suffix(".md").write_text(
        markdown_report(json.loads(old.read_text(encoding="utf-8"))), encoding="utf-8"
    )
print(f"Packaged {classic['all_maps']['count']} maps and comparison")
