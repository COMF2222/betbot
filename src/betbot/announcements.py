"""Confirmed wiki metadata and match times for public tournament cards."""

import hashlib
import html
import re
from datetime import datetime, timedelta, timezone

from .domain import timestamp
from .presentation import MOSCOW, esc
from .providers import normalized

MONTHS = (
    "January February March April May June July August September October November December".split()
)
RU_MONTHS = (
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
)
OFFSETS = {
    "UTC": 0,
    "GMT": 0,
    "CET": 1,
    "CEST": 2,
    "EET": 2,
    "EEST": 3,
    "MSK": 3,
    "CST": 8,
    "SGT": 8,
    "KST": 9,
    "JST": 9,
    "PST": -8,
    "PDT": -7,
    "EST": -5,
    "EDT": -4,
    "BRT": -3,
    "PET": -5,
}


def templates(content):
    """Balanced template blocks, including nested opponents and maps."""
    stack = []
    for token in re.finditer(r"\{\{|\}\}", content):
        if token[0] == "{{":
            stack.append(token.start())
        elif stack:
            start = stack.pop()
            yield content[start : token.end()]


def parameters(block):
    raw = block[2:-2]
    parts, start, depth = [], 0, 0
    for token in re.finditer(r"\{\{|\}\}|\[\[|\]\]|\|", raw):
        if token[0] in ("{{", "[["):
            depth += 1
        elif token[0] in ("}}", "]]"):
            depth -= 1
        elif depth == 0:
            parts.append(raw[start : token.start()].strip())
            start = token.end()
    parts.append(raw[start:].strip())
    values, position = {}, 1
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            values[key.strip().casefold()] = value.strip()
        else:
            values[str(position)] = part
            position += 1
    return parts[0].strip().casefold(), values


def plain(value):
    value = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m[2] or m[1], str(value))
    value = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", value)
    return " ".join(html.unescape(re.sub(r"<[^>]*>|'{2,}", " ", value)).split())


def known_team(value):
    value = plain(value)
    return (
        value
        if value
        and normalized(value) not in {"tbd", "tba", "to be determined", "to be announced", "bye"}
        else None
    )


def logo_name(team):
    return hashlib.sha256(normalized(team).encode("utf-8")).hexdigest()[:24] + ".png"


def page_details(content, page, now):
    content = re.sub(r"<!--.*?-->", "", content, flags=re.S)
    info = next(
        (
            args
            for block in templates(content)
            for name, args in [parameters(block)]
            if name in ("infobox league", "infobox tournament")
        ),
        None,
    )
    if not info:
        return None
    start, end = timestamp(info.get("sdate")), timestamp(info.get("edate"))
    if not start or not end or end < start:
        return None
    team_count = int(info["team_number"]) if info.get("team_number", "").isdigit() else None
    prize = info.get("prizepoolusd", "").replace(",", "").replace(" ", "")
    participants, qualifier_slots = [], 0
    for block in templates(content):
        name, args = parameters(block)
        if name != "teamparticipants":
            continue
        for child in templates(block):
            kind, opponent = parameters(child)
            if kind != "opponent":
                continue
            team = known_team(opponent.get("1", ""))
            if team and team not in participants:
                participants.append(team)
            if not team and re.search(
                r"\|method\s*=\s*qual\b", opponent.get("qualification", ""), re.I
            ):
                qualifier_slots += 1
    return {
        "source_key": "liquipedia:" + page,
        "page": page,
        "name": plain(info.get("name") or page),
        "short_name": plain(info.get("tickername") or info.get("name") or page),
        "start_date": start.date().isoformat(),
        "end_date": end.date().isoformat(),
        "league_id": int(info["leagueid"]) if info.get("leagueid", "").isdigit() else None,
        "tier": int(info["liquipediatier"]) if info.get("liquipediatier", "").isdigit() else None,
        "organizer": plain(info.get("organizer", "")),
        "location": plain(info.get("country", "")),
        "online": info.get("type", "").casefold() == "online",
        "team_count": team_count,
        "participants": participants,
        "qualifier_slots": qualifier_slots,
        "prize_usd": int(prize) if prize.isdigit() else None,
        "logo_file": plain(info.get("imagedark") or info.get("image", "")),
        "source": "https://liquipedia.net/dota2/" + page.replace(" ", "_"),
        "verified_at": now.isoformat(),
    }


def match_time(value):
    match = re.fullmatch(
        r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\s*-\s*(\d{1,2}):(\d{2})\s+\{\{Abbr/([A-Z]+)\}\}",
        value.strip(),
    )
    if not match or match[1] not in MONTHS or match[6] not in OFFSETS:
        return None
    try:
        return datetime(
            int(match[3]),
            MONTHS.index(match[1]) + 1,
            int(match[2]),
            int(match[4]),
            int(match[5]),
            tzinfo=timezone(timedelta(hours=OFFSETS[match[6]])),
        ).astimezone(MOSCOW)
    except ValueError:
        return None


def page_matches(content, event):
    result = []
    for block in templates(re.sub(r"<!--.*?-->", "", content, flags=re.S)):
        name, args = parameters(block)
        if name != "match":
            continue
        start = match_time(args.get("date", ""))
        if not start:
            continue
        teams = []
        for side in (1, 2):
            opponent = args.get(f"opponent{side}", "")
            if not opponent.startswith("{{"):
                break
            kind, fields = parameters(opponent)
            if kind != "teamopponent":
                break
            teams.append(known_team(fields.get("1", "")) or "TBD")
        if len(teams) != 2:
            continue
        maps = [int(k[3:]) for k in args if re.fullmatch(r"map[1-5]", k)]
        best_of = int(args["bestof"]) if args.get("bestof", "").isdigit() else max(maps, default=0)
        result.append(
            {
                "league_id": event["league_id"],
                "league": event["short_name"],
                "teams": teams,
                "start": start.isoformat(),
                "finished": args.get("finished", "").lower() in ("true", "1")
                or args.get("winner") in ("1", "2"),
                "best_of": best_of if best_of in (1, 3, 5, 7) else None,
                "source": event["source"],
                "playoffs": False,
                "live": False,
                "market": False,
            }
        )
    return result


def date_range(event):
    start, end = (
        datetime.fromisoformat(event["start_date"]),
        datetime.fromisoformat(event["end_date"]),
    )
    if start.date() == end.date():
        return f"{start.day} {RU_MONTHS[start.month - 1]}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}–{end.day} {RU_MONTHS[end.month - 1]}"
    return f"{start.day} {RU_MONTHS[start.month - 1]} — {end.day} {RU_MONTHS[end.month - 1]}" + (
        f" {end.year}" if start.year != end.year else ""
    )


def prize_label(event):
    return "$" + f"{event['prize_usd']:,}".replace(",", " ") if event.get("prize_usd") else None


def noun(count, forms):
    return (
        forms[2]
        if 11 <= count % 100 <= 14
        else forms[0]
        if count % 10 == 1
        else forms[1]
        if 2 <= count % 10 <= 4
        else forms[2]
    )


def tournament_caption(event):
    count = len(event.get("participants", []))
    headline = f"🏆 <b>{esc(event['name'], 120)}"
    headline += f": уже {count} {noun(count, ('команда', 'команды', 'команд'))}" if count else ""
    if prize_label(event):
        headline += f" · {prize_label(event)}"
    headline += "</b>"
    facts = [date_range(event)]
    if event.get("online"):
        facts.append(
            "онлайн"
            + (
                f" ({esc({'Europe': 'Европа', 'CIS': 'СНГ', 'China': 'Китай'}.get(event['location'], event['location']), 30)})"
                if event.get("location")
                else ""
            )
        )
    elif event.get("location"):
        facts.append(esc(event["location"], 40))
    if event.get("organizer"):
        facts.append("от " + esc(event["organizer"], 60))
    detail = "👉 " + ", ".join(facts) + "."
    remaining = max(0, (event.get("team_count") or count) - count)
    if remaining:
        detail += f" Ещё {remaining} {noun(remaining, ('место', 'места', 'мест'))}" + (
            " через квалы." if event.get("qualifier_slots") == remaining else " пока не определены."
        )
    return "\n\n".join([headline, detail + "\nℹ️ Прогнозы на карты турнира — здесь, после драфта."])
