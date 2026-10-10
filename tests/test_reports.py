import io
from dataclasses import replace
from datetime import timedelta

from PIL import Image

from betbot.domain import Quote, utcnow
from betbot.reports import demo_report, market_pair, render_report, report_caption, report_payload
from betbot.storage import Store


def published(store, prediction, result):
    store.observe(prediction.draft, "observed")
    identity = store.add_forecast(prediction)
    delivery = store.begin_delivery(identity, "forecast", -100)
    store.finish_delivery(delivery, "sent", 123)
    store.result(prediction.draft.match_id, result)


def test_day_metrics_follow_saved_forecasts_and_market_pair_coverage(tmp_path, prediction):
    store = Store(tmp_path / "db.sqlite3")
    now = utcnow()
    quotes = [
        Quote(
            "Polymarket",
            prediction.draft.match_id,
            "map_winner",
            side,
            odds,
            now,
            None,
            2,
            rules="one",
            gross=True,
        )
        for side, odds in (("radiant", 2.5), ("dire", 1.5))
    ]
    p = replace(prediction, quotes=quotes, draft_probability=0.45)
    published(store, p, {"radiant_win": True, "duration": 1800})
    data = report_payload(
        store, (now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat()
    )
    assert data["wins"] == 1 and data["draft_wins"] == 0
    assert data["market_count"] == 1 and data["against_count"] == 1 and data["against_wins"] == 1
    assert abs(data["market_expected"] - 0.375) < 1e-6
    assert data["comeback"] is None
    store.close()


def test_market_probabilities_do_not_mix_different_rules(prediction):
    now = utcnow()
    a = Quote("PARI", "1", "map_winner", "radiant", 2, now, now, 2, rules="rulesA")
    b = replace(a, outcome="dire", rules="rulesB")
    assert market_pair([a, b]) is None


def test_comeback_only_uses_paired_prices_observed_during_actual_game(
    tmp_path, prediction, monkeypatch
):
    store = Store(tmp_path / "db.sqlite3")
    now = utcnow()
    start = now - timedelta(hours=1)
    finish = start + timedelta(minutes=30)
    published(
        store,
        prediction,
        {"radiant_win": True, "start_time": int(start.timestamp()), "duration": 1800},
    )
    for when, radiant_odds in (
        (start - timedelta(minutes=1), 100),
        (start + timedelta(minutes=10), 10),
        (finish + timedelta(minutes=1), 200),
    ):
        monkeypatch.setattr("betbot.storage.utcnow", lambda when=when: when)
        store.save_quotes(
            prediction.draft.match_id,
            [
                Quote(
                    "Polymarket",
                    prediction.draft.match_id,
                    "map_winner",
                    side,
                    odds,
                    when,
                    None,
                    2,
                    rules="one",
                    gross=True,
                )
                for side, odds in (("radiant", radiant_odds), ("dire", 1.1))
            ],
        )
    data = report_payload(
        store, (now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat()
    )
    assert data["comeback"]["odds"] == 10
    assert data["comeback"]["team"] == prediction.draft.radiant_name
    assert "наблюдали" in report_caption(data)
    store.close()


def test_prices_for_another_map_are_excluded_from_market_comparison(tmp_path, prediction):
    store = Store(tmp_path / "db.sqlite3")
    now = utcnow()
    quotes = [
        Quote("Polymarket", prediction.draft.match_id, "map_winner", side, 2, now, None, 1)
        for side in ("radiant", "dire")
    ]
    published(store, replace(prediction, quotes=quotes), {"radiant_win": True})
    data = report_payload(
        store, (now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat()
    )
    assert data["market_count"] == 0
    store.close()


def test_missing_quotes_are_not_fabricated_and_demo_is_explicit(tmp_path, prediction):
    store = Store(tmp_path / "db.sqlite3")
    published(store, prediction, {"radiant_win": False})
    now = utcnow()
    data = report_payload(
        store, (now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat()
    )
    assert data["market_count"] == 0 and data["market_expected"] == 0
    assert "Сравнение с рынком появится" in report_caption(data)
    demo = demo_report()
    assert demo["wins"] == 6 and demo["settled"] == 7 and demo["demo"]
    assert "ДЕМОНСТРАЦИЯ" in report_caption(demo)
    assert len(report_caption(demo)) < 1024
    with Image.open(io.BytesIO(render_report(demo))) as image:
        assert image.size == (1280, 720)
    store.close()


def test_channel_report_counts_only_posts_delivered_to_that_channel(tmp_path, prediction):
    store = Store(tmp_path / "db.sqlite3")
    now = utcnow()
    published(store, prediction, {"radiant_win": True})
    second = replace(prediction, draft=replace(prediction.draft, match_id="second"))
    published(store, second, {"radiant_win": False})
    identity = store.forecasts()[0]["id"]
    delivery = store.begin_delivery(identity, "forecast", -200)
    store.finish_delivery(delivery, "sent", 234)
    data = report_payload(
        store,
        (now - timedelta(days=1)).isoformat(),
        (now + timedelta(days=1)).isoformat(),
        destination=-200,
    )
    assert data["published"] == data["settled"] == data["losses"] == 1
    assert data["cumulative"]["settled"] == 1
    assert store.summary()["published"] == 2
    store.close()
