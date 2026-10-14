import json

import pytest

from betbot.providers import ProviderError, TheOddsAPI


class Catalogue:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def get(self, url, params):
        self.calls.append((url, params))
        return self.response


async def test_key_is_sent_only_to_the_correct_provider_and_full_catalogue_requested():
    http = Catalogue([{"key": "soccer_epl", "title": "EPL", "active": True}])
    report = await TheOddsAPI(http, "private-test-key").coverage()
    assert http.calls == [
        ("https://api.the-odds-api.com/v4/sports", {"apiKey": "private-test-key", "all": "true"})
    ]
    assert report["ok"] and report["connected"] is False
    assert report["sports_count"] == 1 and report["dota_sports"] == []
    assert "отсутствует" in report["note"]
    assert "private-test-key" not in json.dumps(report)


async def test_future_catalogue_support_does_not_pretend_map_markets_are_connected():
    http = Catalogue([{"key": "esports_dota2", "title": "Dota 2", "active": True}])
    report = await TheOddsAPI(http, "private-test-key").coverage()
    assert report["dota_sports"][0]["key"] == "esports_dota2"
    assert report["connected"] is False and "ещё не проверены" in report["note"]


async def test_invalid_catalogue_is_not_reported_as_empty_success():
    with pytest.raises(ProviderError):
        await TheOddsAPI(Catalogue({"error": "bad response"}), "private-test-key").coverage()
