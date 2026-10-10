import html
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import ImageFont

from .domain import Prediction, best_quotes, utcnow

MOSCOW = ZoneInfo("Europe/Moscow")


def esc(value: str, maximum: int = 100) -> str:
    return html.escape(str(value)[:maximum])


def caption(p: Prediction, max_age: int = 30) -> str:
    d = p.draft
    map_label = f"карта {d.map_number}" if d.map_number else f"карта ID {d.match_id}"
    result = [
        f"🤖 <b>{esc(d.radiant_name, 45)} — {esc(d.dire_name, 45)}</b> · {map_label}",
        f"Игроки + драфт: <b>{esc(p.favorite_name, 45)} {p.favorite_probability:.0%}</b>",
        f"Только драфт: {p.draft_probability:.0%} / {1 - p.draft_probability:.0%}",
        f"Безубыточный кэф по модели: {1 / p.favorite_probability:.2f} до расходов",
    ]
    if not p.signal:
        result.insert(1, "⚖️ <b>Нет выраженного фаворита · слабый сигнал.</b>")
    if p.late_analysis:
        result.insert(
            1,
            "⏱ <b>Поздний разбор: карта идёт или источник задержан.</b> Отдельный учёт.",
        )
        result.append(
            f"Часы источника: {max(0, d.game_time) // 60}:{max(0, d.game_time) % 60:02d} · DotaTV: {d.delay_seconds // 60} мин задержки. Модель получает только пики и составы."
        )
    quotes = [
        q
        for q in p.quotes
        if q.market == "map_winner" and q.outcome == p.favorite and q.fresh(utcnow(), max_age)
    ]
    if quotes:
        result.append("\nКотировки на фаворита:")
        for q in sorted(quotes, key=lambda q: q.odds, reverse=True)[:3]:
            suffix = " · эквивалент до расходов" if q.gross else ""
            result.append(f"{esc(q.source, 25)}: <b>{q.odds:.2f}</b>{suffix}")
    else:
        result.append("\nСвежей подтверждённой цены на эту карту нет.")
    bucket = p.reliability.get("combined", {})
    if bucket.get("count", 0) >= 100 and bucket.get("success_rate") is not None:
        result.append(
            f"\nПрошлый временной тест: {bucket['success_rate']:.0%} верных исходов в этой группе уверенности, {bucket['count']} карт. Все про-лиги."
        )
    result.append("\nИтог карты будет ответом на этот прогноз.")
    return "\n".join(result)


def explanation(p: Prediction, heroes: dict[int, dict], max_age: int = 30) -> str:
    lines = ["<b>Почему получилась такая оценка</b>"]
    for reason in p.reasons[:10]:
        reason = re.sub(
            r"hero:(\d+)",
            lambda m: str(heroes.get(int(m[1]), {}).get("localized_name", f"Hero #{m[1]}")),
            reason,
        )
        lines.append("• " + esc(reason, 220))
    lines.append("\n<b>Драфт</b>")
    for name, players in (
        (p.draft.radiant_name, p.draft.radiant),
        (p.draft.dire_name, p.draft.dire),
    ):
        lines.append(esc(name, 55) + ":")
        assignments = p.analysis.get("roles", {}).get(
            "radiant" if players is p.draft.radiant else "dire", []
        )
        positions = {r["account_id"]: r["position"] for r in assignments if r.get("position")}
        for player in players:
            hero = heroes.get(player.hero_id, {}).get("localized_name", f"Hero #{player.hero_id}")
            position = positions.get(player.account_id)
            suffix = f" · предполагаемая поз. {position}" if position else ""
            lines.append(
                f"  {esc(player.name or f'ID {player.account_id}', 40)} — {esc(hero, 35)}{suffix}"
            )
    if p.duration_interval:
        lo, hi = p.duration_interval
        lines.append(
            f"\nОриентир длительности: {p.duration_seconds / 60:.0f} мин; эмпирический диапазон 80%: {lo / 60:.0f}–{hi / 60:.0f} мин."
        )
        lines.append("Модель длительности экспериментальная; точность проверяется отдельно.")
    for total in p.totals[:4]:
        side = "ТБ" if total["outcome"] == "over" else "ТМ"
        lines.append(
            f"{side} {total['line']:g} мин: {total['probability']:.0%} · {esc(total['source'], 25)} {total['odds']:.2f}"
        )
    selected = [q for q in p.quotes if q.outcome == p.favorite and q.market == "map_winner"]
    groups = best_quotes(selected, utcnow(), max_age)
    if groups:
        lines.append("\n<b>Цена и вероятность</b>")
        for q in groups[:3]:
            expectation = p.favorite_probability * q.odds - 1
            label = "до расходов" if q.gross else "по условиям указанного рынка"
            lines.append(
                f"{esc(q.source, 25)}: ожидание по модели {expectation:+.1%} на условную единицу, {label}."
            )
        lines.append(
            "Цены с разными правилами расчёта или неизвестными расходами сравниваются отдельно."
        )
    lines.append(
        f"\nДанные наблюдались: {p.draft.observed_at.astimezone(MOSCOW):%d.%m %H:%M:%S} МСК. Задержка источника: {p.draft.delay_seconds} с."
    )
    lines.append(f"Версия: <code>{esc(p.model_version, 70)}</code>. Проценты — оценка модели.")
    return "\n".join(lines)


def result_text(p: Prediction, result: dict) -> str:
    radiant_win = result.get("radiant_win")
    if not isinstance(radiant_win, bool):
        return "ℹ️ Результат карты пока не подтверждён."
    win = radiant_win == (p.favorite == "radiant")
    winner = p.draft.radiant_name if radiant_win else p.draft.dire_name
    label = f"карту {p.draft.map_number}" if p.draft.map_number else f"карту ID {p.draft.match_id}"
    lines = [
        f"{'✅ Зашло' if win else '❌ Мимо'}: <b>{esc(winner, 60)}</b> выиграли {label}.",
        f"Прогноз был: {esc(p.favorite_name, 60)} {p.favorite_probability:.0%}.",
    ]
    if p.late_analysis:
        lines.append(
            "⏱ Это результат разбора с задержкой; он учитывается отдельно от прогнозов сразу после драфта."
        )
    if result.get("duration"):
        seconds = int(result["duration"])
        lines.append(f"Длительность: {seconds // 60}:{seconds % 60:02d}.")
        for total in p.totals[:8]:
            line_seconds = total["line"] * 60
            label = ("ТБ" if total["outcome"] == "over" else "ТМ") + f" {total['line']:g} мин"
            if seconds == line_seconds:
                lines.append(f"ℹ️ {label}: длительность равна линии; расчёт по правилам площадки.")
            else:
                won = (
                    seconds > line_seconds if total["outcome"] == "over" else seconds < line_seconds
                )
                lines.append(
                    f"{'✅' if won else '❌'} {label} · {esc(total['source'], 25)} {total['odds']:.2f}."
                )
    quotes = [q for q in p.quotes if q.market == "map_winner" and q.outcome == p.favorite]
    if quotes:
        lines.append(
            "Цены в момент прогноза: "
            + "; ".join(
                f"{esc(q.source, 25)} {q.odds:.2f}{' до расходов' if q.gross else ''}"
                for q in quotes[:5]
            )
        )
    return "\n".join(lines)


def stats_text(summary: dict, label: str = "За последние 7 дней") -> str:
    s = summary
    rate = f"{s['wins'] / s['settled']:.1%}" if s["settled"] else "пока нет результатов"
    text = (
        f"📊 <b>{esc(label, 100)}</b>\n"
        f"Опубликовано: {s['published']}\n✅ {s['wins']} · ❌ {s['losses']}\n"
        f"Подтверждено: {s['settled']} · ожидают результата: {s['pending']}\n"
        f"Доля верных прогнозов: {rate}\nПропуски / неполные данные: {s['skipped']}\n"
        "Финансовый результат не измеряется по проценту угаданных карт."
    )
    late = s.get("late", {})
    if late.get("published"):
        rate = f"{late['wins'] / late['settled']:.1%}" if late["settled"] else "ожидают результата"
        text += f"\n\n⏱ <b>Отдельно: разборы с задержкой</b>\nОпубликовано: {late['published']} · верно {late['wins']} из {late['settled']} · {rate}."
    return text


def font(size: int, bold: bool = False):
    names = [
        Path("C:/Windows/Fonts/seguisb.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
        Path(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
    ]
    for path in names:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def render_card(
    p: Prediction,
    heroes: dict[int, dict] | None = None,
    assets: Path | None = None,
    demo: bool = False,
) -> bytes:
    from .graphics import forecast_image

    return forecast_image(p, heroes, assets, demo)


def np_gradient(width: int, height: int) -> bytes:
    import numpy as np

    xx, yy = np.meshgrid(np.linspace(0, 1, width), np.linspace(0, 1, height))
    left = np.exp(-((xx + 0.1) ** 2 + (yy - 0.45) ** 2) / 0.16)
    right = np.exp(-((xx - 1.1) ** 2 + (yy - 0.3) ** 2) / 0.16)
    arr = np.stack(
        (12 + 8 * right, 18 + 28 * left + 9 * right, 33 + 13 * left + 35 * right), axis=-1
    )
    return arr.astype(np.uint8).tobytes()
