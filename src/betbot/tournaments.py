"""Liquipedia tournament discovery; market times never replace tournament dates."""

import asyncio
import re
import time
from datetime import date, timedelta
from html.parser import HTMLParser
from urllib.parse import unquote

import httpx

from .domain import timestamp, utcnow
from .presentation import MOSCOW
from .providers import ProviderError, normalized

LIQUIPEDIA_API = "https://liquipedia.net/dota2/api.php"
USER_AGENT = "Betbot/0.1 (https://github.com/COMF2222/betbot; contact: https://github.com/COMF2222/betbot/issues)"
MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def tournament_notice_window(now=None):
    today = (now or utcnow()).astimezone(MOSCOW).date()
    next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    return today.isoformat(), (next_month - timedelta(days=1)).isoformat()


def calendar_dates(text):
    """Dates printed by Liquipedia's tournament tables, including year boundaries."""
    text = " ".join(text.split())
    text = re.sub(r"[–—−�]", "-", text)
    token = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})"
    match = re.fullmatch(token + r",?\s+(\d{4})", text)
    if match:
        month, day, year = match.groups()
        start = date(int(year), MONTHS[month], int(day))
        return start, start
    match = re.fullmatch(
        token
        + r"(?:,?\s+(\d{4}))?\s*-\s*(?:(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+)?(\d{1,2}),?\s+(\d{4})",
        text,
    )
    if not match:
        return None
    sm, sd, sy, em, ed, ey = match.groups()
    em = em or sm
    sy = int(sy or (int(ey) - (MONTHS[sm] > MONTHS[em])))
    return date(sy, MONTHS[sm], int(sd)), date(int(ey), MONTHS[em], int(ed))


class CalendarParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.cells = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self.cells = []
        elif tag == "td" and self.cells is not None:
            self.cell = {"text": "", "links": [], "class": attrs.get("class", "")}
        elif tag == "a" and self.cell is not None:
            self.cell["links"].append(attrs.get("href", ""))

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.cells.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.cells is not None:
            self.rows.append(self.cells)
            self.cells, self.cell = None, None


def parse_calendar(html, tier, now=None):
    parser = CalendarParser()
    parser.feed(html)
    today = (now or utcnow()).astimezone(MOSCOW).date()
    events = {}
    for cells in parser.rows:
        title = next((c for c in cells if "column__tournament" in c["class"]), None)
        if not title:
            continue
        dates = None
        for cell in cells:
            try:
                dates = calendar_dates(cell["text"])
            except ValueError:
                continue
            if dates:
                break
        if not dates or dates[1] < today or dates[0] > today + timedelta(days=180):
            continue
        link = next((a for a in title["links"] if a.startswith("/dota2/")), None)
        if not link:
            continue
        page = unquote(link.removeprefix("/dota2/")).split("#")[0].replace("_", " ")
        events[page] = {
            "source_key": "liquipedia:" + page,
            "page": page,
            "name": " ".join(title["text"].split()),
            "tier": tier,
            "start_date": dates[0].isoformat(),
            "end_date": dates[1].isoformat(),
            "source": "https://liquipedia.net" + link,
            "tier_source": "Liquipedia",
        }
    return list(events.values())


def infobox_fields(content):
    # Only read the infobox, not tier/league fields from results or nested event listings.
    match = re.search(r"\{\{Infobox (?:league|tournament)\b", content, re.I)
    if not match:
        return {}
    start, depth, end = match.start(), 0, None
    for token in re.finditer(r"\{\{|\}\}", content[start:]):
        depth += 1 if token[0] == "{{" else -1
        if depth == 0:
            end = start + token.end()
            break
    if end is None:
        return {}
    return dict(
        re.findall(
            r"^\s*\|\s*(liquipediatier|leagueid|sdate|edate)\s*=\s*([^\n|{}]*)",
            content[start:end],
            re.M | re.I,
        )
    )


class Liquipedia:
    def __init__(self, http):
        self.http = http
        self.lock = asyncio.Lock()
        self.last_request = None
        self.last_parse = None

    async def query(self, **params):
        async with self.lock:
            delay = 0
            if self.last_request is not None:
                delay = max(delay, 2.1 - (time.monotonic() - self.last_request))
            if params.get("action") == "parse" and self.last_parse is not None:
                delay = max(delay, 30.1 - (time.monotonic() - self.last_parse))
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_request = time.monotonic()
            if params.get("action") == "parse":
                self.last_parse = self.last_request
            # Shared HTTP.get retries too quickly for Liquipedia's parse rate limit.
            # A failed request is retried by the next catalog refresh with the same limiter.
            try:
                response = await self.http.client.get(
                    LIQUIPEDIA_API,
                    params={"format": "json", **params},
                    headers={"User-Agent": USER_AGENT},
                )
                response.raise_for_status()
                raw = response.json()
            except (httpx.HTTPError, ValueError):
                raise ProviderError("Недоступен Liquipedia API") from None
            if not isinstance(raw, dict) or raw.get("error"):
                raise ProviderError("Liquipedia API не вернул календарь")
            return raw

    async def tournaments(self, now=None):
        now = now or utcnow()
        candidates = {}
        for tier in (1, 2):
            raw = await self.query(action="parse", page=f"Tier_{tier}_Tournaments", prop="text")
            try:
                html = raw["parse"]["text"]["*"]
            except (KeyError, TypeError):
                raise ProviderError("Изменилась схема календаря Liquipedia") from None
            for event in parse_calendar(html, tier, now):
                candidates[event["page"]] = event
        if not candidates:
            raise ProviderError("Не удалось прочитать предстоящие турниры Liquipedia")
        # Batched revisions confirm each event's own tier, dates and actual Dota league ID.
        events = []
        pages = list(candidates)
        for offset in range(0, len(pages), 50):
            raw = await self.query(
                action="query",
                prop="revisions",
                rvprop="content",
                rvslots="main",
                titles="|".join(pages[offset : offset + 50]),
                redirects=1,
                formatversion=2,
            )
            aliases = {r["to"]: r["from"] for r in raw.get("query", {}).get("redirects", [])}
            for row in raw.get("query", {}).get("pages", []):
                page = row.get("title", "")
                candidate = candidates.get(page) or candidates.get(aliases.get(page))
                if not candidate:
                    continue
                try:
                    content = row["revisions"][0]["slots"]["main"]["content"]
                    fields = infobox_fields(content)
                    tier = int(fields.get("liquipediatier", "").strip())
                    start = date.fromisoformat(fields["sdate"].strip())
                    end = date.fromisoformat(fields["edate"].strip())
                    league = fields.get("leagueid", "").strip()
                    league_id = int(league) if league.isdigit() and int(league) > 0 else None
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                today = now.astimezone(MOSCOW).date()
                if (
                    tier not in (1, 2)
                    or end < today
                    or start > today + timedelta(days=180)
                    or end < start
                ):
                    continue
                events.append(
                    {
                        **candidate,
                        "tier": tier,
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                        "league_id": league_id,
                    }
                )
        return events


def enrich_markets(tournaments, markets, now=None):
    now = now or utcnow()
    result = [dict(event) for event in tournaments]
    for market in markets:
        start = timestamp(market.get("startTime"))
        metadata = market.get("eventMetadata") or {}
        league = normalized(str(metadata.get("league") or ""))
        if not start or not league or market.get("closed") or market.get("ended"):
            continue
        if not market.get("live") and start < now - timedelta(hours=12):
            continue
        description = str(metadata.get("serie") or "") + " " + str(metadata.get("tournament") or "")
        qualifier = bool(re.search(r"qualifier|play.in", description, re.I))
        matching = [
            event
            for event in result
            if league in normalized(event["name"])
            and qualifier == bool(re.search(r"qualifier|play.in", event["name"], re.I))
            and event["start_date"]
            <= start.astimezone(MOSCOW).date().isoformat()
            <= event["end_date"]
        ]
        if len(matching) != 1:
            continue
        event = matching[0]
        event["market_keywords"] = [str(metadata["league"])]
        if not event.get("next_match_at") or start.isoformat() < event["next_match_at"]:
            event["next_match_at"] = start.isoformat()
            event["polymarket_url"] = "https://polymarket.com/event/" + str(market.get("slug", ""))
        event["live_match"] = bool(event.get("live_match") or market.get("live"))
    return result


def event_status(event, activity=None, now=None):
    now = now or utcnow()
    day = now.astimezone(MOSCOW).date().isoformat()
    if day > event["end_date"]:
        return "Завершён"
    played = timestamp((activity or {}).get(str(event.get("league_id")), {}).get("last_match_at"))
    if event.get("live_match") or (
        played
        and played.astimezone(MOSCOW).date().isoformat() >= event["start_date"]
        and played <= now
    ):
        return "Уже начался"
    if day > event["start_date"]:
        return "Уже начался"
    return "Начинается сегодня" if day == event["start_date"] else "Скоро начнётся"
