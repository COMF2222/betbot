import copy
from dataclasses import replace

import numpy as np
import pytest
from test_model import history_rows

from betbot.experiments import (
    build_examples,
    feature_view,
    fit_candidate,
    research_features,
    select_candidate,
    swap_features,
    training_weights,
)
from betbot.model import History, chronological_split


def test_current_and_future_outcomes_and_economy_cannot_change_current_features():
    rows = history_rows(10)
    original = build_examples(rows)
    altered = copy.deepcopy(rows)
    for row in altered[4:]:
        row["radiant_win"] = not row["radiant_win"]
        row["gold10"] = [999999] * 10
        row["gpms"] = [9999] * 10
        row["lane_roles"] = [2] * 10
    actual = build_examples(altered)
    assert actual[:5] == original[:5]
    assert actual[5] != original[5]  # A previous completed result may affect the next map.
    assert not any(key in original[4] for key in ("radiant_win", "duration", "gold10", "gpms"))


def test_ongoing_game_is_not_added_until_finish_and_availability_buffer():
    rows = history_rows(3)
    start = rows[0]["start_time"]
    rows[0]["duration"] = 600
    rows[1]["start_time"] = start + 660
    rows[2]["start_time"] = start + 661
    features = build_examples(rows)
    assert features[1]["team_elo"] == 0
    assert features[2]["team_elo"] != 0


def test_research_advantages_reverse_when_teams_swap(draft):
    history = History()
    history.update(draft, True)
    history.analytics.roles = {
        str(p.account_id): [i + 1] * 10
        for side in (draft.radiant, draft.dire)
        for i, p in enumerate(side)
    }
    original = research_features(history, draft)
    other = research_features(
        history,
        replace(
            draft,
            radiant=draft.dire,
            dire=draft.radiant,
            radiant_id=draft.dire_id,
            dire_id=draft.radiant_id,
        ),
    )
    assert other == pytest.approx(swap_features(original))
    assert "pair:1:13" in feature_view(original, "classic")
    assert not any(k.startswith("pair:") for k in feature_view(original, "compact"))


@pytest.mark.parametrize("kind", ["linear", "hist_gradient"])
def test_future_targets_do_not_affect_fitted_model_or_its_calibration(kind):
    rows = history_rows(300)
    features = build_examples(rows)
    split = chronological_split(rows)
    candidate = {
        "kind": kind,
        "features": "classic" if kind == "linear" else "compact",
        "C": 0.15,
        "max_depth": 2,
        "max_leaf_nodes": 4,
        "min_samples_leaf": 10,
        "max_iter": 10,
    }
    original = fit_candidate(candidate, rows, features, split, split[2])
    altered = copy.deepcopy(rows)
    for i in split[2]:
        altered[i]["radiant_win"] = not altered[i]["radiant_win"]
    actual = fit_candidate(candidate, altered, features, split, split[2])
    assert actual == pytest.approx(original)
    assert np.isfinite(actual).all() and ((actual > 0) & (actual < 1)).all()


def test_recent_training_does_not_use_dates_from_test_rows():
    rows = history_rows(20)
    weights = training_weights(rows, [0, 1, 2], 90)
    rows[-1]["start_time"] += 99999999
    assert training_weights(rows, [0, 1, 2], 90) == pytest.approx(weights)
    assert weights[0] < weights[2] and weights.mean() == pytest.approx(1)


def test_selection_requires_stable_probability_gain_without_accuracy_loss():
    plan = {
        "minimum_brier_improvement": 0.0005,
        "candidates": [
            {"name": name} for name in ("baseline", "unstable", "worse_accuracy", "improved")
        ],
    }

    def result(brier, wins):
        return {"brier": brier, "wins": wins, "count": 100}

    records = {
        "baseline": [result(0.24, 60), result(0.23, 60)],
        "unstable": [result(0.20, 70), result(0.24, 60)],
        "worse_accuracy": [result(0.20, 55), result(0.20, 55)],
        "improved": [result(0.239, 61), result(0.229, 61)],
    }
    assert select_candidate(records, plan) == "improved"
    records["improved"] = [result(0.24, 60), result(0.23, 60)]
    assert select_candidate(records, plan) is None
