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
        if (
            event.get("closed")
            or event.get("ended")
            or len(teams) != 2
            or not all(teams)
            or not start
        ):
            continue
        start = start.astimezone(MOSCOW)
        if start.date() != now.date() or (start < now and not event.get("live")):
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
            "league_id": rule["league_id"],
            "live": bool(event.get("live")),
            "market": True,
            "best_of": None,
            "source": "Polymarket",
        }
    return sorted(selected.values(), key=lambda row: row["start"])


def combined_schedule(wiki, market, enabled_ids, now, live_drafts=(), aliases=None):
    """Keep only selected tickets and today's unfinished series; never guess LIVE."""
    aliases = aliases or {}
    live = set()
    for draft in live_drafts:
        updated = draft.source_updated_at
        if draft.active and updated and 0 <= (now - updated).total_seconds() <= 180:
            draft = draft.with_team_aliases(aliases)
            live.add(
                (
                    draft.league_id,
                    tuple(sorted(normalized(t) for t in (draft.radiant_name, draft.dire_name))),
                )
            )
    selected = {}
    for row in [*wiki, *market]:
        start = timestamp(row.get("start"))
        identity = row.get("league_id")
        if (
            identity not in enabled_ids
            or not start
            or start.astimezone(MOSCOW).date() != now.astimezone(MOSCOW).date()
            or row.get("finished")
        ):
            continue
        teams = tuple(sorted(normalized(t) for t in row["teams"]))
        active = bool(row.get("live") or (identity, teams) in live)
        if start < now and not active:
            continue
        key = (identity, teams, start)
        if row.get("market"):
            nearby = [
                candidate
                for candidate in selected
                if candidate[:2] == key[:2] and abs((candidate[2] - start).total_seconds()) <= 900
            ]
            if len(nearby) == 1:
                key = nearby[0]
            elif len(nearby) > 1:
                continue
        # Wiki times/format are primary; attach market coverage and LIVE separately.
        if key in selected:
            selected[key]["market"] |= bool(row.get("market"))
            selected[key]["live"] |= active
        else:
            selected[key] = {
                **row,
                "start": start.astimezone(MOSCOW),
                "live": active,
                "market": bool(row.get("market")),
            }
    return sorted(selected.values(), key=lambda row: (not row["live"], row["start"]))


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
        f"⚡️ <b>Сегодня {esc(label, 70)}: {count} {noun}"
        + (", матч уже идёт" if first.get("live") else f", первая в {first['start']:%H:%M} МСК")
        + "</b>",
        "",
        f"👉 Главная: {esc(first['teams'][0], 40)} vs {esc(first['teams'][1], 40)}"
        + (" уже идёт" if first.get("live") else f" в {first['start']:%H:%M}")
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
