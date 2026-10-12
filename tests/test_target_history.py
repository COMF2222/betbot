import copy

import pytest
from test_model import history_rows

from betbot.domain import timestamp
from betbot.experiments import build_examples, candidate_partition, feature_view
from betbot.model import chronological_split
from betbot.presentation import MOSCOW
from betbot.target_history import augment_target_features


def scope_for(rows):
    return {
        "events": [
            {
                "league_id": 123,
                "start_date": timestamp(rows[0]["start_time"])
                .astimezone(MOSCOW)
                .date()
                .isoformat(),
                "end_date": timestamp(rows[-1]["start_time"]).astimezone(MOSCOW).date().isoformat(),
            }
        ]
    }


def domain_only(features):
    return [{k: v for k, v in f.items() if k.startswith("tier1:")} for f in features]


def test_non_target_results_never_change_independent_tier1_ratings():
    rows = history_rows(10)
    rows[0]["leagueid"] = 999
    features = build_examples(rows)
    extended = augment_target_features(rows, features, scope_for(rows))
    altered = copy.deepcopy(rows)
    altered[0]["radiant_win"] = not altered[0]["radiant_win"]
    assert domain_only(augment_target_features(altered, features, scope_for(rows))) == domain_only(
        extended
    )
    assert extended[1]["tier1:team_elo"] == 0
    assert extended[2]["tier1:team_elo"] != 0
    assert feature_view(extended[2], "classic") == feature_view(features[2], "classic")
    assert feature_view(extended[2], "compact") == feature_view(features[2], "compact")
    assert "tier1:team_elo" in feature_view(extended[2], "tier1_ratings")


def test_current_result_and_ongoing_prior_map_do_not_change_tier1_context():
    rows = history_rows(3)
    start = rows[0]["start_time"]
    rows[0]["duration"] = 600
    rows[1]["start_time"], rows[2]["start_time"] = start + 660, start + 661
    features = build_examples(rows)
    original = augment_target_features(rows, features, scope_for(rows))
    assert original[1]["tier1:team_elo"] == 0
    assert original[2]["tier1:team_elo"] != 0
    for row in rows[1:]:
        row["radiant_win"] = not row["radiant_win"]
        row["gold10"] = [999999] * 10
        row["duration"] = 12000
    assert domain_only(augment_target_features(rows, features, scope_for(rows))) == domain_only(
        original
    )


def test_training_exclusions_do_not_remove_or_use_any_heldout_test_maps():
    rows = history_rows(300)
    split = chronological_split(rows)
    for i in (0, split[1][0], split[2][0]):
        rows[i]["leagueid"] = 999
    train, calibration = candidate_partition(
        {"exclude_training_leagues": [999], "exclude_calibration_leagues": [999]}, rows, split
    )
    assert train == split[0][1:]
    assert calibration == split[1][1:]
    assert not set(train + calibration) & set(split[2])
    with pytest.raises(ValueError, match="Insufficient"):
        candidate_partition({"exclude_training_leagues": [123, 999]}, rows, split)
