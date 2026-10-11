from dataclasses import replace
from datetime import timedelta

import pytest

from betbot.domain import utcnow
from betbot.model import History, Model, chronological_split, fit


def history_rows(n=300):
    start = int((utcnow() - timedelta(days=30)).timestamp())
    return [
        {
            "match_id": i + 1,
            "start_time": start + i * 7200,
            "radiant_team_id": 1 + (i % 2),
            "dire_team_id": 2 - (i % 2),
            "radiant_win": bool((i * 7) % 11 < 6),
            "duration": 1800 + (i % 9) * 120,
            "leagueid": 123,
            "series_id": 1 + i // 2,
            "heroes": [1, 13, 29, 5, 30, 54, 11, 2, 20, 26],
            "accounts": list(range(1, 11)),
            "slots": [0, 1, 2, 3, 4, 128, 129, 130, 131, 132],
        }
        for i in range(n)
    ]


def test_temporal_split_purges_series_crossing_boundaries():
    rows = history_rows()
    rows[179]["series_id"] = rows[180]["series_id"] = 9999
    rows[239]["series_id"] = rows[240]["series_id"] = 8888
    groups = chronological_split(rows)
    assert all(179 not in g and 180 not in g and 239 not in g and 240 not in g for g in groups)
    for left, right in zip(groups, groups[1:], strict=False):
        assert max(rows[i]["start_time"] + rows[i]["duration"] + 60 for i in left) < min(
            rows[i]["start_time"] for i in right
        )
        assert not {rows[i]["series_id"] for i in left} & {rows[i]["series_id"] for i in right}


@pytest.mark.parametrize("feature_set", ["classic", "signatures"])
def test_fit_and_safe_json_model_roundtrip(tmp_path, draft, feature_set):
    path = tmp_path / "model.json"
    report = fit(history_rows(), path, feature_set=feature_set)
    assert report["rows"] == 300
    assert report["test"] >= 25
    assert 0 <= report["brier"] <= 1
    for name in ("combined", "draft"):
        assert sum(b["count"] for b in report["reliability"][name]) == report["test"]
    assert not report["publication_ready"]  # Fewer than 200 independent test maps.
    model = Model(path)
    p = model.predict(draft)
    assert set(p.analysis["comfort"]) == {"radiant", "dire"}
    assert len(p.analysis["comfort"]["radiant"]) == 5
    assert 0 < p.radiant_probability < 1
    assert p.duration_interval[0] <= p.duration_interval[1]
    assert (
        p.reliability["combined"]["from"]
        <= p.favorite_probability
        < p.reliability["combined"]["to"]
    )
    with pytest.raises(ValueError, match="позже"):
        model.predict(replace(draft, observed_at=utcnow() - timedelta(days=60)))
    with pytest.raises(ValueError, match="истории обучения"):
        model.predict(replace(draft, match_id="1"))


def test_insufficient_training_data_fails_without_saving_model(tmp_path):
    path = tmp_path / "model.json"
    with pytest.raises(ValueError):
        fit(history_rows(20), path)
    assert not path.exists()


def test_features_before_result_do_not_include_that_outcome(draft):
    history = History()
    before = history.features(draft)
    assert before["team_elo"] == 0
    history.update(draft, True)
    assert history.features(draft)["team_elo"] > before["team_elo"]
    assert before["team_elo"] == 0


def test_roster_experiment_detects_replacement_without_using_current_result(draft):
    from betbot.domain import Player

    history = History()
    history.update(draft, True)
    original = history.features(draft, feature_set="roster")
    changed = replace(draft, radiant=(Player(999999, draft.radiant[0].hero_id), *draft.radiant[1:]))
    features = history.features(changed, feature_set="roster")
    assert original["roster_retention"] == 0
    assert features["roster_retention"] < 0
    assert features["team_elo"] == original["team_elo"]
