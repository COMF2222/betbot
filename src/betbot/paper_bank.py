"""Private, virtual flat-stake accounts. No execution or wallet integrations."""

import json
import re
from dataclasses import replace
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from .domain import Prediction, timestamp, utcnow
from .market_status import MARKET_ISSUES
from .presentation import esc, market_snapshot, tournament_line

SKIPS = {
    **MARKET_ISSUES,
    "no_initial_odds": "при публикации прогноза не удалось получить кэф на эту карту",
    "already_finished": "результат уже известен, новую ставку не открываем",
    "insufficient": "не хватает свободного баланса",
}


def parse_money(value):
    value = str(value).strip()
    if not re.fullmatch(r"\d{1,10}(?:[,.]\d{1,2})?", value):
        raise ValueError("Введите положительную сумму, например 100 или 100,50.")
    amount = Decimal(value.replace(",", "."))
    if not 0 < amount <= 1_000_000_000:
        raise ValueError("Сумма должна быть от 0,01 до 1 000 000 000.")
    return int(amount * 100)


def money(cents):
    return f"{Decimal(cents) / 100:,.2f}".replace(",", " ").replace(".", ",")


def initial_quote(prediction, created_at, max_age=30):
    """Exactly the displayed first market price, never a later attached quote."""
    if not prediction.draft.map_number or not created_at:
        return None
    valid = [
        q
        for q in prediction.quotes
        if q.source == "Polymarket"
        and q.market == "map_winner"
        and q.match_id == prediction.draft.match_id
        and q.map_number == prediction.draft.map_number
        and q.price_kind == "market"
        and q.price_origin == "live"
        and q.line is None
        and q.odds <= 1_000_000
        and q.observed_at
        and q.observed_at <= created_at
        and q.fresh(created_at, max_age)
    ]
    own, _, _ = market_snapshot(replace(prediction, quotes=valid), None)
    return own if own and Decimal(f"{own.odds:.2f}") > 1 else None


class PaperBank:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def session(self, owner, identity=None):
        row = self.db.execute(
            "SELECT * FROM paper_sessions WHERE owner_id=? AND "
            + ("id=?" if identity else "closed_at IS NULL"),
            (owner, identity) if identity else (owner,),
        ).fetchone()
        return dict(row) if row else None

    def start(self, owner, initial_cents, stake_cents, now=None):
        if owner <= 0 or not 0 < stake_cents <= initial_cents <= 100_000_000_000:
            raise ValueError("Ставка должна быть положительной и не больше начального баланса.")
        now = now or utcnow()
        with self.db:
            self.db.execute(
                "UPDATE paper_sessions SET active=0,closed_at=? WHERE owner_id=? AND closed_at IS NULL",
                (now.isoformat(), owner),
            )
            boundary = self.db.execute("SELECT COALESCE(MAX(id),0) FROM forecasts").fetchone()[0]
            cursor = self.db.execute(
                "INSERT INTO paper_sessions(owner_id,initial_cents,stake_cents,cash_cents,started_at,min_forecast_id) VALUES (?,?,?,?,?,?)",
                (owner, initial_cents, stake_cents, initial_cents, now.isoformat(), boundary),
            )
        return cursor.lastrowid

    def change_stake(self, owner, stake_cents):
        if not 0 < stake_cents <= 100_000_000_000:
            raise ValueError("Введите положительную сумму ставки.")
        with self.db:
            self.db.execute(
                "UPDATE paper_sessions SET stake_cents=? WHERE owner_id=? AND closed_at IS NULL",
                (stake_cents, owner),
            )

    def toggle(self, owner, field):
        if field not in ("active", "notifications"):
            raise ValueError("Unknown paper setting")
        with self.db:
            session = self.session(owner)
            if not session:
                return
            self.db.execute(
                f"UPDATE paper_sessions SET {field}=1-{field} WHERE id=?", (session["id"],)
            )
            if field == "active" and not session["active"]:
                boundary = self.db.execute("SELECT COALESCE(MAX(id),0) FROM forecasts").fetchone()[
                    0
                ]
                self.db.execute(
                    "UPDATE paper_sessions SET min_forecast_id=? WHERE id=?",
                    (boundary, session["id"]),
                )
            if field == "notifications" and session["notifications"]:
                self.db.execute(
                    "UPDATE paper_notifications SET state='suppressed' WHERE state='planned' AND bet_id IN (SELECT id FROM paper_bets WHERE session_id=?)",
                    (session["id"],),
                )

    def summary(self, owner, identity=None):
        session = self.session(owner, identity)
        if not session:
            return None
        bets = list(
            self.db.execute(
                "SELECT state,COUNT(*) AS n,COALESCE(SUM(stake_cents),0) AS stakes,COALESCE(SUM(payout_cents),0) AS payouts FROM paper_bets WHERE session_id=? GROUP BY state",
                (session["id"],),
            )
        )
        counts = {row["state"]: row["n"] for row in bets}
        reserved = sum(row["stakes"] for row in bets if row["state"] == "open")
        turnover = sum(row["stakes"] for row in bets if row["state"] in ("won", "lost"))
        equity = session["cash_cents"] + reserved
        return {
            **session,
            "reserved": reserved,
            "equity": equity,
            "profit": equity - session["initial_cents"],
            "counts": counts,
            "turnover": turnover,
        }

    def sessions(self, owner):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id,started_at,closed_at FROM paper_sessions WHERE owner_id=? ORDER BY id DESC LIMIT 10",
                (owner,),
            )
        ]

    def history(self, owner, identity=None, page=0):
        session = self.session(owner, identity)
        if not session:
            return []
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM paper_bets WHERE session_id=? ORDER BY id DESC LIMIT 10 OFFSET ?",
                (session["id"], max(0, page) * 10),
            )
        ]

    def capture(self, owners, now=None, max_quote_age=30):
        now = now or utcnow()
        # Recover an already known payout before checking funds for a new map.
        self.settle(owners, now)
        for session in list(
            self.db.execute("SELECT * FROM paper_sessions WHERE active=1 AND closed_at IS NULL")
        ):
            if session["owner_id"] not in owners:
                continue
            rows = list(
                self.db.execute(
                    "SELECT f.*,m.result FROM forecasts f JOIN matches m ON m.id=f.match_id WHERE f.state='published' AND f.id>? AND NOT EXISTS(SELECT 1 FROM paper_bets b WHERE b.session_id=? AND b.match_id=f.match_id) ORDER BY f.id",
                    (session["min_forecast_id"], session["id"]),
                )
            )
            for row in rows:
                p = Prediction.from_dict(json.loads(row["payload"]))
                quote = initial_quote(p, timestamp(row["created_at"]), max_quote_age)
                reason = (
                    "already_finished" if row["result"] else "no_initial_odds" if not quote else ""
                )
                issue = p.analysis.get("market_status", {}).get("code")
                if reason == "no_initial_odds" and issue in MARKET_ISSUES:
                    reason = issue
                current = self.session(session["owner_id"])
                if not current or current["id"] != session["id"] or not current["active"]:
                    break
                stake = current["stake_cents"]
                if not reason and current["cash_cents"] < stake:
                    reason = "insufficient"
                snapshot = {
                    "prediction": p.to_dict(),
                    "favorite": p.favorite,
                    "quote": quote.to_dict() if quote else None,
                }
                with self.db:
                    cursor = self.db.execute(
                        "INSERT OR IGNORE INTO paper_bets(session_id,forecast_id,match_id,snapshot,stake_cents,odds,quote_at,created_at,state,reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            session["id"],
                            row["id"],
                            row["match_id"],
                            json.dumps(snapshot, ensure_ascii=False),
                            0 if reason else stake,
                            f"{quote.odds:.2f}" if quote else None,
                            quote.observed_at.isoformat() if quote else None,
                            now.isoformat(),
                            "skipped" if reason else "open",
                            reason,
                        ),
                    )
                    if not cursor.rowcount:
                        continue
                    if not reason:
                        self.db.execute(
                            "UPDATE paper_sessions SET cash_cents=cash_cents-? WHERE id=?",
                            (stake, session["id"]),
                        )
                    self._notification(
                        cursor.lastrowid, "open", session["owner_id"], session["id"], now
                    )

    def settle(self, owners, now=None):
        now = now or utcnow()
        rows = list(
            self.db.execute(
                "SELECT b.*,s.owner_id,m.result FROM paper_bets b JOIN paper_sessions s ON s.id=b.session_id JOIN matches m ON m.id=b.match_id WHERE b.state='open' AND m.result IS NOT NULL ORDER BY b.id"
            )
        )
        for row in rows:
            if row["owner_id"] not in owners:
                continue
            outcome = json.loads(row["result"]).get("radiant_win")
            if not isinstance(outcome, bool):
                continue
            favorite = json.loads(row["snapshot"])["favorite"]
            won = outcome == (favorite == "radiant")
            payout = (
                int(
                    (Decimal(row["stake_cents"]) * Decimal(row["odds"])).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                if won
                else 0
            )
            with self.db:
                changed = self.db.execute(
                    "UPDATE paper_bets SET state=?,payout_cents=?,settled_at=? WHERE id=? AND state='open'",
                    ("won" if won else "lost", payout, now.isoformat(), row["id"]),
                )
                if changed.rowcount:
                    self.db.execute(
                        "UPDATE paper_sessions SET cash_cents=cash_cents+? WHERE id=?",
                        (payout, row["session_id"]),
                    )
                    self._notification(row["id"], "result", row["owner_id"], row["session_id"], now)

    def _notification(self, bet_id, kind, owner, session_id, now):
        summary = self.summary(owner, session_id)
        if summary["notifications"]:
            payload = {
                "cash": summary["cash_cents"],
                "reserved": summary["reserved"],
                "equity": summary["equity"],
            }
            self.db.execute(
                "INSERT OR IGNORE INTO paper_notifications(bet_id,kind,payload,created_at) VALUES (?,?,?,?)",
                (bet_id, kind, json.dumps(payload), now.isoformat()),
            )

    def notifications(self, owners, limit=20):
        if not owners:
            return []
        owners = sorted(owners)
        rows = self.db.execute(
            "SELECT n.*,b.snapshot,b.stake_cents,b.odds,b.state AS bet_state,b.reason,b.payout_cents,b.session_id,s.owner_id FROM paper_notifications n JOIN paper_bets b ON b.id=n.bet_id JOIN paper_sessions s ON s.id=b.session_id WHERE n.state='planned' AND n.not_before<=? AND s.notifications=1 AND s.owner_id IN ("
            + ",".join("?" for _ in owners)
            + ") ORDER BY n.id LIMIT ?",
            (utcnow().isoformat(), *owners, limit),
        )
        return [dict(row) for row in rows]

    def begin_notification(self, identity):
        with self.db:
            return bool(
                self.db.execute(
                    "UPDATE paper_notifications SET state='sending' WHERE id=? AND state='planned'",
                    (identity,),
                ).rowcount
            )

    def finish_notification(self, identity, state, message_id=None, error="", retry_after=0):
        with self.db:
            self.db.execute(
                "UPDATE paper_notifications SET state=?,message_id=?,error=?,not_before=? WHERE id=?",
                (
                    state,
                    message_id,
                    error,
                    (utcnow() + timedelta(seconds=retry_after)).isoformat() if retry_after else "",
                    identity,
                ),
            )


def notification_text(row, aliases=None):
    snapshot = json.loads(row["snapshot"])
    p = Prediction.from_dict(snapshot["prediction"])
    p.draft = p.draft.with_team_aliases(aliases or {})
    balance = json.loads(row["payload"])
    title = f"💰 <b>Виртуальный банк #{row['session_id']}</b>"
    match = f"{esc(p.favorite_name, 50)} · карта {p.draft.map_number or '?'}"
    if row["kind"] == "open":
        body = (
            f"⏭ Пропуск: {match}\n{SKIPS[row['reason']]}"
            if row["reason"]
            else f"📝 {match}\nСтавка: <b>{money(row['stake_cents'])}</b> · первый кэф <b>{row['odds']}</b>"
        )
    else:
        profit = row["payout_cents"] - row["stake_cents"]
        body = f"{'✅ Зашло' if row['bet_state'] == 'won' else '❌ Мимо'}: {match}\nИтог ставки: <b>{'+' if profit > 0 else ''}{money(profit)}</b> · кэф {row['odds']}"
    if (snapshot.get("quote") or {}).get("price_basis") == "midpoint_fallback":
        body += " · ориентир по стакану"
    return "\n".join(
        [
            title,
            tournament_line(p),
            "",
            body,
            f"\nБаланс: <b>{money(balance['equity'])}</b> · свободно {money(balance['cash'])}"
            + (
                f"\nВ открытых ставках: {money(balance['reserved'])}" if balance["reserved"] else ""
            ),
        ]
    )
