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


def exact_event(draft):
    return {
        "slug": "zxcursed-alohadance",
        "markets": [
            {
                "id": "map-2",
                "question": "Game 2 Winner",
                "sportsMarketType": "child_moneyline",
                "acceptingOrders": True,
                "outcomes": [draft.radiant_name, draft.dire_name],
                "clobTokenIds": ["radiant-token", "dire-token"],
            },
            {
                "id": "series",
                "question": "Match Winner",
                "sportsMarketType": "moneyline",
                "acceptingOrders": True,
                "outcomes": [draft.radiant_name, draft.dire_name],
                "clobTokenIds": ["series-radiant", "series-dire"],
            },
        ],
    }


async def test_wide_spread_reads_each_tokens_trade_instead_of_shared_book_price(draft):
    calls = []

    class HTTP:
        async def get(self, url, params):
            calls.append((url, params))
            if url.endswith("/book"):
                return {
                    "bids": [{"price": "0.02", "size": "20"}],
                    "asks": [{"price": "0.98", "size": "10"}],
                    # Real incident: both books exposed the SAME side's 4% trade.
                    "last_trade_price": "0.04",
                }
            assert url.endswith("/last-trade-price")
            return {
                "price": "0.98" if params["token_id"] == "radiant-token" else "0.04",
                "side": "BUY",
            }

    quotes = await Polymarket(HTTP()).quotes(draft, exact_event(draft))
    assert len(quotes) == 2
    assert [q.odds for q in quotes] == pytest.approx([1 / 0.98, 25])
    assert all(q.map_number == 2 and q.market_id == "map-2" for q in quotes)
    assert [q.token_id for q in quotes] == ["radiant-token", "dire-token"]
    assert len(calls) == 4
    assert {params["token_id"] for _, params in calls} == {"radiant-token", "dire-token"}


@pytest.mark.parametrize(
    "trade",
    [
        {"price": "0.5", "side": ""},
        {"price": "0.5"},
        {"price": "NaN", "side": "BUY"},
        {"price": "1", "side": "SELL"},
    ],
)
async def test_placeholder_or_invalid_token_trade_does_not_become_initial_odds(draft, trade):
    class HTTP:
        async def get(self, url, params):
            if url.endswith("/book"):
                return {"bids": [], "asks": [], "last_trade_price": "0.5"}
            return trade

    assert await Polymarket(HTTP()).quotes(draft, exact_event(draft)) == []


async def test_inconsistent_token_trades_still_rejected(draft):
    class HTTP:
        async def get(self, url, params):
            if url.endswith("/book"):
                return {"last_trade_price": "0.5"}
            return {"price": "0.99", "side": "BUY"}

    assert await Polymarket(HTTP()).quotes(draft, exact_event(draft)) == []


async def test_narrow_spread_uses_midpoint_without_trade_request(draft):
    class HTTP:
        async def get(self, url, params):
            assert url.endswith("/book")
            return {
                "bids": [{"price": "0.49", "size": "10"}],
                "asks": [{"price": "0.51", "size": "10"}],
                "last_trade_price": "0.04",
            }

    quotes = await Polymarket(HTTP()).quotes(draft, exact_event(draft))
    assert len(quotes) == 2 and all(q.odds == 2 for q in quotes)


async def test_conflicting_stale_trades_use_marked_current_book_midpoints(draft):
    class HTTP:
        async def get(self, url, params):
            radiant = params["token_id"] == "radiant-token"
            if url.endswith("/book"):
                return {
                    "bids": [{"price": "0.72" if radiant else "0.03", "size": "20"}],
                    "asks": [{"price": "0.97" if radiant else "0.28", "size": "10"}],
                    "last_trade_price": "0.72",
                }
            return {"price": "0.98" if radiant else "0.28", "side": "BUY"}

    quotes = await Polymarket(HTTP()).quotes(draft, exact_event(draft))
    assert len(quotes) == 2
    assert [q.odds for q in quotes] == pytest.approx([1 / 0.845, 1 / 0.155])
    assert [q.ask_odds for q in quotes] == pytest.approx([1 / 0.97, 1 / 0.28])
    assert all(q.price_basis == "midpoint_fallback" and q.price_origin == "live" for q in quotes)
    assert all(q.map_number == 2 and q.market_id == "map-2" for q in quotes)
    p = replace(draft_prediction(draft), quotes=quotes)
    assert "ориентир по стакану" in caption(p)


def draft_prediction(draft):
    from betbot.demo import demo_prediction

    return replace(demo_prediction(), draft=draft)


async def test_wide_untraded_placeholder_books_do_not_create_fake_midpoint_prices(draft):
    class HTTP:
        async def get(self, url, params):
            if url.endswith("/book"):
                return {
                    "bids": [{"price": "0.02", "size": "20"}],
                    "asks": [{"price": "0.98", "size": "20"}],
                    "last_trade_price": "0.5",
                }
            return {"price": "0.5", "side": ""}

    assert await Polymarket(HTTP()).quotes(draft, exact_event(draft)) == []


async def test_incoherent_books_cannot_rescue_incoherent_last_trades(draft):
    class HTTP:
        async def get(self, url, params):
            if url.endswith("/book"):
                return {
                    "bids": [{"price": "0.65", "size": "20"}],
                    "asks": [{"price": "0.95", "size": "20"}],
                }
            return {"price": "0.98", "side": "BUY"}

    assert await Polymarket(HTTP()).quotes(draft, exact_event(draft)) == []


def test_price_basis_classes_do_not_mix_in_market_pairs_or_spike_reports(prediction):
    now = utcnow()
    a = Quote(
        "Polymarket",
        prediction.draft.match_id,
        "map_winner",
        "radiant",
        1.5,
        now,
        None,
        2,
        price_kind="market",
    )
    b = replace(a, outcome="dire", odds=3, price_basis="midpoint_fallback")
    assert market_pair([a, b]) is None
    p = replace(prediction, quotes=[a], draft=replace(prediction.draft, observed_at=now))
    when = now + timedelta(minutes=2)
    text = result_text(
        p,
        {"radiant_win": True, "start_time": now.timestamp(), "duration": 1800},
        [
            {
                "observed_at": when.isoformat(),
                "quotes": [
                    replace(a, odds=20, observed_at=when, price_basis="midpoint_fallback").to_dict()
                ],
            }
        ],
    )
    assert "наблюдали до" not in text


@pytest.mark.parametrize("ask,expected", [(None, None), (0.7, "1.43")])
async def test_token_price_only_opens_paper_stake_with_original_buy_price(
    tmp_path, prediction, ask, expected
):
    from betbot.paper_bank import PaperBank
    from betbot.storage import Store

    class HTTP:
        async def get(self, url, params):
            if url.endswith("/book"):
                return {
                    "last_trade_price": "0.04",
                    "asks": [{"price": str(ask), "size": "1000"}] if ask else [],
                }
            return {
                "price": "0.68" if params["token_id"] == "radiant-token" else "0.33",
                "side": "BUY",
            }

    store = Store(tmp_path / "ledger.sqlite3")
    try:
        bank = PaperBank(store)
        bank.start(1, 100000, 10000)
        draft = replace(prediction.draft, observed_at=utcnow())
        quotes = await Polymarket(HTTP()).quotes(draft, exact_event(draft))
        p = replace(prediction, draft=draft, radiant_probability=0.6, quotes=quotes)
        store.observe(draft, "observed")
        identity = store.add_forecast(p)
        delivery = store.begin_delivery(identity, "forecast", -100)
        store.finish_delivery(delivery, "sent", 1)
        bank.capture({1})
        bet = bank.history(1)[0]
        if expected:
            assert bet["state"] == "open" and bet["odds"] == expected
            assert bank.summary(1)["cash_cents"] == 90000
            assert bank.summary(1)["reserved"] == 10000
        else:
            assert bet["state"] == "skipped" and bet["reason"] == "no_initial_buy_price"
            assert bank.summary(1)["cash_cents"] == 100000
            assert bank.summary(1)["reserved"] == 0
    finally:
        store.close()


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
