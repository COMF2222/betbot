"""A short morning schedule from confirmed tournament matches on Polymarket."""

from .domain import timestamp
from .presentation import MOSCOW, esc
from .providers import normalized


def scheduled_series(events, rules, now):
    now = now.astimezone(MOSCOW)
    selected = {}
    for event in events:
        start = timestamp(event.get("startTime"))
        teams = [str(t.get("name") or "") for t in event.get("teams", [])]
        if event.get("closed") or len(teams) != 2 or not all(teams) or not start:
            continue
        start = start.astimezone(MOSCOW)
        if start.date() != now.date() or start < now:
            continue
        matching = [
            rule
            for rule in rules
            if any(
                normalized(keyword) in normalized(event.get("title", ""))
                for keyword in rule["keywords"]
            )
        ]
        if len(matching) != 1:
            continue
        rule = matching[0]
        key = (rule["league_id"], start.isoformat(), tuple(sorted(normalized(t) for t in teams)))
        selected[key] = {
            "league": rule["name"],
            "teams": teams,
            "start": start,
            "playoffs": "playoff" in event.get("title", "").casefold(),
        }
    return sorted(selected.values(), key=lambda row: row["start"])


def brief_caption(series, previous=None):
    if not series:
        return None
    count = len(series)
    noun = (
        "серия"
        if count % 10 == 1 and count % 100 != 11
        else "серии"
        if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14
        else "серий"
    )
    leagues = list(dict.fromkeys(row["league"] for row in series))
    label = leagues[0] if len(leagues) == 1 else "Dota 2"
    first = series[0]
    lines = [
        f"⚡️ <b>Сегодня {esc(label, 70)}: {count} {noun}, первая в {first['start']:%H:%M} МСК</b>",
        "",
        f"👉 Главная: {esc(first['teams'][0], 40)} vs {esc(first['teams'][1], 40)} в {first['start']:%H:%M}"
        + (", плей-офф." if first["playoffs"] else "."),
    ]
    if previous and previous["settled"]:
        lines[-1] += f" Вчера бот {previous['wins']} из {previous['settled']}."
    delayed = (previous or {}).get("late", {})
    if delayed.get("settled"):
        lines.append(f"⏱ Вчера в отдельном учёте: {delayed['wins']} из {delayed['settled']}.")
    if len(series) > 1:
        lines.append(
            "Ещё: "
            + "; ".join(
                f"{esc(r['teams'][0], 30)} — {esc(r['teams'][1], 30)} в {r['start']:%H:%M}"
                for r in series[1:4]
            )
            + "."
        )
    lines.append("ℹ️ Прогноз на каждую карту после драфта.")
    return "\n".join(lines)
