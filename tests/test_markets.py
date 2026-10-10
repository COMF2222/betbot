from dataclasses import replace
from datetime import timedelta

import pytest

from betbot.domain import Quote, best_quotes, utcnow
from betbot.providers import OddsFeed, Polymarket, eligible_draft, parse_live


def quote(**kwargs):
    now = utcnow()
    values = dict(
        source="PARI",
        match_id="1",
        market="map_winner",
        outcome="radiant",
        odds=1.8,
        observed_at=now,
        source_updated_at=now,
        map_number=2,
        rules="rules-v1",
    )
    values.update(kwargs)
    return Quote(**values)


def test_best_price_does_not_mix_maps_lines_rules_or_gross_prices():
    quotes = [
        quote(odds=1.7),
        quote(source="BetBoom", odds=1.9),
        quote(map_number=1, odds=3),
        quote(market="duration_total", outcome="over", line=38.5, odds=2),
        quote(market="duration_total", outcome="over", line=40.5, odds=2.5),
        quote(rules="different", odds=2.1),
        quote(gross=True, odds=2.3),
    ]
    result = best_quotes(quotes, utcnow(), 30)
    assert len(result) == 6
    assert quote(odds=1.7) not in result
    exact = [
        q
        for q in result
        if q.market == "map_winner" and q.map_number == 2 and q.rules == "rules-v1" and not q.gross
    ]
    assert exact[0].source == "BetBoom"


@pytest.mark.parametrize(
    "changes",
    [
        {"active": False},
        {"odds": float("nan")},
        {"odds": float("inf")},
        {"odds": 1},
        {"source_updated_at": utcnow() - timedelta(seconds=60)},
        {"source_updated_at": utcnow() + timedelta(minutes=1)},
    ],
)
def test_inactive_stale_and_nonfinite_quotes_are_excluded(changes):
    assert not quote(**changes).fresh(utcnow(), 30)


def test_only_exact_map_winner_is_accepted():
    base = {
        "acceptingOrders": True,
        "sportsMarketType": "child_moneyline",
        "question": "Dota 2: A vs B - Game 2 Winner",
    }
    assert Polymarket.map_market(base, 2)
    assert not Polymarket.map_market(base, 1)
    assert not Polymarket.map_market({**base, "question": "A vs B - Match Winner"}, 2)
    assert not Polymarket.map_market({**base, "sportsMarketType": "totals"}, 2)
    assert not Polymarket.map_market({**base, "closed": True}, 2)


class FakeHTTP:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, params))
        return self.result


@pytest.mark.parametrize(
    "version,ids,expected",
    [
        ("v1", {"clobTokenIds": '["ctf0","ctf1"]', "positionIds": ["v20", "v21"]}, "ctf0"),
        ("v2", {"clobTokenIds": '["ctf0","ctf1"]', "positionIds": ["v20", "v21"]}, "v20"),
    ],
)
async def test_polymarket_uses_ask_and_correct_protocol_ids(draft, version, ids, expected):
    http = FakeHTTP(
        {
            "asks": [{"price": "0.9", "size": "10"}, {"price": "0.5", "size": "20"}],
            "bids": [{"price": "0.4", "size": "30"}],
        }
    )
    provider = Polymarket(http)
    event = {
        "slug": "test",
        "markets": [
            {
                "id": "7",
                "version": version,
                **ids,
                "question": "Game 2 Winner",
                "sportsMarketType": "child_moneyline",
                "acceptingOrders": True,
                "outcomes": f'["{draft.dire_name}","{draft.radiant_name}"]',
            }
        ],
    }
    quotes = await provider.quotes(draft, event)
    assert http.calls[0][1]["token_id"] == expected
    assert len(quotes) == 2
    assert quotes[0].outcome == "dire"
    assert quotes[0].odds == 2
    assert quotes[0].liquidity == 10
    assert quotes[0].gross


async def test_unconfirmed_map_has_no_market_quotes(draft):
    http = FakeHTTP({})
    assert await Polymarket(http).quotes(replace(draft, map_number=None), {"markets": []}) == []
    assert not http.calls


def test_ambiguous_events_are_not_bound(draft):
    event = {
        "teams": [{"name": draft.radiant_name}, {"name": draft.dire_name}],
        "startTime": draft.observed_at.isoformat(),
    }
    assert Polymarket.find_event(draft, [event]) == event
    assert Polymarket.find_event(draft, [event, {**event, "id": "other"}]) is None


async def test_feed_cannot_freshen_stale_cached_records(draft):
    q = quote(match_id=draft.match_id, source_updated_at=utcnow() - timedelta(hours=1))
    http = FakeHTTP({"quotes": [q.to_dict()]})
    result = await OddsFeed(http, "https://example.com").quotes(draft)
    assert len(result) == 1
    assert not result[0].fresh(utcnow(), 30)


async def test_feed_rejects_wrong_match_and_missing_source_time(draft):
    q = quote(match_id="another")
    missing = quote(match_id=draft.match_id, source_updated_at=None)
    http = FakeHTTP({"quotes": [q.to_dict(), missing.to_dict()]})
    assert await OddsFeed(http, "https://example.com").quotes(draft) == []


def test_live_parser_keeps_players_on_correct_sides():
    raw = {
        "match_id": "123",
        "league_id": 5,
        "team_id_radiant": 1,
        "team_id_dire": 2,
        "team_name_radiant": "A",
        "team_name_dire": "B",
        "last_update_time": int(utcnow().timestamp()),
        "game_time": -50,
        "players": [
            {"team": 0, "team_slot": 1, "account_id": 10, "hero_id": 1},
            {"team": 1, "team_slot": 1, "account_id": 20, "hero_id": 2},
        ],
    }
    draft = parse_live(raw)
    assert draft.radiant[0].account_id == 10
    assert draft.dire[0].account_id == 20
    assert not draft.complete
    assert draft.map_number is None


def test_late_and_incomplete_drafts_do_not_publish(draft):
    assert eligible_draft(draft, 180, 90) is None
    assert eligible_draft(
        replace(draft, source_updated_at=utcnow() - timedelta(minutes=5)), 180, 90
    )
    assert eligible_draft(replace(draft, game_time=60, delay_seconds=120), 180, 90)
    assert eligible_draft(replace(draft, radiant=draft.radiant[:4]), 180, 90)


def test_source_age_is_added_to_game_clock_for_publication(draft):
    old = replace(
        draft, game_time=0, delay_seconds=0, source_updated_at=utcnow() - timedelta(seconds=120)
    )
    assert "опоздал" in eligible_draft(old, 180, 90)
