import copy
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from betbot.domain import Quote, utcnow
from betbot.paper_bank import PaperBank, notification_text
from betbot.presentation import caption, result_text
from betbot.providers import Polymarket
from betbot.storage import Store


def decider_event(draft, best_of=3):
    tied = best_of // 2
    return {
        "id": "1187492",
        "slug": "dota2-zxcurs-aloha1-2026-10-14",
        "title": f"Dota 2: A vs B (BO{best_of}) - 1win Streamers League #3",
        "seriesSlug": "dota-2",
        "teams": [{"name": draft.dire_name}, {"name": draft.radiant_name}],
        "startTime": draft.observed_at.isoformat(),
        "updatedAt": utcnow().isoformat(),
        "live": True,
        "ended": False,
        "score": f"000-000|{tied}-{tied}|Bo{best_of}",
        "period": f"{best_of}/{best_of}",
        "markets": [
            {
                "id": "5682167",
                "conditionId": "series-condition",
                "question": f"Dota 2: A vs B (BO{best_of}) - 1win Streamers League #3",
                "groupItemTitle": "Match Winner",
                "sportsMarketType": "moneyline",
                "acceptingOrders": True,
                "closed": False,
                "outcomes": [draft.dire_name, draft.radiant_name],
                "clobTokenIds": ["series-dire", "series-radiant"],
            }
        ],
    }


class Books:
    def __init__(self):
        self.calls = []

    async def get(self, url, params):
        self.calls.append((url, params))
        assert url.endswith("/book")
        assert params["token_id"] in ("series-radiant", "series-dire")
        return {
            "bids": [{"price": "0.49", "size": "1000"}],
            "asks": [{"price": "0.51", "size": "1000"}],
        }


@pytest.mark.parametrize("best_of", [1, 3, 5])
async def test_live_verified_decider_uses_series_tokens_with_exact_map_and_proof(draft, best_of):
    d = replace(draft, map_number=best_of)
    event = decider_event(d, best_of)
    original = copy.deepcopy(event)
    http = Books()
    quotes = await Polymarket(http).quotes(d, event)
    assert event == original
    assert len(quotes) == 2
    assert [q.outcome for q in quotes] == ["dire", "radiant"]
    assert all(q.map_number == best_of and q.market == "map_winner" for q in quotes)
    assert all(q.market_id == "5682167" and q.market_basis == "series_decider" for q in quotes)
    assert all(q.market_context["series_score"] == [best_of // 2] * 2 for q in quotes)
    assert all(q.market_context["underlying_market"] == "series_winner" for q in quotes)
    assert all(f":decider:BO{best_of}:map{best_of}" in q.rules for q in quotes)
    assert Quote.from_dict(quotes[0].to_dict()) == quotes[0]


@pytest.mark.parametrize(
    "changes",
    [
        {"score": "000-000|2-0|Bo3"},
        {"score": "000-000|0-0|Bo3"},
        {"score": "1-1"},
        {"score": "000-000|1-1|Bo5"},
        {"period": "2/3"},
        {"period": "3/5"},
        {"period": None},
        {"live": False},
        {"ended": True},
        {"closed": True},
        {"seriesSlug": "counter-strike"},
        {"teams": [{"name": "Other"}, {"name": "Team"}]},
        {"updatedAt": (utcnow() - timedelta(minutes=3)).isoformat()},
        {"updatedAt": (utcnow() + timedelta(minutes=1)).isoformat()},
        {"updatedAt": None},
        {"startTime": (utcnow() - timedelta(days=2)).isoformat()},
        {"title": "A vs B (BO5)"},
    ],
)
async def test_unverified_or_finished_series_cannot_supply_map_prices(draft, changes):
    d = replace(draft, map_number=3)
    event = {**decider_event(d), **changes}
    http = Books()
    assert await Polymarket(http).quotes(d, event) == []
    assert not http.calls


@pytest.mark.parametrize(
    "best_of,map_number,score,period",
    [
        (3, 1, "0-0", "1/3"),
        (3, 2, "1-0", "2/3"),
        (5, 3, "1-1", "3/5"),
        (5, 4, "2-1", "4/5"),
        (5, 5, "3-1", "5/5"),
    ],
)
def test_series_market_is_never_used_for_nondeciding_map(draft, best_of, map_number, score, period):
    d = replace(draft, map_number=map_number)
    event = decider_event(d, best_of)
    event.update(score=f"000-000|{score}|Bo{best_of}", period=period)
    assert Polymarket.select_market(d, event) == (None, "map_market_missing")


def test_exact_map_market_takes_priority_and_closed_map_cannot_be_bypassed(draft):
    d = replace(draft, map_number=3)
    event = decider_event(d)
    exact = {
        **event["markets"][0],
        "id": "exact-3",
        "question": "Game 3 Winner",
        "groupItemTitle": "Game 3 Winner",
        "sportsMarketType": "child_moneyline",
    }
    event["markets"].append(exact)
    assert Polymarket.select_market(d, event)[0]["id"] == "exact-3"
    exact["closed"] = True
    assert Polymarket.select_market(d, event) == (None, "map_market_closed")


def test_ambiguous_series_and_mismatched_outcomes_are_rejected(draft):
    d = replace(draft, map_number=3)
    event = decider_event(d)
    event["markets"].append({**event["markets"][0], "id": "other-series"})
    assert Polymarket.select_market(d, event) == (None, "market_ambiguous")
    event["markets"].pop()
    event["markets"][0]["outcomes"] = ["Other", "Team"]
    assert Polymarket.select_market(d, event) == (None, "market_identity")


async def test_decider_paper_bet_uses_original_buy_price_and_labels_underlying_series(
    tmp_path, prediction
):
    d = replace(prediction.draft, map_number=3)
    event = decider_event(d)
    quotes = await Polymarket(Books()).quotes(d, event)
    p = replace(prediction, draft=d, quotes=quotes)
    assert "рынок серии при 1:1" in caption(p)
    store = Store(tmp_path / "paper.sqlite3")
    try:
        bank = PaperBank(store)
        bank.start(1, 100000, 10000)
        store.observe(d, "observed")
        fid = store.add_forecast(p)
        delivery = store.begin_delivery(fid, "forecast", -100)
        store.finish_delivery(delivery, "sent", 1)
        before = store.forecast(fid)["payload"]
        bank.capture({1})
        bet = bank.history(1)[0]
        assert bet["state"] == "open" and bet["odds"] == "1.96"
        snapshot = json.loads(bet["snapshot"])["quote"]
        assert snapshot["market_basis"] == "series_decider"
        assert snapshot["price_basis"] == "buy_ask"
        assert snapshot["market_context"]["series_score"] == [1, 1]
        assert "Рынок серии при 1:1" in notification_text(bank.notifications({1})[0])
        store.result(d.match_id, {"radiant_win": p.favorite == "radiant"})
        bank.settle({1})
        bank.settle({1})
        assert bank.summary(1)["profit"] == 9600
        assert store.forecast(fid)["payload"] == before
        assert "рынок серии при 1:1" in result_text(p, {"radiant_win": True})
    finally:
        store.close()
