import copy
from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest
from test_model import history_rows

from betbot.evidence import EvidenceHistory, Glicko, augment_evidence_features, glicko_update
from betbot.experiments import (
    build_examples,
    candidate_partition,
    feature_view,
    fit_candidate,
    select_candidate,
    swap_features,
)
from betbot.model import chronological_split
from betbot.research_estimators import balance_weights


def test_glicko_matches_published_three_opponent_example():
    rating, rd = glicko_update(
        1500, 200, [(1400, 30, True), (1550, 100, False), (1700, 300, False)]
    )
    assert rating == pytest.approx(1464.106, abs=0.01)
    assert rd == pytest.approx(151.399, abs=0.01)


def test_glicko_inactivity_increases_uncertainty_without_inventing_new_result():
    history = Glicko()
    history.values[1] = 1800, 50, 0
    assert history.get(1, 180 * 86400) == pytest.approx((1800, 350))
    assert history.get(1, 0) == (1800, 50)
    assert history.values[1] == (1800, 50, 0)


def test_new_evidence_cannot_see_current_or_future_winner_duration_or_economy():
    rows = history_rows(16)
    features = build_examples(rows)
    original = augment_evidence_features(rows, features)
    altered = copy.deepcopy(rows)
    for row in altered[5:]:
        row["radiant_win"] = not row["radiant_win"]
        row["duration"] = 12000
        row["gold10"] = [999999] * 10
        row["gpms"] = [9999] * 10
    actual = augment_evidence_features(altered, features)
    assert actual[:6] == original[:6]
    assert actual[6] != original[6]
    for old, new in zip(features, original, strict=True):
        assert feature_view(old, "classic") == feature_view(new, "classic")
        assert feature_view(old, "compact") == feature_view(new, "compact")


def test_new_evidence_waits_until_after_completion_buffer():
    rows = history_rows(3)
    start = rows[0]["start_time"]
    rows[0]["duration"] = 600
    rows[1]["start_time"], rows[2]["start_time"] = start + 660, start + 661
    original = augment_evidence_features(rows, build_examples(rows))
    assert original[1]["evidence:glicko_team"] == 0
    assert original[2]["evidence:glicko_team"] != 0


def test_new_advantages_reverse_on_side_swap_and_metadata_is_even(draft):
    history = EvidenceHistory()
    for i in range(15):
        history.update(draft, True, 0.6, draft.observed_at.timestamp() - 1000 + i)
    later = replace(draft, observed_at=draft.observed_at + timedelta(hours=2))
    original = history.features(later)
    other = history.features(
        replace(
            later,
            radiant=draft.dire,
            dire=draft.radiant,
            radiant_id=draft.dire_id,
            dire_id=draft.radiant_id,
        )
    )
    assert other == pytest.approx(swap_features(original))
    assert original["evidence:roster_min"] == 1
    assert len([k for k in original if k.startswith("triple:")]) == 20
    assert len([k for k in original if k.startswith("identity_player_hero:")]) == 10
    assert len([k for k in original if k.startswith("identity_team_hero:")]) == 10
    base = {"team_elo": 0.3, "player_elo": 0.2, "hero_experience": 0.1, "pair:1:13": 0.5}
    assert feature_view({**base, **original}, "supported_pairs")["pair:1:13"] < 0.5
    assert (
        feature_view({**base, **original}, "glicko_team")["team_elo"]
        == original["evidence:glicko_team"]
    )


def test_tournament_and_h2h_form_require_matching_identity_and_rosters(draft):
    history = EvidenceHistory()
    history.update(draft, True, 0.6, draft.observed_at.timestamp() - 100)
    same = history.features(draft)
    assert same["evidence:event_residual"] > 0
    assert same["evidence:h2h_residual"] > 0
    different_league = history.features(replace(draft, league_id=999))
    assert different_league["evidence:event_residual"] == 0
    assert different_league["evidence:h2h_residual"] > 0
    different_team = history.features(replace(draft, radiant_id=999))
    assert different_team["evidence:h2h_residual"] == 0
    changed_roster = history.features(
        replace(
            draft,
            radiant=tuple(replace(p, account_id=p.account_id + 999999) for p in draft.radiant),
        )
    )
    assert changed_roster["evidence:h2h_residual"] == 0
    assert changed_roster["evidence:roster_min"] == 0
    assert not any(k.startswith("identity_team_hero:") for k in changed_roster)


@pytest.mark.parametrize("mode", ["league", "series", "patch"])
def test_sample_weights_use_training_metadata_only_and_preserve_total_regularization(mode):
    rows = history_rows(300)
    rows[0]["leagueid"], rows[0]["series_id"], rows[0]["patch"] = 999, 999, 999
    indices = list(range(180))
    original = balance_weights(rows, indices, np.ones(180), mode)
    assert original.mean() == pytest.approx(1)
    for row in rows[180:]:
        row["leagueid"], row["series_id"], row["patch"] = 999, 999, 999
        row["radiant_win"] = not row["radiant_win"]
    assert balance_weights(rows, indices, np.ones(180), mode) == pytest.approx(original)


def test_roster_filter_only_removes_training_maps_without_dropping_heldout_coverage():
    rows = history_rows(300)
    split = chronological_split(rows)
    features = [{"evidence:roster_min": 1.0} for row in rows]
    features[split[0][0]]["evidence:roster_min"] = 0
    features[split[2][0]]["evidence:roster_min"] = 0
    train, cal = candidate_partition(
        {"roster_stable_training": 0.8}, rows, split, features=features
    )
    assert train == split[0][1:] and cal == split[1]
    with pytest.raises(ValueError, match="roster metadata"):
        candidate_partition({"roster_stable_training": 0.8}, rows, split)


@pytest.mark.parametrize("kind", ["elastic", "extra_trees", "mlp", "offset", "spline"])
def test_new_learners_fit_scalers_and_weights_without_current_or_future_test_labels(kind):
    rows = history_rows(300)
    features = build_examples(rows)
    split = chronological_split(rows)
    candidate = {
        "kind": kind,
        "features": "compact" if kind in ("mlp", "extra_trees") else "classic",
        "C": 0.15,
    }
    original = fit_candidate(candidate, rows, features, split, split[2])
    for i in split[2]:
        rows[i]["radiant_win"] = not rows[i]["radiant_win"]
    actual = fit_candidate(candidate, rows, features, split, split[2])
    assert actual == pytest.approx(original)
    assert np.isfinite(actual).all() and (actual > 0).all() and (actual < 1).all()


def test_pooled_domain_calibration_ignores_test_labels():
    rows = history_rows(300)
    features = build_examples(rows)
    for i, row in enumerate(features):
        row["evidence:tier1"] = float(i % 3 == 0)
    split = chronological_split(rows)
    candidate = {"kind": "linear", "features": "classic", "C": 0.15, "calibration": "pooled_tier1"}
    original = fit_candidate(candidate, rows, features, split, split[2])
    for i in split[2]:
        rows[i]["radiant_win"] = not rows[i]["radiant_win"]
    assert fit_candidate(candidate, rows, features, split, split[2]) == pytest.approx(original)


def test_unconverged_candidate_cannot_win_predeclared_selection():
    plan = {
        "primary_metric": "accuracy",
        "minimum_accuracy_improvement": 0.0001,
        "maximum_brier_regression": 0.002,
        "require_convergence": True,
        "candidates": [{"name": "baseline"}, {"name": "network"}],
    }
    records = {
        "baseline": [{"wins": 60, "count": 100, "brier": 0.23}],
        "network": [
            {"wins": 65, "count": 100, "brier": 0.23, "fit_warnings": ["Did not converge"]}
        ],
    }
    assert select_candidate(records, plan) is None
    records["network"][0]["fit_warnings"] = []
    assert select_candidate(records, plan) == "network"
