"""Live, comparable market moves; never change or recompute the original forecast."""

from .domain import Prediction, Quote, utcnow
from .presentation import MOSCOW, esc, stars, tournament_line
from .reports import market_pair


def favorite_quote(p, quotes, max_age, now=None):
    pair = market_pair(
        [
            q
            for q in quotes
            if q.source == "Polymarket"
            and q.market == "map_winner"
            and q.match_id == p.draft.match_id
            and q.map_number == p.draft.map_number
            and q.price_kind == "market"
            and q.price_origin == "live"
            and q.fresh(now or utcnow(), max_age)
        ]
    )
    if not pair or not all(0.02 <= pair[side] <= 0.98 for side in ("radiant", "dire")):
        return None
    return pair["quotes"][p.favorite]


def identity(q):
    key = [
        q.source,
        q.match_id,
        q.map_number,
        q.market,
        q.market_id,
        q.rules,
        q.gross,
        q.price_kind,
    ]
    # Preserve persisted identities of ordinary quotes across this upgrade.
    return key + [q.price_basis] if q.price_basis else key


def comparable_quote(p, quotes, current):
    return next(
        (
            q
            for q in quotes
            if q.outcome == p.favorite
            and identity(q) == identity(current)
            and q.price_origin == "live"
            and 1 < q.odds < float("inf")
        ),
        None,
    )


def increased(previous, current, relative, minimum):
    delta = current - previous
    return delta + 1e-9 >= minimum and delta / previous + 1e-9 >= relative


def alert_text(p: Prediction, previous: float, current: Quote):
    change = current.odds / previous - 1
    basis = " · ориентир по стакану" if current.price_basis == "midpoint_fallback" else ""
    return "\n".join(
        [
            f"📈 <b>Кэф на {esc(p.favorite_name, 45)} вырос</b>, карта {p.draft.map_number}: <b>{previous:.2f} → {current.odds:.2f}</b> (+{change:.0%})",
            tournament_line(p),
            f"ℹ️ {esc(current.source, 25)}{basis} · {current.observed_at.astimezone(MOSCOW):%H:%M} МСК. Исходный прогноз: {p.favorite_probability:.0%}, {stars(p.favorite_probability)}",
        ]
    )
