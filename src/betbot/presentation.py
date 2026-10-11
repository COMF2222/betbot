import html
import math
import re
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import ImageFont

from .domain import Prediction, Quote, best_quotes, timestamp, utcnow
from .timing import timing_note

MOSCOW = ZoneInfo("Europe/Moscow")


def esc(value: str, maximum: int = 100) -> str:
    return html.escape(str(value)[:maximum])


def stars(probability: float) -> str:
    from .graphics import strength

    return "★" * strength(probability)[0] or "☆"


def short_reasons(p: Prediction, heroes: dict | None = None) -> str:
    heroes = heroes or {}

    def hero(identity):
        return str(heroes.get(identity, {}).get("localized_name", f"Hero #{identity}"))

    sign = 1 if p.favorite == "radiant" else -1
    reasons = []
    if sign * p.features.get("player_elo", 0) > 0.025:
        reasons.append("Игроки сильнее")
    elif sign * p.features.get("hero_experience", 0) > 0.01:
        reasons.append("Игроки успешнее на своих героях")
    elif sign * p.features.get("team_elo", 0) > 0.025:
        reasons.append("Команда сильнее по прошлым картам")
    statistics = []
    for item in p.analysis.get("counters", []):
        if item.get("count", 0) >= 20 and item.get("win_rate", 0) >= 0.55:
            statistics.append(
                (
                    item.get("excess", item["win_rate"] - 0.5),
                    f"{hero(item['hero'])} бьёт {hero(item['enemy'])} в {item['win_rate']:.0%} игр",
                )
            )
    for item in p.analysis.get("lanes", []):
        if item.get("count", 0) >= 20 and item.get("map_win_rate", 0) >= 0.55:
            statistics.append(
                (
                    (item["map_win_rate"] - 0.5) * item["count"] / (item["count"] + 30),
                    f"связка {hero(item['core'])} + {hero(item['support'])} берёт {item['map_win_rate']:.0%} игр",
                )
            )
    for item in p.analysis.get("pairs", []):
        if item.get("count", 0) < 20 or item.get("advantage", 0) <= 0:
            continue
        own = item["side"] == p.favorite
        if (own and item["win_rate"] < 0.55) or (not own and item["win_rate"] > 0.45):
            continue
        team = p.draft.radiant_name if item["side"] == "radiant" else p.draft.dire_name
        a, b = item["heroes"]
        statistics.append(
            (
                item["advantage"],
                f"сочетание {hero(a)} + {hero(b)} у {team} берёт {'лишь ' if not own else ''}{item['win_rate']:.0%} игр",
            )
        )
    if statistics:
        statistics.sort(key=lambda item: item[0], reverse=True)
        reasons += [item for _, item in statistics[: 2 - len(reasons)]]
    if not reasons:
        reasons.append("Небольшой перевес по сумме игроков и драфта")
    return esc(", ".join(reasons), 260)


def market_snapshot(p: Prediction, max_age=None, now=None):
    from .reports import market_pair

    quotes = [
        q
        for q in p.quotes
        if q.market == "map_winner"
        and q.match_id == p.draft.match_id
        and q.map_number == p.draft.map_number
        and q.active
        and math.isfinite(q.odds)
        and q.odds > 1
        and (max_age is None or q.fresh(now or utcnow(), max_age))
    ]
    pair = market_pair(quotes)
    opponent = "dire" if p.favorite == "radiant" else "radiant"
    if pair:
        return pair["quotes"][p.favorite], pair["quotes"][opponent], pair[p.favorite]
    own = sorted(
        (q for q in quotes if q.outcome == p.favorite),
        key=lambda q: (q.source != "Polymarket", -q.odds),
    )
    return (own[0], None, 1 / own[0].odds) if own else (None, None, None)


def caption(p: Prediction, max_age: int = 30, heroes=None, now=None) -> str:
    d = p.draft
    opponent = d.dire_name if p.favorite == "radiant" else d.radiant_name
    map_label = f"карта {d.map_number}" if d.map_number else f"карта ID {d.match_id}"
    lines = [
        f"🤖 <b>Бот за {esc(p.favorite_name, 45)} против {esc(opponent, 45)}</b>, {map_label}: <b>{p.favorite_probability:.0%}</b>, {stars(p.favorite_probability)}",
        "",
        "👉 " + short_reasons(p, heroes),
    ]
    info = []
    if not p.signal:
        info.append("Слабый сигнал.")
    if max(p.draft_probability, 1 - p.draft_probability) < 0.55:
        info.append("По одним героям ровно.")
    elif (p.draft_probability >= 0.5) != (p.favorite == "radiant"):
        info.append(
            f"По одним героям за {esc(opponent, 45)} {max(p.draft_probability, 1 - p.draft_probability):.0%}."
        )
    own, other, probability = market_snapshot(p, max_age, now)
    if own:
        label = "Рынок"
        if (own.observed_at - p.draft.observed_at).total_seconds() > 30:
            label += f" на {own.observed_at.astimezone(MOSCOW):%H:%M}"
        line = f"{label}: {esc(p.favorite_name, 45)} <b>{own.odds:.2f}</b> ({probability:.0%})"
        if other:
            line += f", {esc(opponent, 45)} {other.odds:.2f}"
        info.append(line)
    else:
        info.append("Рынок: кэф на эту карту пока недоступен.")
    lines.append("ℹ️ " + " ".join(info))
    if note := timing_note(p):
        lines.append("⏱ " + note)
    return "\n".join(lines)


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
        side = "radiant" if players is p.draft.radiant else "dire"
        comfort = {r["account_id"]: r for r in p.analysis.get("comfort", {}).get(side, [])}
        for player in players:
            hero = heroes.get(player.hero_id, {}).get("localized_name", f"Hero #{player.hero_id}")
            position = positions.get(player.account_id)
            suffix = f" · предполагаемая поз. {position}" if position else ""
            sample = comfort.get(player.account_id, {})
            if sample.get("count"):
                suffix += f" · на герое {sample['wins']}/{sample['count']} побед ({sample['win_rate']:.0%})"
                if sample.get("pick_share") is not None:
                    suffix += f" · {sample['pick_share']:.0%} его про-карт"
            elif sample:
                suffix += " · прошлых про-карт на герое нет в истории"
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
            executable_odds = q.ask_odds if q.price_kind == "market" else q.odds
            if executable_odds is None:
                continue
            expectation = p.favorite_probability * executable_odds - 1
            label = "до расходов" if q.gross else "по условиям указанного рынка"
            lines.append(
                f"{esc(q.source, 25)}: покупка по кэфу {executable_odds:.2f}, ожидание по модели {expectation:+.1%} на условную единицу, {label}."
            )
        lines.append(
            "Цены с разными правилами расчёта или неизвестными расходами сравниваются отдельно."
        )
    lines.append(
        f"\nДанные наблюдались: {p.draft.observed_at.astimezone(MOSCOW):%d.%m %H:%M:%S} МСК. Задержка источника: {p.draft.delay_seconds} с."
    )
    lines.append(f"Версия: <code>{esc(p.model_version, 70)}</code>. Проценты — оценка модели.")
    return "\n".join(lines)


def result_text(p: Prediction, result: dict, snapshots=None) -> str:
    radiant_win = result.get("radiant_win")
    if not isinstance(radiant_win, bool):
        return "ℹ️ Результат карты пока не подтверждён."
    win = radiant_win == (p.favorite == "radiant")
    winner = p.draft.radiant_name if radiant_win else p.draft.dire_name
    label = f"карту {p.draft.map_number}" if p.draft.map_number else f"карту ID {p.draft.match_id}"
    signal = f"{p.favorite_probability:.0%}, {stars(p.favorite_probability)}"
    own, _, market_probability = market_snapshot(p)
    price = f", кэф {own.odds:.2f}" if own else ""
    if own and (own.observed_at - p.draft.observed_at).total_seconds() > 30:
        price += f" (на {own.observed_at.astimezone(MOSCOW):%H:%M})"
    if win:
        lines = [f"✅ <b>Зашло: {esc(winner, 60)} взяли {label}</b>, {signal}{price}"]
    else:
        lines = [
            f"❌ <b>Мимо: {label} взяли {esc(winner, 60)}</b>, бот был за {esc(p.favorite_name, 60)} с {signal}{price}"
        ]
    start = timestamp(result.get("start_time"))
    finish = (
        start + timedelta(seconds=int(result["duration"]))
        if start and result.get("duration")
        else None
    )
    observations = []
    if own and finish:
        for snapshot in snapshots or []:
            when = timestamp(snapshot.get("observed_at"))
            if not when or not max(start, p.draft.observed_at, own.observed_at) <= when <= finish:
                continue
            for raw in snapshot.get("quotes", []):
                q = Quote.from_dict(raw)
                if (
                    (
                        q.source,
                        q.match_id,
                        q.map_number,
                        q.market,
                        q.rules,
                        q.gross,
                        q.market_id,
                    )
                    != (
                        own.source,
                        own.match_id,
                        own.map_number,
                        own.market,
                        own.rules,
                        own.gross,
                        own.market_id,
                    )
                    or q.price_kind != own.price_kind
                    or not q.fresh(when, 30)
                ):
                    continue
                observations.append(q)
    if win and observations:
        peak = max((q.odds for q in observations if q.outcome == p.favorite), default=own.odds)
        if peak > own.odds + 0.01:
            lines[0] += f", наблюдали до {peak:.2f}"
    elif not win:
        winner_side = "radiant" if radiant_win else "dire"
        comeback = max(
            (q for q in observations if q.outcome == winner_side),
            key=lambda q: q.odds,
            default=None,
        )
        if comeback and 1 / comeback.odds <= 0.4:
            lines.append(
                f"👉 По ходу рынок давал {esc(winner, 45)} {1 / comeback.odds:.0%} (кэф {comeback.odds:.2f}), но они выиграли карту"
            )
        if market_probability is not None:
            expected = p.favorite_name if market_probability >= 0.5 else winner
            probability = max(market_probability, 1 - market_probability)
            lines.append(
                f"ℹ️ Рынок {'тоже ' if market_probability >= 0.5 else ''}ждал {esc(expected, 45)} ({probability:.0%})"
            )
    if note := timing_note(p):
        lines.append("⏱ " + note)
    if result.get("duration"):
        seconds = int(result["duration"])
        for total in p.totals[:3]:
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
        text += f"\n\n⏱ <b>Отдельно: время прогноза не подтверждено или карта уже шла</b>\nОпубликовано: {late['published']} · верно {late['wins']} из {late['settled']} · {rate}."
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
