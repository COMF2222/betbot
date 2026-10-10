"""Tournament navigation based on dates and observed games, never on league ID order."""

import json
from datetime import timedelta
from pathlib import Path

from .domain import timestamp, utcnow
from .presentation import MOSCOW
from .providers import normalized


def calendar_events(path: Path | None = None) -> list[dict]:
    path = path or Path("config/tournament-calendar.json")
    if not path.exists():
        path = Path(__file__).with_name("resources") / "tournament-calendar.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["events"]
    except (OSError, ValueError, KeyError):
        return []


def activity_snapshot(live, recent, now=None) -> dict:
    now = now or utcnow()
    result = {}
    for row in recent:
        moment = timestamp(row.get("start_time"))
        identity = row.get("leagueid")
        if identity and moment and now - timedelta(days=7) <= moment <= now:
            entry = result.setdefault(str(identity), {})
            entry["last_match_at"] = max(entry.get("last_match_at", ""), moment.isoformat())
    for draft in live:
        moment = draft.source_updated_at
        if draft.active and moment and 0 <= (now - moment).total_seconds() <= 180:
            result.setdefault(str(draft.league_id), {})["live_updated_at"] = moment.isoformat()
    return result


def league_rows(catalog, activity, events, view="current", query="", now=None):
    now = now or utcnow()
    day = now.astimezone(MOSCOW).date().isoformat()
    scheduled = {int(e["league_id"]): e for e in events}
    rows = []
    for original in catalog:
        row = dict(original)
        event = scheduled.get(row["id"], {})
        observations = activity.get(str(row["id"]), {})
        live = timestamp(observations.get("live_updated_at"))
        played = timestamp(observations.get("last_match_at"))
        on_calendar = bool(event and event["start_date"] <= day <= event["end_date"])
        is_live = bool(live and 0 <= (now - live).total_seconds() <= 180)
        today = bool(
            played and played <= now and played.astimezone(MOSCOW).date().isoformat() == day
        )
        if is_live:
            status, rank = "Сейчас есть матч", 0
        elif on_calendar:
            status, rank = "Идёт турнир", 1
        elif today:
            status, rank = "Сегодня были матчи", 2
        elif played and played <= now and now - played <= timedelta(days=7):
            status, rank = f"Последний матч {played.astimezone(MOSCOW):%d.%m}", 3
        elif event and day > event["end_date"]:
            status, rank = "Завершён", 4
        elif event and day < event["start_date"]:
            status, rank = "Предстоящий", 4
        else:
            status, rank = "Нет текущих матчей", 5
        current = rank <= 2
        if view == "current" and not current:
            continue
        if view == "selected" and not row["enabled"]:
            continue
        if query and normalized(query) not in normalized(row["name"]) and query != str(row["id"]):
            continue
        row.update(
            status=status,
            current=current,
            calendar=event,
            calendar_active=on_calendar,
            last_match_at=observations.get("last_match_at"),
            rank=rank,
        )
        if on_calendar:
            row["name"] = event["name"]
        rows.append(row)
    return sorted(
        rows,
        key=lambda r: (
            r["rank"],
            -int(timestamp(r["last_match_at"]).timestamp()) if r["last_match_at"] else 0,
            r["name"],
        ),
    )
