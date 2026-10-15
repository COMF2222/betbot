import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from .domain import Draft, Prediction, utcnow
from .timing import separate_timing


class Store:
    """Single-process durable ledger. Never overwrite the content of a forecast."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS leagues(id INTEGER PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS matches(id TEXT PRIMARY KEY, draft TEXT NOT NULL, seen_at TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', result TEXT);
            CREATE TABLE IF NOT EXISTS forecasts(id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT NOT NULL REFERENCES matches(id), payload TEXT NOT NULL, created_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'preview', chat_id INTEGER, topic_id INTEGER, message_id INTEGER, result_sent INTEGER NOT NULL DEFAULT 0, result_message_id INTEGER);
            CREATE UNIQUE INDEX IF NOT EXISTS forecast_active ON forecasts(match_id) WHERE state!='superseded';
            CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY AUTOINCREMENT, forecast_id INTEGER REFERENCES forecasts(id), kind TEXT NOT NULL, destination INTEGER NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, message_id INTEGER, error TEXT);
            CREATE TABLE IF NOT EXISTS reports(period TEXT NOT NULL, destination INTEGER NOT NULL, state TEXT NOT NULL, message_id INTEGER, PRIMARY KEY(period, destination));
            CREATE TABLE IF NOT EXISTS health(source TEXT PRIMARY KEY, checked_at TEXT NOT NULL, ok INTEGER NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quote_history(id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT NOT NULL, observed_at TEXT NOT NULL, quotes TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS quote_history_match ON quote_history(match_id,observed_at);
            CREATE TABLE IF NOT EXISTS destinations(chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL, chat_type TEXT NOT NULL, topic_id INTEGER, enabled INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS delivery_destination ON deliveries(forecast_id,kind,destination,state);
            CREATE TABLE IF NOT EXISTS tournaments(id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, league_id INTEGER, watch_requested INTEGER NOT NULL DEFAULT 0, discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tournament_notices(tournament_id INTEGER NOT NULL REFERENCES tournaments(id), admin_id INTEGER NOT NULL, state TEXT NOT NULL, message_id INTEGER, decision TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(tournament_id,admin_id));
            CREATE TABLE IF NOT EXISTS paper_sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL CHECK(owner_id>0), initial_cents INTEGER NOT NULL, stake_cents INTEGER NOT NULL, cash_cents INTEGER NOT NULL, started_at TEXT NOT NULL, min_forecast_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, notifications INTEGER NOT NULL DEFAULT 1, closed_at TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS paper_current_owner ON paper_sessions(owner_id) WHERE closed_at IS NULL;
            CREATE TABLE IF NOT EXISTS paper_bets(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES paper_sessions(id), forecast_id INTEGER NOT NULL REFERENCES forecasts(id), match_id TEXT NOT NULL, snapshot TEXT NOT NULL, stake_cents INTEGER NOT NULL, odds TEXT, quote_at TEXT, created_at TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', payout_cents INTEGER NOT NULL DEFAULT 0, settled_at TEXT, UNIQUE(session_id,forecast_id), UNIQUE(session_id,match_id));
            CREATE TABLE IF NOT EXISTS paper_notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, bet_id INTEGER NOT NULL REFERENCES paper_bets(id), kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'planned', created_at TEXT NOT NULL, message_id INTEGER, error TEXT NOT NULL DEFAULT '', not_before TEXT NOT NULL DEFAULT '', UNIQUE(bet_id,kind));
            CREATE TABLE IF NOT EXISTS paper_rule_versions(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES paper_sessions(id), min_forecast_id INTEGER NOT NULL, bands TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS paper_rule_versions_session ON paper_rule_versions(session_id,min_forecast_id,id);
            CREATE TABLE IF NOT EXISTS result_checks(match_id TEXT PRIMARY KEY REFERENCES matches(id),checked_at TEXT NOT NULL);
        """)
        if "topic_id" not in {r[1] for r in self.db.execute("PRAGMA table_info(deliveries)")}:
            with self.db:
                self.db.execute("BEGIN")
                self.db.execute("ALTER TABLE deliveries ADD COLUMN topic_id INTEGER")
                self.db.execute(
                    "UPDATE deliveries SET topic_id=(SELECT topic_id FROM forecasts WHERE forecasts.id=deliveries.forecast_id)"
                )
        # A process crash between sending and saving is ambiguous. Do not resend automatically.
        with self.db:
            self.db.execute("UPDATE deliveries SET state='uncertain' WHERE state='sending'")
            self.db.execute("UPDATE forecasts SET state='uncertain' WHERE state='sending'")
            self.db.execute("UPDATE reports SET state='uncertain' WHERE state='sending'")
            self.db.execute("UPDATE tournament_notices SET state='uncertain' WHERE state='sending'")
            self.db.execute(
                "UPDATE paper_notifications SET state='uncertain' WHERE state='sending'"
            )

    def destination(self, chat_id, title, chat_type, topic_id=None, enabled=True):
        with self.db:
            self.db.execute(
                "INSERT INTO destinations VALUES (?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,chat_type=excluded.chat_type,topic_id=excluded.topic_id,enabled=excluded.enabled",
                (
                    chat_id,
                    title,
                    chat_type,
                    topic_id if chat_type != "channel" else None,
                    int(enabled),
                ),
            )

    def destinations(self, enabled_only=False):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM destinations"
                + (" WHERE enabled=1" if enabled_only else "")
                + " ORDER BY chat_type='channel',chat_id"
            )
        ]

    def toggle_destination(self, chat_id):
        with self.db:
            self.db.execute("UPDATE destinations SET enabled=1-enabled WHERE chat_id=?", (chat_id,))

    def plan_publication(self, forecast_id, destinations):
        with self.db:
            row = self.forecast(forecast_id)
            existing = self.db.execute(
                "SELECT 1 FROM deliveries WHERE forecast_id=? AND kind='forecast' LIMIT 1",
                (forecast_id,),
            ).fetchone()
            if row and row["state"] == "preview" and not existing:
                self.db.executemany(
                    "INSERT INTO deliveries(forecast_id,kind,destination,state,created_at,topic_id) VALUES (?,'forecast',?,'planned',?,?)",
                    [
                        (forecast_id, target["chat_id"], utcnow().isoformat(), target["topic_id"])
                        for target in destinations
                    ],
                )
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM deliveries WHERE forecast_id=? AND kind='forecast' AND state IN ('planned','failed') ORDER BY id",
                (forecast_id,),
            )
        ]

    def publications(self, forecast_id=None, limit=500):
        query = "SELECT f.*,d.destination AS destination,d.topic_id AS destination_topic_id,d.message_id AS post_id,m.result FROM deliveries d JOIN forecasts f ON f.id=d.forecast_id JOIN matches m ON m.id=f.match_id WHERE d.kind='forecast' AND d.state='sent'"
        args = []
        if forecast_id is not None:
            query += " AND f.id=?"
            args.append(forecast_id)
        query += " ORDER BY f.id DESC,d.id LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(query, args)]

    def pending_publications(self):
        return [
            r[0]
            for r in self.db.execute(
                "SELECT DISTINCT forecast_id FROM deliveries WHERE kind='forecast' AND state IN ('planned','failed')"
            )
        ]

    def delivery_sent(self, forecast_id, kind, destination):
        return bool(
            self.db.execute(
                "SELECT 1 FROM deliveries WHERE forecast_id=? AND kind=? AND destination=? AND state='sent'",
                (forecast_id, kind, destination),
            ).fetchone()
        )

    def close(self) -> None:
        self.db.close()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def audit(self, action: str, detail: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO audit(created_at,action,detail) VALUES (?,?,?)",
                (utcnow().isoformat(), action, detail),
            )

    def league(self, league_id: int, name: str, enabled: bool | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO leagues(id,name) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (league_id, name),
            )
            if enabled is not None:
                self.db.execute(
                    "UPDATE leagues SET enabled=? WHERE id=?", (int(enabled), league_id)
                )

    def leagues(self, enabled_only: bool = False) -> list[dict]:
        query = (
            "SELECT * FROM leagues"
            + (" WHERE enabled=1" if enabled_only else "")
            + " ORDER BY name"
        )
        return [dict(row) for row in self.db.execute(query)]

    def enabled(self, league_id: int) -> bool:
        row = self.db.execute("SELECT enabled FROM leagues WHERE id=?", (league_id,)).fetchone()
        return bool(row and row[0])

    def remember_tournament(self, event):
        now = utcnow().isoformat()
        with self.db:
            previous = self.db.execute(
                "SELECT * FROM tournaments WHERE source_key=?", (event["source_key"],)
            ).fetchone()
            league_id = event.get("league_id") or (previous["league_id"] if previous else None)
            self.db.execute(
                "INSERT INTO tournaments(source_key,payload,league_id,discovered_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET payload=excluded.payload,league_id=excluded.league_id,updated_at=excluded.updated_at",
                (event["source_key"], json.dumps(event, ensure_ascii=False), league_id, now, now),
            )
            if league_id:
                self.db.execute(
                    "INSERT INTO leagues(id,name) VALUES (?,?) ON CONFLICT(id) DO NOTHING",
                    (league_id, event["name"]),
                )
                if previous and previous["watch_requested"] and not previous["league_id"]:
                    self.db.execute("UPDATE leagues SET enabled=1 WHERE id=?", (league_id,))

    def tournament(self, identity):
        row = self.db.execute("SELECT * FROM tournaments WHERE id=?", (identity,)).fetchone()
        if not row:
            return None
        value = {
            **json.loads(row["payload"]),
            **{k: row[k] for k in ("id", "league_id", "watch_requested")},
        }
        value["enabled"] = (
            self.enabled(value["league_id"])
            if value["league_id"]
            else bool(value["watch_requested"])
        )
        return value

    def tournaments(self):
        ids = [r[0] for r in self.db.execute("SELECT id FROM tournaments ORDER BY id")]
        return [self.tournament(identity) for identity in ids]

    def watch_tournament(self, identity, enabled=True):
        event = self.tournament(identity)
        if not event:
            return None
        with self.db:
            self.db.execute(
                "UPDATE tournaments SET watch_requested=? WHERE id=?", (int(enabled), identity)
            )
            if event["league_id"]:
                self.db.execute(
                    "UPDATE leagues SET enabled=? WHERE id=?", (int(enabled), event["league_id"])
                )
        return self.tournament(identity)

    def tournament_notice(self, identity, admin):
        row = self.db.execute(
            "SELECT * FROM tournament_notices WHERE tournament_id=? AND admin_id=?",
            (identity, admin),
        ).fetchone()
        return dict(row) if row else None

    def rearm_distant_tournament_notices(self, until):
        """Old wide-window alerts may be offered once when their month arrives."""
        distant = [
            row["id"]
            for row in self.db.execute("SELECT id,payload FROM tournaments")
            if json.loads(row["payload"])["start_date"] > until
        ]
        with self.db:
            self.db.executemany(
                "UPDATE tournament_notices SET state='planned' WHERE tournament_id=? AND state='sent' AND decision IS NULL",
                [(identity,) for identity in distant],
            )

    def begin_tournament_notice(self, identity, admin, repeat=False):
        with self.db:
            row = self.tournament_notice(identity, admin)
            if row and (
                row["state"] in ("sending", "uncertain")
                or not repeat
                and (row["state"] not in ("planned", "failed") or row["decision"])
            ):
                return False
            self.db.execute(
                "INSERT INTO tournament_notices(tournament_id,admin_id,state,updated_at) VALUES (?,?,'sending',?) ON CONFLICT(tournament_id,admin_id) DO UPDATE SET state='sending',updated_at=excluded.updated_at",
                (identity, admin, utcnow().isoformat()),
            )
        return True

    def finish_tournament_notice(self, identity, admin, state, message_id=None):
        with self.db:
            self.db.execute(
                "UPDATE tournament_notices SET state=?,message_id=COALESCE(?,message_id),updated_at=? WHERE tournament_id=? AND admin_id=?",
                (state, message_id, utcnow().isoformat(), identity, admin),
            )

    def decide_tournament(self, identity, admin, decision):
        with self.db:
            self.db.execute(
                "INSERT INTO tournament_notices(tournament_id,admin_id,state,decision,updated_at) VALUES (?,?,'planned',?,?) ON CONFLICT(tournament_id,admin_id) DO UPDATE SET decision=excluded.decision,updated_at=excluded.updated_at",
                (identity, admin, decision, utcnow().isoformat()),
            )

    def observe(self, draft: Draft, status: str, reason: str = "") -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO matches(id,draft,seen_at,status,reason) VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET draft=excluded.draft,seen_at=excluded.seen_at,
                status=CASE WHEN matches.result IS NULL THEN excluded.status ELSE matches.status END,
                reason=CASE WHEN matches.result IS NULL THEN excluded.reason ELSE matches.reason END""",
                (
                    draft.match_id,
                    json.dumps(draft.to_dict(), ensure_ascii=False),
                    utcnow().isoformat(),
                    status,
                    reason,
                ),
            )

    def match(self, match_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        return dict(row) if row else None

    def draft(self, match_id: str) -> Draft | None:
        row = self.match(match_id)
        return Draft.from_dict(json.loads(row["draft"])) if row else None

    def mark(self, match_id: str, status: str, reason: str = "") -> None:
        with self.db:
            self.db.execute(
                "UPDATE matches SET status=?,reason=? WHERE id=? AND result IS NULL",
                (status, reason, match_id),
            )

    def add_forecast(self, p: Prediction) -> int | None:
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO forecasts(match_id,payload,created_at) VALUES (?,?,?)",
                (
                    p.draft.match_id,
                    json.dumps(p.to_dict(), ensure_ascii=False),
                    utcnow().isoformat(),
                ),
            )
        return cursor.lastrowid if cursor.rowcount else None

    def forecast(self, forecast_id: int) -> dict | None:
        row = self.db.execute(
            "SELECT f.*,m.result FROM forecasts f JOIN matches m ON m.id=f.match_id WHERE f.id=?",
            (forecast_id,),
        ).fetchone()
        return dict(row) if row else None

    def has_forecast(self, match_id: str) -> bool:
        return bool(
            self.db.execute(
                "SELECT 1 FROM forecasts WHERE match_id=? AND state!='superseded'", (match_id,)
            ).fetchone()
        )

    def supersede_preview(self, match_id: str) -> bool:
        with self.db:
            cursor = self.db.execute(
                "UPDATE forecasts SET state='superseded' WHERE match_id=? AND state='preview'",
                (match_id,),
            )
        return bool(cursor.rowcount)

    def forecasts(self, state: str | None = None, limit: int = 30) -> list[dict]:
        query = "SELECT f.*,m.result FROM forecasts f JOIN matches m ON m.id=f.match_id"
        args = []
        if state:
            query += " WHERE f.state=?"
            args.append(state)
        query += " ORDER BY f.id DESC LIMIT ?"
        args.append(limit)
        return [dict(row) for row in self.db.execute(query, args)]

    def published_between(self, since: str, until: str, destination=None) -> list[dict]:
        query = "SELECT f.*,m.result FROM forecasts f JOIN matches m ON m.id=f.match_id WHERE f.state='published' AND f.created_at>=? AND f.created_at<?"
        args = [since, until]
        if destination is not None:
            query += " AND EXISTS(SELECT 1 FROM deliveries d WHERE d.forecast_id=f.id AND d.kind='forecast' AND d.state IN ('sent','archived') AND d.destination=?)"
            args.append(destination)
        query += " ORDER BY f.created_at,f.id"
        return [dict(row) for row in self.db.execute(query, args)]

    def save_quotes(self, match_id: str, quotes, observed_at=None) -> None:
        if not quotes:
            return
        with self.db:
            self.db.execute(
                "INSERT INTO quote_history(match_id,observed_at,quotes) VALUES(?,?,?)",
                (
                    match_id,
                    (observed_at or utcnow()).isoformat(),
                    json.dumps([q.to_dict() for q in quotes], ensure_ascii=False),
                ),
            )

    def quote_snapshots(self, match_id: str) -> list[dict]:
        return [
            {"observed_at": row["observed_at"], "quotes": json.loads(row["quotes"])}
            for row in self.db.execute(
                "SELECT observed_at,quotes FROM quote_history WHERE match_id=? ORDER BY observed_at",
                (match_id,),
            )
        ]

    def unresolved(self) -> list[str]:
        return [
            row[0]
            for row in self.db.execute(
                "SELECT id FROM matches WHERE result IS NULL AND (status!='disabled' OR EXISTS(SELECT 1 FROM forecasts f WHERE f.match_id=matches.id)) ORDER BY seen_at"
            )
        ]

    def waiting_quote_drafts(self):
        return [
            Draft.from_dict(json.loads(row[0]))
            for row in self.db.execute(
                "SELECT m.draft FROM matches m JOIN leagues l ON l.id=json_extract(m.draft,'$.league_id') "
                "WHERE m.status='waiting_quotes' AND m.result IS NULL AND l.enabled=1 "
                "AND NOT EXISTS(SELECT 1 FROM forecasts f WHERE f.match_id=m.id AND f.state!='superseded') "
                "ORDER BY m.seen_at,m.id LIMIT 8"
            )
        ]

    def result_candidates(self, limit=8, now=None) -> list[str]:
        threshold = ((now or utcnow()) - timedelta(seconds=60)).isoformat()
        return [
            row[0]
            for row in self.db.execute(
                "SELECT m.id FROM matches m LEFT JOIN result_checks c ON c.match_id=m.id "
                "WHERE m.result IS NULL AND (m.status!='disabled' OR EXISTS(SELECT 1 FROM forecasts f WHERE f.match_id=m.id)) "
                "AND (c.checked_at IS NULL OR c.checked_at<=?) "
                "ORDER BY COALESCE(c.checked_at,''),EXISTS(SELECT 1 FROM forecasts f WHERE f.match_id=m.id) DESC,m.seen_at,m.id LIMIT ?",
                (threshold, limit),
            )
        ]

    def result_checked(self, match_id, now=None):
        with self.db:
            self.db.execute(
                "INSERT INTO result_checks VALUES (?,?) ON CONFLICT(match_id) DO UPDATE SET checked_at=excluded.checked_at",
                (match_id, (now or utcnow()).isoformat()),
            )

    def finished(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id,draft,result FROM matches WHERE result IS NOT NULL"
            )
        ]

    def result(self, match_id: str, value: dict) -> None:
        with self.db:
            self.db.execute(
                "UPDATE matches SET result=?,status='finished' WHERE id=? AND result IS NULL",
                (json.dumps(value), match_id),
            )

    def begin_delivery(
        self, forecast_id: int, kind: str, destination: int, topic_id=None
    ) -> int | None:
        with self.db:
            existing = self.db.execute(
                "SELECT * FROM deliveries WHERE forecast_id=? AND kind=? AND destination=? ORDER BY id DESC LIMIT 1",
                (forecast_id, kind, destination),
            ).fetchone()
            if existing and existing["state"] in ("sending", "sent", "uncertain", "archived"):
                return None
            if kind == "forecast":
                row = self.forecast(forecast_id)
                if not row or row["state"] == "superseded":
                    return None
                self.db.execute(
                    "UPDATE forecasts SET state=CASE WHEN state='published' THEN state ELSE 'sending' END,chat_id=COALESCE(chat_id,?),topic_id=CASE WHEN chat_id IS NULL THEN ? ELSE topic_id END WHERE id=?",
                    (destination, topic_id, forecast_id),
                )
            if existing:
                self.db.execute(
                    "UPDATE deliveries SET state='sending',topic_id=? WHERE id=?",
                    (topic_id, existing["id"]),
                )
                return existing["id"]
            cursor = self.db.execute(
                "INSERT INTO deliveries(forecast_id,kind,destination,state,created_at,topic_id) VALUES (?,?,?,'sending',?,?)",
                (forecast_id, kind, destination, utcnow().isoformat(), topic_id),
            )
            return cursor.lastrowid

    def finish_delivery(
        self, delivery_id: int, state: str, message_id: int | None = None, error: str = ""
    ) -> None:
        row = self.db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if not row:
            return
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET state=?,message_id=?,error=? WHERE id=?",
                (state, message_id, error, delivery_id),
            )
            if row["kind"] == "forecast":
                states = {
                    r[0]
                    for r in self.db.execute(
                        "SELECT state FROM deliveries WHERE forecast_id=? AND kind='forecast'",
                        (row["forecast_id"],),
                    )
                }
                fstate = (
                    "published"
                    if "sent" in states
                    else "sending"
                    if "sending" in states
                    else "uncertain"
                    if "uncertain" in states
                    else "preview"
                )
                self.db.execute(
                    "UPDATE forecasts SET state=?,message_id=CASE WHEN chat_id=? THEN ? ELSE message_id END WHERE id=?",
                    (fstate, row["destination"], message_id, row["forecast_id"]),
                )
                if state == "sent":
                    self.db.execute(
                        "UPDATE matches SET status='published',reason='' WHERE id=(SELECT match_id FROM forecasts WHERE id=?) AND result IS NULL",
                        (row["forecast_id"],),
                    )
            elif row["kind"] == "result" and state == "sent":
                complete = not self.db.execute(
                    "SELECT 1 FROM deliveries f WHERE f.forecast_id=? AND f.kind='forecast' AND f.state='sent' AND NOT EXISTS(SELECT 1 FROM deliveries r WHERE r.forecast_id=f.forecast_id AND r.destination=f.destination AND r.kind='result' AND r.state='sent')",
                    (row["forecast_id"],),
                ).fetchone()
                self.db.execute(
                    "UPDATE forecasts SET result_sent=?,result_message_id=? WHERE id=?",
                    (int(complete), message_id, row["forecast_id"]),
                )

    def confirm_delivery(
        self, forecast_id: int, kind: str, message_id: int, destination=None
    ) -> bool:
        rows = self.db.execute(
            "SELECT id,destination FROM deliveries WHERE forecast_id=? AND kind=? AND state='uncertain' ORDER BY id DESC",
            (forecast_id, kind),
        ).fetchall()
        rows = [r for r in rows if destination is None or r["destination"] == destination]
        if len(rows) != 1:
            return False
        self.finish_delivery(rows[0]["id"], "sent", message_id)
        self.audit("confirm_delivery", f"{forecast_id}:{kind}:{message_id}")
        return True

    def health(self, source: str, ok: bool, detail: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO health VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET checked_at=excluded.checked_at,ok=excluded.ok,detail=excluded.detail",
                (source, utcnow().isoformat(), int(ok), detail[:500]),
            )

    def health_rows(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM health ORDER BY source")]

    def uncertain(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT forecast_id,kind,destination FROM deliveries WHERE state='uncertain' ORDER BY id DESC LIMIT 5"
            )
        ]

    def recent_matches(self, limit: int = 15) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id,status,reason,seen_at FROM matches ORDER BY seen_at DESC LIMIT ?",
                (limit,),
            )
        ]

    def report_state(self, period: str, destination: int):
        row = self.db.execute(
            "SELECT state FROM reports WHERE period=? AND destination=?", (period, destination)
        ).fetchone()
        return row[0] if row else None

    def begin_report(self, period: str, destination: int) -> bool:
        with self.db:
            row = self.db.execute(
                "SELECT state FROM reports WHERE period=? AND destination=?", (period, destination)
            ).fetchone()
            if row and row[0] != "failed":
                return False
            self.db.execute(
                "INSERT INTO reports(period,destination,state) VALUES (?,?,'sending') ON CONFLICT(period,destination) DO UPDATE SET state='sending'",
                (period, destination),
            )
        return True

    def finish_report(self, period: str, destination: int, state: str, message_id=None):
        with self.db:
            self.db.execute(
                "UPDATE reports SET state=?,message_id=? WHERE period=? AND destination=?",
                (state, message_id, period, destination),
            )

    def summary(self, since: str | None = None, until: str | None = None) -> dict:
        since = since or (utcnow() - timedelta(days=7)).isoformat()
        until = until or utcnow().isoformat()
        rows = [
            dict(row)
            for row in self.db.execute(
                "SELECT f.*,m.result FROM forecasts f JOIN matches m ON m.id=f.match_id WHERE f.state='published' AND f.created_at>=? AND f.created_at<?",
                (since, until),
            )
        ]

        def count(late):
            published = wins = settled = 0
            for row in rows:
                payload = json.loads(row["payload"])
                if separate_timing(Prediction.from_dict(payload)) != late:
                    continue
                published += 1
                result = json.loads(row["result"]) if row["result"] else None
                if result and isinstance(result.get("radiant_win"), bool):
                    settled += 1
                    wins += (payload["radiant_probability"] >= 0.5) == result["radiant_win"]
            return {
                "published": published,
                "settled": settled,
                "wins": wins,
                "losses": settled - wins,
                "pending": published - settled,
            }

        skips = self.db.execute(
            "SELECT COUNT(*) FROM matches WHERE reason!='' AND seen_at>=? AND seen_at<?",
            (since, until),
        ).fetchone()[0]
        return {
            **count(False),
            "late": count(True),
            "skipped": skips,
        }
