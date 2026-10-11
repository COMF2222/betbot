from dataclasses import replace
from datetime import timedelta

import pytest

from betbot.domain import Quote, utcnow
from betbot.presentation import caption, result_text
from betbot.providers import Polymarket
from betbot.reports import market_pair


@pytest.mark.parametrize(
    "bid,ask,trade,expected",
    [
        (0.63, 0.65, 0.9, 0.64),
        (0.1, 0.98, 0.64, 0.64),
        (0.9, 0.1, 0.5, None),
        (None, 0.98, 0.64, 0.64),
        (0.1, 0.98, None, None),
        (0.1, 0.98, float("nan"), None),
    ],
)
def test_displayed_price_follows_polymarket_rule_instead_of_expensive_asks(
    bid, ask, trade, expected
):
    book = {
        "bids": [] if bid is None else [{"price": str(bid), "size": "10"}],
        "asks": [{"price": str(ask), "size": "100"}],
        "last_trade_price": trade,
    }
    selected = Polymarket.displayed_price(book)
    if expected is None:
        assert selected is None
    else:
        assert selected[0] == pytest.approx(expected)
        assert selected[1] == pytest.approx(1 / ask)


def test_market_probability_is_the_displayed_price_and_offer_prices_stay_separate(prediction):
    now = utcnow()
    quotes = [
        Quote(
            "Polymarket",
            prediction.draft.match_id,
            "map_winner",
            side,
            1 / p,
            now,
            None,
            2,
            price_kind="market",
        )
        for side, p in (("radiant", 0.64), ("dire", 0.37))
    ]
    assert market_pair(quotes)["radiant"] == 0.64
    assert market_pair([quotes[0], replace(quotes[1], price_kind="offer")]) is None


async def test_history_keeps_token_map_and_historical_timestamp_and_filters_future_points(
    prediction,
):
    now = utcnow()
    q = Quote(
        "Polymarket",
        prediction.draft.match_id,
        "map_winner",
        "radiant",
        1.5,
        now,
        None,
        2,
        market_id="exact-map-2",
        price_kind="market",
        token_id="token-2",
    )
    calls = []

    class HTTP:
        async def get(self, url, params):
            calls.append((url, params))
            return {
                "history": [
                    {"t": int(when.timestamp()), "p": price}
                    for when, price in (
                        (now - timedelta(minutes=1), 0.1),
                        (now + timedelta(minutes=5), 0.31),
                        (now + timedelta(hours=2), 0.01),
                        (now + timedelta(minutes=10), float("nan")),
                    )
                ]
            }

    history = await Polymarket(HTTP()).history([q], now, now + timedelta(minutes=30))
    assert len(history) == 1 and history[0].odds == pytest.approx(1 / 0.31)
    assert history[0].observed_at < now + timedelta(minutes=6)
    assert history[0].price_origin == "history" and history[0].price_kind == "market"
    assert history[0].market_id == "exact-map-2" and history[0].map_number == 2
    assert calls[0][1]["market"] == "token-2"


def test_later_quote_attachment_does_not_claim_a_forecast_time_price(prediction):
    now = utcnow()
    late = Quote(
        "Polymarket",
        prediction.draft.match_id,
        "map_winner",
        "radiant",
        1.54,
        now,
        None,
        2,
        price_kind="market",
    )
    p = replace(
        prediction,
        draft=replace(prediction.draft, observed_at=now - timedelta(minutes=5)),
        quotes=[late],
    )
    assert "Рынок на " in caption(p, now=now)


def test_result_ignores_offer_price_spikes_when_forecast_uses_market_prices(prediction):
    now = utcnow()
    q = Quote(
        "Polymarket",
        prediction.draft.match_id,
        "map_winner",
        "radiant",
        1.54,
        now,
        None,
        2,
        price_kind="market",
        market_id="m",
    )
    p = replace(prediction, quotes=[q], draft=replace(prediction.draft, observed_at=now))
    when = now + timedelta(minutes=5)
    snapshots = [
        {
            "observed_at": when.isoformat(),
            "quotes": [replace(q, odds=99, observed_at=when, price_kind="offer").to_dict()],
        }
    ]
    text = result_text(
        p, {"radiant_win": True, "start_time": int(now.timestamp()), "duration": 1800}, snapshots
    )
    assert "99" not in text
