"""Daily/weekly visuals derived from the immutable ledger and saved market snapshots."""

import json
import math
from datetime import UTC, datetime, time, timedelta

from .domain import Prediction, Quote, timestamp, utcnow
from .graphics import AMBER, CYAN, GOLD, MUTED, SUBTLE, SURFACE, Canvas, strength
from .presentation import MOSCOW, esc
from .timing import separate_timing


def period_bounds(period="today", now=None):
    now = (now or utcnow()).astimezone(MOSCOW)
    today = datetime.combine(now.date(), time.min, MOSCOW)
    if period == "yesterday":
        start, end = today - timedelta(days=1), today
    elif period == "week":
        start, end = now - timedelta(days=7), now
    else:
        start, end = today, today + timedelta(days=1)
    label = "Итоги недели" if period == "week" else "Итоги дня"
    return start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat(), label


def market_pair(quotes):
    groups = {}
    for q in quotes:
        if (
            q.market != "map_winner"
            or q.outcome not in ("radiant", "dire")
            or not q.active
            or not math.isfinite(q.odds)
            or q.odds <= 1
        ):
            continue
        key = (q.source, q.match_id, q.map_number, q.rules, q.gross, q.market_id, q.price_kind)
        groups.setdefault(key, {})[q.outcome] = q
    ordered = sorted(groups.items(), key=lambda item: (item[0][0] != "Polymarket", item[0]))
    for key, pair in ordered:
        if set(pair) != {"radiant", "dire"}:
            continue
        if (
            not pair["radiant"].observed_at
            or not pair["dire"].observed_at
            or abs((pair["radiant"].observed_at - pair["dire"].observed_at).total_seconds()) > 30
        ):
            continue
        r, d = 1 / pair["radiant"].odds, 1 / pair["dire"].odds
        if key[6] == "market" and abs(r + d - 1) > 0.10 + 1e-9:
            continue
        return {
            "radiant": r if key[6] == "market" else r / (r + d),
            "dire": d if key[6] == "market" else d / (r + d),
            "source": key[0],
            "quotes": pair,
        }
    return None


def entries_from_rows(rows, late_analysis=False, aliases=None):
    result = []
    for row in rows:
        p = Prediction.from_dict(json.loads(row["payload"]))
        p.draft = p.draft.with_team_aliases(aliases or {})
        if separate_timing(p) != late_analysis:
            continue
        actual = json.loads(row["result"]) if row["result"] else {}
        won = actual.get("radiant_win")
        settled = isinstance(won, bool)
        pure = (
            "radiant"
            if p.draft_probability > 0.5
            else "dire"
            if p.draft_probability < 0.5
            else None
        )
        pure_probability = max(p.draft_probability, 1 - p.draft_probability)
        pair = market_pair(
            [
                q
                for q in p.quotes
                if q.match_id == p.draft.match_id and q.map_number == p.draft.map_number
            ]
        )
        map_label = (
            f"карта {p.draft.map_number}" if p.draft.map_number else f"карта ID {p.draft.match_id}"
        )
        result.append(
            {
                "match_id": p.draft.match_id,
                "label": f"{p.draft.radiant_name} — {p.draft.dire_name} · {map_label}",
                "favorite": p.favorite_name,
                "probability": p.favorite_probability,
                "strength": strength(p.favorite_probability)[0],
                "correct": won == (p.favorite == "radiant") if settled else None,
                "draft_favorite": p.draft.radiant_name
                if pure == "radiant"
                else p.draft.dire_name
                if pure
                else "Ровно",
                "draft_probability": pure_probability,
                "draft_strength": strength(pure_probability)[0],
                "draft_correct": won == (pure == "radiant") if settled and pure else None,
                "market_probability": pair[p.favorite] if pair and settled else None,
                "against_market": bool(pair and settled and pair[p.favorite] < 0.5),
                "actual": actual,
                "prediction": p,
            }
        )
    return result


def totals(entries):
    complete = [e for e in entries if e["correct"] is not None]
    pure = [e for e in complete if e["draft_correct"] is not None]
    prices = [e for e in complete if e["market_probability"] is not None]
    against = [e for e in prices if e["against_market"]]
    strong = [e for e in complete if e["strength"] >= 3]
    wins = sum(e["correct"] for e in complete)
    return {
        "published": len(entries),
        "settled": len(complete),
        "wins": wins,
        "losses": len(complete) - wins,
        "pending": len(entries) - len(complete),
        "accuracy": wins / len(complete) if complete else None,
        "draft_count": len(pure),
        "draft_wins": sum(e["draft_correct"] for e in pure),
        "market_count": len(prices),
        "market_expected": sum(e["market_probability"] for e in prices),
        "against_count": len(against),
        "against_wins": sum(e["correct"] for e in against),
        "strong_count": len(strong),
        "strong_wins": sum(e["correct"] for e in strong),
    }


def report_payload(store, since, until, label="Итоги дня", destination=None, late_analysis=False):
    aliases = store.get("team_aliases", {})
    entries = entries_from_rows(
        store.published_between(since, until, destination), late_analysis, aliases
    )
    cumulative = totals(
        entries_from_rows(
            store.published_between(
                "1970-01-01T00:00:00+00:00", min(until, utcnow().isoformat()), destination
            ),
            late_analysis,
            aliases,
        )
    )
    comeback = None
    for entry in entries:
        if entry["correct"] is None:
            continue
        actual = entry["actual"]
        start = timestamp(actual.get("start_time"))
        duration = actual.get("duration")
        if not start or not duration:
            continue
        finish = start + timedelta(seconds=int(duration))
        winner = "radiant" if actual["radiant_win"] else "dire"
        prediction = entry["prediction"]
        team = prediction.draft.radiant_name if winner == "radiant" else prediction.draft.dire_name
        for snapshot in store.quote_snapshots(entry["match_id"]):
            when = timestamp(snapshot["observed_at"])
            if not when or not start <= when <= finish:
                continue
            quotes = [Quote.from_dict(q) for q in snapshot["quotes"]]
            pair = market_pair(
                [
                    q
                    for q in quotes
                    if q.match_id == entry["match_id"]
                    and q.map_number == prediction.draft.map_number
                    and q.observed_at
                    and start <= q.observed_at <= finish
                ]
            )
            if (
                pair
                and pair[winner] <= 0.15
                and (not comeback or pair[winner] < comeback["probability"])
            ):
                comeback = {
                    "team": team,
                    "opponent": prediction.draft.dire_name
                    if winner == "radiant"
                    else prediction.draft.radiant_name,
                    "map_number": prediction.draft.map_number,
                    "date": prediction.draft.observed_at.astimezone(MOSCOW).strftime("%d.%m"),
                    "label": entry["label"],
                    "probability": pair[winner],
                    "odds": pair["quotes"][winner].odds,
                    "source": pair["source"],
                    "observed_at": when.isoformat(),
                }
    return {
        "label": label,
        "since": since,
        "until": until,
        "date": timestamp(since).astimezone(MOSCOW).date().isoformat(),
        "entries": entries,
        "cumulative": cumulative,
        "comeback": comeback,
        "demo": False,
        "late_analysis": late_analysis,
        **totals(entries),
    }


def report_caption(data):
    total = data["settled"]
    label = data["label"].lower().replace(" · отдельный учёт", "")
    title = f"{'✅' if data['accuracy'] is not None and data['accuracy'] >= 0.5 else '📊'} <b>{data['wins']} из {total} · {esc(label, 100)}</b>"
    lines = [title]
    if data.get("late_analysis"):
        lines.append("⏱ Статистика отдельно: разборы после старта или без точного времени.")
    misses = [e for e in data["entries"] if e["correct"] is False]
    if misses:
        lines.append(
            "Мимо: "
            + "; ".join(esc(e["label"], 100) for e in misses[:2])
            + (f" · ещё {len(misses) - 2}" if len(misses) > 2 else "")
        )
    if data["pending"]:
        lines.append(f"⏳ Ещё {data['pending']} карт ожидают результата. Отчёт предварительный.")
    if data["market_count"]:
        lines.append(
            f"👉 Рынок ждал {data['market_expected']:.1f} из {data['market_count']} карт с ценами."
        )
        if data["against_count"]:
            lines.append(f"Против рынка: {data['against_wins']} из {data['against_count']}.")
    else:
        lines.append("ℹ️ Сравнение с рынком появится после сохранения парных котировок.")
    if data["comeback"]:
        c = data["comeback"]
        lines.append(
            f"👉 Камбэк по цене рынка: {esc(c['team'], 40)} победили; наблюдали {c['probability']:.0%} (кэф {c['odds']:.2f})."
        )
    all_time = data["cumulative"]
    lines.append(
        f"ℹ️ С начала: {all_time['wins']} из {all_time['settled']} · только драфт: {all_time['draft_wins']} из {all_time['draft_count']}."
    )
    if all_time["market_count"] and all_time["settled"] != data["settled"]:
        lines.append(
            f"Рынок: примерно {all_time['market_expected']:.1f} из {all_time['market_count']} карт с ценами."
        )
    if data.get("additional"):
        extra = data["additional"]
        lines.append(
            f"⏱ Отдельный учёт: {extra['wins']} из {extra['settled']} — разборы после старта или без точного времени."
        )
    if data.get("demo"):
        lines.insert(0, "<b>ДЕМОНСТРАЦИЯ · все данные условные</b>")
    return "\n".join(lines)


def public_report_payload(store, since, until, label="Итоги дня", destination=None):
    """One public message, keeping early and additional statistics distinct."""
    early = report_payload(store, since, until, label, destination)
    extra = report_payload(store, since, until, label, destination, late_analysis=True)
    if not early["published"]:
        extra["label"] += " · отдельный учёт"
        return extra
    if extra["published"]:
        early["additional"] = extra
    return early


def weekly_caption(data):
    """Compact weekly recap with honest quote coverage and all confidence buckets."""
    from .presentation import stars

    lines = [f"📈 <b>{data['wins']} из {data['settled']} за неделю</b>"]
    if data["market_count"]:
        coverage = (
            ""
            if data["market_count"] == data["settled"]
            else f" из {data['market_count']} карт с кэфами"
        )
        lines[0] += f", рынок ждал {data['market_expected']:.1f}{coverage}"
    lines.append("")
    buckets = []
    for level, label in ((0, "☆"), (1, "★"), (2, "★★"), (3, "★★★ и выше")):
        rows = [
            e
            for e in data["entries"]
            if e["correct"] is not None
            and (e["strength"] >= 3 if level == 3 else e["strength"] == level)
        ]
        if not rows:
            continue
        text = f"{label} {sum(e['correct'] for e in rows)} из {len(rows)}"
        priced = [e for e in rows if e["market_probability"] is not None]
        if level == 3 and priced:
            coverage = "" if len(priced) == len(rows) else f" из {len(priced)} с кэфами"
            text += f" (рынок ждал {sum(e['market_probability'] for e in priced):.1f}{coverage})"
        buckets.append(text)
    if buckets:
        lines.append("👉 По звёздам: " + ", ".join(buckets))
    miss = max(
        (e for e in data["entries"] if e["correct"] is False),
        key=lambda e: e["probability"],
        default=None,
    )
    if miss:
        p = miss["prediction"]
        opponent = p.draft.dire_name if p.favorite == "radiant" else p.draft.radiant_name
        lines.append(
            f"👉 Промах недели: {esc(p.favorite_name, 40)} {p.favorite_probability:.0%} {stars(p.favorite_probability)} против {esc(opponent, 40)}, карта {p.draft.map_number or '?'} ({p.draft.observed_at.astimezone(MOSCOW):%d.%m})"
        )
    if data["comeback"]:
        c = data["comeback"]
        opponent = f" против {esc(c['opponent'], 40)}" if c.get("opponent") else ""
        date = f" ({c['date']})" if c.get("date") else ""
        lines.append(
            f"👉 Камбэк недели: {esc(c['team'], 40)}{opponent}, карта {c.get('map_number') or '?'}{date}, по ходу рынок давал им {c['probability']:.0%}"
        )
    cumulative = data["cumulative"]
    lines.append(
        f"ℹ️ По одним героям {data['draft_wins']} из {data['draft_count']}. С запуска {cumulative['wins']} из {cumulative['settled']}"
    )
    if data.get("late_analysis"):
        lines.append("⏱ Разборы с задержкой · отдельный учёт")
    if data.get("additional"):
        extra = data["additional"]
        lines.append(f"⏱ С задержкой отдельно: {extra['wins']} из {extra['settled']}")
    if data.get("demo"):
        lines.insert(0, "<b>ДЕМОНСТРАЦИЯ · все данные условные</b>")
    return "\n".join(lines)


def daily_report_hour(value):
    """Legacy evening settings now mean one final report after midnight."""
    return 0 if int(value) >= 12 else int(value)


def render_report(data):
    c = Canvas()
    d = c.draw
    moment = datetime.fromisoformat(data["date"])
    months = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    weekdays = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
    weekly = "недели" in data["label"].lower() and bool(timestamp(data.get("until")))
    end = timestamp(data["until"]).astimezone(MOSCOW) - timedelta(seconds=1) if weekly else None
    calendar_week = (
        weekly and moment.weekday() == 0 and end.date() == moment.date() + timedelta(days=6)
    )
    c.header(
        data["label"].lower() + " · Dota 2",
        f"{moment:%d.%m} — {end:%d.%m}" if weekly else f"{moment.day} {months[moment.month - 1]}",
        ("пн — вс" if calendar_week else "7 дней") if weekly else weekdays[moment.weekday()],
    )
    c.panel((56, 148, 1224, 282), CYAN)
    c.text(80, 171, "ИГРОКИ + ДРАФТ", 17, bold=True, middle=True)
    if data.get("late_analysis"):
        c.text(360, 171, "ОТДЕЛЬНЫЙ УЧЁТ", 13, GOLD, True, width=330, middle=True)
    c.text(
        80,
        218,
        f"{data['wins']} / {data['settled']}",
        77,
        CYAN,
        True,
        width=640,
        fit=True,
        middle=True,
    )
    c.text(
        80, 263, f"{data['wins']} зашло · {data['losses']} мимо", 17, MUTED, width=640, middle=True
    )
    rate = f"{data['accuracy']:.0%}" if data["accuracy"] is not None else "—"
    c.text(1200, 171, "ТОЧНОСТЬ", 14, MUTED, True, right=True, middle=True)
    c.text(1200, 233, rate, 85, bold=True, width=365, fit=True, right=True, middle=True)
    c.panel((56, 300, 1224, 342))
    c.text(80, 321, "ТОЛЬКО ДРАФТ · для сравнения", 15, MUTED, True, width=410, middle=True)
    count, wins = data["draft_count"], data["draft_wins"]
    c.bar(315, wins / count if count else 0, 530, 440, (AMBER, SUBTLE))
    c.text(1200, 321, f"{wins} / {count}", 21, AMBER, True, right=True, width=190, middle=True)
    c.text(
        80,
        362,
        f"КАРТЫ · опубликовано {data['published']}",
        14,
        MUTED,
        True,
        width=560,
        middle=True,
    )
    c.text(690, 362, "ИГРОКИ + ДРАФТ", 13, CYAN, True, middle=True)
    c.text(978, 362, "ТОЛЬКО ДРАФТ", 13, AMBER, True, middle=True)
    for index, entry in enumerate(data["entries"][:5]):
        center = 400 + index * 46
        if index % 2 == 0:
            d.rounded_rectangle((56, center - 21, 1224, center + 21), radius=9, fill=SUBTLE)
        c.text(80, center, f"{index + 1:02d}", 13, MUTED, middle=True)
        teams, separator, map_number = entry["label"].rpartition(" · карта ")
        c.text(
            118,
            center,
            teams if separator else entry["label"],
            17,
            bold=True,
            width=478,
            middle=True,
        )
        if separator:
            c.text(
                646, center, f"#{map_number}", 15, MUTED, True, right=True, width=42, middle=True
            )
        for x, correct, name, probability, color in (
            (690, entry["correct"], entry["favorite"], entry["probability"], CYAN),
            (
                978,
                entry["draft_correct"],
                entry["draft_favorite"],
                entry["draft_probability"],
                AMBER,
            ),
        ):
            c.result(x, center - 11.5, correct)
            c.text(x + 33, center, name, 14, bold=True, width=155, middle=True)
            c.text(
                x + 238,
                center,
                f"{probability:.0%}",
                18,
                color,
                True,
                right=True,
                width=58,
                middle=True,
            )
    if not data["entries"]:
        c.text(
            80,
            423,
            "За этот период опубликованных прогнозов пока нет",
            22,
            MUTED,
            width=1100,
            middle=True,
        )
    more = (
        f"Ещё {len(data['entries']) - 5} карт — в постах выше"
        if len(data["entries"]) > 5
        else "Все карты этого периода"
    )
    c.text(56, 624, more, 14, MUTED, width=620, middle=True)
    status = ""
    if data["pending"]:
        status = f"Ожидают результата: {data['pending']} · предварительно"
    elif data.get("demo"):
        status = "ДЕМОНСТРАЦИЯ · ДАННЫЕ УСЛОВНЫЕ"
    elif data.get("additional"):
        extra = data["additional"]
        status = f"Отдельный учёт: {extra['wins']}/{extra['settled']}"
    if status:
        c.text(
            1224,
            624,
            status,
            13,
            GOLD if data.get("demo") or data["pending"] else MUTED,
            right=True,
            width=500,
            middle=True,
        )
    c.panel((56, 649, 1224, 703), fill=SURFACE)
    total = data["cumulative"]
    c.text(
        80,
        676,
        f"С начала: {total['wins']}/{total['settled']} · только драфт {total['draft_wins']}/{total['draft_count']}",
        17,
        bold=True,
        width=790,
        middle=True,
    )
    c.stars(906, 668, 3, 15)
    c.text(
        1200,
        676,
        f"и выше: {total['strong_wins']}/{total['strong_count']}",
        15,
        right=True,
        width=160,
        middle=True,
    )
    return c.png()


def demo_report():
    from dataclasses import replace

    from .demo import demo_prediction

    p = demo_prediction()
    entries = []
    for i, probability in enumerate((0.56, 0.56, 0.63, 0.69, 0.62, 0.71, 0.58)):
        own = replace(p, radiant_probability=probability, draft_probability=0.53 + (i % 2) * 0.02)
        entries.append(
            {
                "match_id": f"DEMO-{i}",
                "label": f"Команда Север — Команда Юг · карта {1 + i % 3}",
                "favorite": "Команда Север",
                "probability": probability,
                "strength": strength(probability)[0],
                "correct": i != 0,
                "draft_favorite": "Команда Север",
                "draft_probability": own.draft_probability,
                "draft_strength": 0,
                "draft_correct": i != 1,
                "market_probability": 0.65,
                "against_market": False,
                "actual": {},
                "prediction": own,
            }
        )
    return {
        "label": "Итоги дня",
        "date": utcnow().astimezone(MOSCOW).date().isoformat(),
        "entries": entries,
        "cumulative": totals(entries),
        "comeback": None,
        "demo": True,
        **totals(entries),
    }
