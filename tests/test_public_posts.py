from dataclasses import replace
from datetime import timedelta

from betbot.domain import Quote, utcnow
from betbot.presentation import caption, result_text


def prices(prediction, now, favorite_odds=1.52, opponent_odds=2.7):
    return [
        Quote(
            "Polymarket",
            prediction.draft.match_id,
            "map_winner",
            side,
            odds,
            now,
            None,
            prediction.draft.map_number,
            rules="same-map",
            market_id="market-2",
            gross=True,
        )
        for side, odds in (("radiant", favorite_odds), ("dire", opponent_odds))
    ]


def test_public_post_is_short_and_uses_real_hero_statistics(prediction):
    now = utcnow()
    p = replace(
        prediction,
        radiant_probability=0.59,
        draft_probability=0.52,
        quotes=prices(prediction, now),
        features={"player_elo": 0.4},
        analysis={
            "used_in_model": True,
            "counters": [{"hero": 33, "enemy": 54, "count": 80, "win_rate": 0.7125}],
        },
    )
    before = p.to_dict()
    text = caption(
        p, heroes={33: {"localized_name": "Enigma"}, 54: {"localized_name": "Lifestealer"}}
    )
    assert "Бот за" in text and "карта 2" in text and "59%" in text and "★" in text
    assert "Игроки сильнее, Enigma бьёт Lifestealer в 71% игр" in text
    assert "80 карт" not in text and "По одним героям ровно" in text
    assert "по истории" not in text and "Рейтинг" not in text
    assert "1.52" in text and "2.70" in text
    assert len(text) < 650 and len([line for line in text.splitlines() if line]) == 3
    assert all(
        word not in text for word in ("Безубыточный", "Версия", "временной тест", "Длительность")
    )
    assert p.to_dict() == before


def test_small_samples_and_wrong_map_prices_are_not_presented_as_evidence(prediction):
    now = utcnow()
    p = replace(
        prediction,
        quotes=[replace(q, map_number=5) for q in prices(prediction, now)],
        analysis={"counters": [{"hero": 33, "enemy": 54, "count": 2, "win_rate": 1.0}]},
    )
    text = caption(p)
    assert "100%" not in text and "1.52" not in text and "недоступен" in text


def test_result_uses_original_price_and_only_observed_in_game_extremes(prediction):
    now = utcnow()
    p = replace(
        prediction,
        quotes=prices(prediction, now),
        duration_seconds=None,
        draft=replace(prediction.draft, observed_at=now),
    )
    snapshots = [
        {"observed_at": when.isoformat(), "quotes": [q.to_dict() for q in prices(p, when, odds)]}
        for when, odds in ((now + timedelta(minutes=3), 2.04), (now + timedelta(hours=1), 99.0))
    ]
    result = {"radiant_win": True, "start_time": int(now.timestamp()), "duration": 1800}
    text = result_text(p, result, snapshots)
    assert "Зашло" in text and "карту 2" in text and "кэф 1.52" in text
    assert "наблюдали до 2.04" in text and "99.00" not in text
    assert len(text.splitlines()) == 1


def test_miss_does_not_invent_a_comeback_or_a_market_preference(prediction):
    now = utcnow()
    p = replace(prediction, quotes=prices(prediction, now))
    result = {"radiant_win": False, "start_time": int(now.timestamp()), "duration": 1800}
    text = result_text(p, result)
    assert "Мимо" in text and "бот был за" in text and "Рынок тоже ждал" in text
    assert "падала" not in text
    when = now + timedelta(minutes=5)
    snapshots = [
        {
            "observed_at": when.isoformat(),
            "quotes": [q.to_dict() for q in prices(p, when, 1.03, 33.33)],
        }
    ]
    assert "3% (кэф 33.33)" in result_text(p, result, snapshots)


def test_stale_price_is_omitted_from_new_post_but_retained_in_result(prediction):
    p = replace(prediction, quotes=prices(prediction, utcnow() - timedelta(days=1)))
    assert "1.52" not in caption(p)
    assert "кэф 1.52" in result_text(p, {"radiant_win": True})
