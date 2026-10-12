import io
import json
from dataclasses import replace
from datetime import timedelta

from PIL import Image

from betbot.domain import Quote, utcnow
from betbot.reports import demo_report, market_pair, render_report, report_caption, report_payload
from betbot.storage import Store


def test_weekly_caption_counts_every_confidence_bucket_and_quote_coverage(prediction):
    from betbot.graphics import strength
    from betbot.reports import totals, weekly_caption

    data = demo_report()
    entries = data["entries"][:4]
    for entry, probability, won in zip(
        entries, (0.52, 0.56, 0.60, 0.66), (True, True, False, False), strict=True
    ):
        entry.update(
            probability=probability,
            strength=strength(probability)[0],
            correct=won,
            prediction=replace(prediction, radiant_probability=probability),
        )
    entries[0]["market_probability"] = None
    data.update(entries=entries, **totals(entries))
    text = weekly_caption(data)
    assert "2 из 4 за неделю" in text
    assert "из 3 карт с кэфами" in text
    assert "☆ 1 из 1" in text and "★ 1 из 1" in text and "★★ 0 из 1" in text
    assert "★★★ и выше 0 из 1" in text
    assert "Промах недели" in text and "66%" in text
    assert len(text) < 1024


def test_weekly_comeback_and_render_cover_closed_calendar_week():
    from betbot.reports import weekly_caption

    data = demo_report()
    data.update(
        label="Итоги недели",
        date="2026-10-12",
        until="2026-10-18T21:00:00+00:00",
        comeback={
            "team": "Cloud Dawning",
            "opponent": "Cresent",
            "map_number": 1,
            "date": "10.10",
            "probability": 0.03,
        },
    )
    assert "Камбэк недели: Cloud Dawning против Cresent, карта 1 (10.10)" in weekly_caption(data)
    assert "рынок давал им 3%" in weekly_caption(data)
    assert Image.open(io.BytesIO(render_report(data))).size == (1280, 720)


def published(store, prediction, result):
    store.observe(prediction.draft, "observed")
    identity = store.add_forecast(prediction)
    delivery = store.begin_delivery(identity, "forecast", -100)
    store.finish_delivery(delivery, "sent", 123)
    store.result(prediction.draft.match_id, result)


def test_old_delayed_clock_is_excluded_from_early_totals_without_changing_payload(
    tmp_path, prediction
):
    store = Store(tmp_path / "db.sqlite3")
    p = replace(
        prediction,
        draft=replace(
            prediction.draft,
            game_time=-62,
            delay_seconds=900,
            clock_is_delayed=False,
            source_name="Steam + OpenDota",
        ),
        late_analysis=False,
    )
    published(store, p, {"radiant_win": True})
    original = store.forecast(1)["payload"]
    stats = store.summary()
    assert stats["published"] == 0
    assert stats["late"]["published"] == stats["late"]["wins"] == 1
    assert store.forecast(1)["payload"] == original
    assert json.loads(original)["late_analysis"] is False
    store.close()


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


def test_report_uses_verified_public_names_without_rewriting_forecasts(tmp_path, prediction):
    store = Store(tmp_path / "db.sqlite3")
    published(store, prediction, {"radiant_win": True})
    before = store.forecasts()[0]["payload"]
    store.set("team_aliases", {str(prediction.draft.radiant_id): "Skyreavers"})
    now = utcnow()
    data = report_payload(
        store, (now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat()
    )
    assert data["entries"][0]["favorite"] == "Skyreavers"
    assert "Skyreavers" in data["entries"][0]["label"]
    assert store.forecasts()[0]["payload"] == before
    assert data["settled"] == data["wins"] == 1
    store.close()


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


def test_complete_report_title_and_market_line_are_not_truncated_or_duplicated():
    data = demo_report()
    data["demo"] = False
    data["late_analysis"] = True
    data["label"] = "Предварительные итоги дня · отдельный учёт"
    data["cumulative"] = {key: data[key] for key in data["cumulative"]}
    text = report_caption(data)
    assert "предварительные итоги дня</b>" in text
    assert text.count("Рынок") == 1
    assert len(text) < 1024


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


def test_delayed_analysis_has_separate_daily_and_cumulative_counts(tmp_path, prediction):
    store = Store(tmp_path / "db.sqlite3")
    published(store, prediction, {"radiant_win": True})
    late = replace(prediction, draft=replace(prediction.draft, match_id="late"), late_analysis=True)
    published(store, late, {"radiant_win": False})
    now = utcnow()
    args = (store, (now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat())
    early = report_payload(*args)
    delayed = report_payload(*args, late_analysis=True)
    assert early["published"] == early["wins"] == early["cumulative"]["wins"] == 1
    assert delayed["published"] == delayed["losses"] == delayed["cumulative"]["losses"] == 1
    assert "Статистика отдельно" in report_caption(delayed)
    with Image.open(io.BytesIO(render_report(delayed))) as image:
        assert image.size == (1280, 720)
    store.close()
