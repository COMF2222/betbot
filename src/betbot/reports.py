"""Daily/weekly visuals derived from the immutable ledger and saved market snapshots."""

import json
import math
from datetime import UTC, datetime, time, timedelta

from .domain import Prediction, Quote, timestamp, utcnow
from .graphics import AMBER, CYAN, GOLD, MUTED, PANEL, Canvas, strength
from .presentation import MOSCOW, esc


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
        key = (q.source, q.match_id, q.map_number, q.rules, q.gross, q.market_id)
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
        return {"radiant": r / (r + d), "dire": d / (r + d), "source": key[0], "quotes": pair}
    return None


def entries_from_rows(rows, late_analysis=False):
    result = []
    for row in rows:
        p = Prediction.from_dict(json.loads(row["payload"]))
        if p.late_analysis != late_analysis:
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
    entries = entries_from_rows(store.published_between(since, until, destination), late_analysis)
    cumulative = totals(
        entries_from_rows(
            store.published_between(
                "1970-01-01T00:00:00+00:00", min(until, utcnow().isoformat()), destination
            ),
            late_analysis,
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
    title = f"{'✅' if data['accuracy'] is not None and data['accuracy'] >= 0.5 else '📊'} <b>{data['wins']} из {total} · {esc(data['label'].lower(), 35)}</b>"
    lines = [title]
    if data.get("late_analysis"):
        lines.append(
            "⏱ Разборы с задержкой / в ходе карты. Статистика отдельно от прогнозов сразу после драфта."
        )
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
            f"👉 По ценам рынка ожидалось примерно {data['market_expected']:.1f} из {data['market_count']} карт с котировками."
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
    if all_time["market_count"]:
        lines.append(
            f"Рынок: примерно {all_time['market_expected']:.1f} из {all_time['market_count']} карт с ценами."
        )
    if data.get("demo"):
        lines.insert(0, "<b>ДЕМОНСТРАЦИЯ · все данные условные</b>")
    return "\n".join(lines)


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
    c.header(
        data["label"].lower() + " · Dota 2",
        f"{moment.day} {months[moment.month - 1]}",
        weekdays[moment.weekday()],
    )
    d.rounded_rectangle((56, 148, 410, 316), radius=18, fill=PANEL)
    c.text(
        80,
        164,
        "ВЕРНЫХ ПОЗДНИХ РАЗБОРОВ" if data.get("late_analysis") else "ВЕРНЫХ ПРОГНОЗОВ",
        13,
        MUTED,
        True,
    )
    number = c.text(76, 260, str(data["wins"]), 83, CYAN, True, width=170, fit=True, baseline=True)
    count_x = number[2] + 16
    c.text(
        count_x,
        260,
        f"/ {data['settled']}",
        42,
        MUTED,
        True,
        width=386 - count_x,
        fit=True,
        baseline=True,
    )
    rate = f"{data['accuracy']:.0%}" if data["accuracy"] is not None else "—"
    c.text(80, 281, f"{rate} точность · {data['losses']} мимо", 19, bold=True, width=305)
    d.rounded_rectangle((434, 148, 1224, 316), radius=18, fill=PANEL)
    for y, title, count, wins, color in (
        (163, "ИГРОКИ + ДРАФТ", data["settled"], data["wins"], CYAN),
        (235, "ТОЛЬКО ДРАФТ", data["draft_count"], data["draft_wins"], AMBER),
    ):
        c.text(460, y, title, 13, MUTED, True)
        c.text(1198, y - 2, f"{wins} / {count}", 20, color, True, right=True)
        c.bar(y + 35, wins / count if count else 0, 460, 738, (color, "#29354b"))
    c.text(56, 330, f"КАРТЫ / ОПУБЛИКОВАНО {data['published']}", 12, MUTED, True)
    c.text(690, 330, "ИГРОКИ + ДРАФТ", 12, CYAN, True)
    c.text(978, 330, "ТОЛЬКО ДРАФТ", 12, AMBER, True)
    for i, entry in enumerate(data["entries"][:5]):
        y = 363 + i * 48
        c.text(56, y, f"{i + 1:02d}", 13, MUTED)
        c.text(92, y - 1, entry["label"], 17, bold=True, width=575)
        for x, correct, name, probability in (
            (690, entry["correct"], entry["favorite"], entry["probability"]),
            (
                978,
                entry["draft_correct"],
                entry["draft_favorite"],
                entry["draft_probability"],
            ),
        ):
            c.result(x, y + 1, correct)
            c.text(x + 33, y, name, 14, bold=True, width=145)
            c.text(x + 238, y, f"{probability:.0%}", 15, MUTED, right=True)
        d.line((56, y + 38, 1224, y + 38), fill="#283449", width=1)
    if not data["entries"]:
        c.text(56, 392, "За этот период опубликованных прогнозов пока нет", 23, MUTED)
    more = (
        f"Ещё {len(data['entries']) - 5} карт — в постах выше"
        if len(data["entries"]) > 5
        else "Все карты этого периода"
    )
    c.text(56, 612, more, 14, MUTED)
    if data["pending"]:
        c.text(
            1224,
            612,
            f"Ожидают результата: {data['pending']} · предварительно",
            14,
            GOLD,
            right=True,
        )
    elif data.get("demo"):
        c.text(1224, 612, "ДЕМОНСТРАЦИЯ / ВСЕ ДАННЫЕ УСЛОВНЫЕ", 12, GOLD, right=True)
    d.rounded_rectangle((56, 649, 1224, 703), radius=12, fill="#1b293f")
    total = data["cumulative"]
    c.text(
        76,
        666,
        f"С начала: игроки + драфт {total['wins']}/{total['settled']} · только драфт {total['draft_wins']}/{total['draft_count']}",
        16,
        bold=True,
        width=795,
    )
    c.stars(904, 670, 3, 14)
    c.text(
        1200,
        666,
        f"и выше: {total['strong_wins']}/{total['strong_count']}",
        15,
        right=True,
        width=178,
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
