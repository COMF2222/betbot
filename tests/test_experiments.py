import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from test_model import history_rows

from betbot.experiments import (
    build_examples,
    candidate_partition,
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


def test_target_scope_checks_league_and_date_without_expanding_temporal_partition():
    from betbot.domain import timestamp

    rows = history_rows(300)
    split = chronological_split(rows)
    scope = {
        "events": [
            {
                "league_id": 123,
                "start_date": timestamp(rows[0]["start_time"]).date().isoformat(),
                "end_date": timestamp(rows[-1]["start_time"]).date().isoformat(),
            }
        ]
    }
    rows[split[0][0]]["leagueid"] = 999  # Qualifier with another league ID.
    rows[split[0][1]]["start_time"] -= 86400 * 365  # Reused ID, different edition.
    candidate = {"target_training": True, "target_calibration": True}
    train, calibration = candidate_partition(candidate, rows, split, scope)
    assert train == split[0][2:] and calibration == split[1]
    assert not set(train + calibration) & set(split[2])
    with pytest.raises(ValueError, match="verified target scope"):
        candidate_partition(candidate, rows, split)


def test_tier_weighting_and_calibration_still_ignore_future_targets():
    from betbot.domain import timestamp

    rows = history_rows(300)
    features = build_examples(rows)
    split = chronological_split(rows)
    scope = {
        "events": [
            {
                "league_id": 123,
                "start_date": timestamp(rows[0]["start_time"]).date().isoformat(),
                "end_date": timestamp(rows[-1]["start_time"]).date().isoformat(),
            }
        ]
    }
    candidate = {
        "kind": "linear",
        "features": "classic",
        "C": 0.15,
        "tier_multiplier": 4,
        "target_calibration": True,
    }
    original = fit_candidate(candidate, rows, features, split, split[2], scope)
    for i in split[2]:
        rows[i]["radiant_win"] = not rows[i]["radiant_win"]
    assert fit_candidate(candidate, rows, features, split, split[2], scope) == pytest.approx(
        original
    )


def test_direct_counters_are_added_but_unknown_roles_never_invent_lanes(draft):
    history = History()
    features = research_features(history, draft)
    counters = {k for k in features if k.startswith("counter:")}
    assert len(counters) == 25
    assert not any(k.startswith(("mid_counter:", "lane_pair:")) for k in features)
    view = feature_view(features, "counters")
    assert counters.issubset(view) and not counters & feature_view(features, "classic").keys()
    history.analytics.roles = {
        str(p.account_id): [i + 1] * 10
        for side in (draft.radiant, draft.dire)
        for i, p in enumerate(side)
    }
    roles = research_features(history, draft)
    assert len([k for k in roles if k.startswith("mid_counter:")]) == 1
    assert len([k for k in roles if k.startswith("lane_pair:")]) == 4
    assert not any(
        k.startswith(("counter:", "mid_counter:", "lane_pair:"))
        for k in feature_view(roles, "compact")
    )


def test_fixed_blend_is_the_declared_average_without_fitting_holdout_targets():
    rows = history_rows(300)
    features = build_examples(rows)
    split = chronological_split(rows)
    members = [
        {"kind": "linear", "features": "classic", "C": 0.15},
        {"kind": "linear", "features": "counters", "C": 0.05},
    ]
    p = fit_candidate(
        {"kind": "blend", "members": members, "blend_weights": [0.5, 0.5]},
        rows,
        features,
        split,
        split[2],
    )
    expected = np.mean([fit_candidate(m, rows, features, split, split[2]) for m in members], axis=0)
    assert p == pytest.approx(expected)
    with pytest.raises(ValueError, match="blend weights"):
        fit_candidate(
            {"kind": "blend", "members": members, "blend_weights": [-1, 2]},
            rows,
            features,
            split,
            split[2],
        )


def test_completed_experiment_is_reused_but_changed_protocol_cannot_overwrite_it(tmp_path):
    import betbot.experiments as experiments
    from betbot.domain import timestamp

    rows = history_rows(300)
    input_path, plan_path = tmp_path / "history.json", tmp_path / "plan.json"
    input_path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    plan_path.write_text(
        json.dumps({"name": "test", "candidates": [], "development_prefixes": [1]}),
        encoding="utf-8",
    )
    baseline_dir, output = tmp_path / "baseline", tmp_path / "output"
    baseline_dir.mkdir()
    output.mkdir()
    baseline = {
        "requested_start": timestamp(rows[180]["start_time"]).isoformat(),
        "requested_end": timestamp(rows[-1]["start_time"] + rows[-1]["duration"] + 61).isoformat(),
        "history_maps_before_period": 180,
        "evaluation_scope": None,
    }
    (baseline_dir / "report.json").write_text(json.dumps(baseline), encoding="utf-8")
    protocol = {
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "implementation_sha256": hashlib.sha256(
            Path(experiments.__file__).read_bytes()
        ).hexdigest(),
        "data_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
    }
    (output / "protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
    saved = '{"promoted": false, "selected_candidate": null}'
    (output / "comparison.json").write_text(saved, encoding="utf-8")
    assert experiments.run_experiments(
        input_path, baseline_dir, plan_path, output, progress=lambda _: None
    ) == json.loads(saved)
    plan_path.write_text('{"name":"different"}', encoding="utf-8")
    with pytest.raises(ValueError, match="different experiment"):
        experiments.run_experiments(
            input_path, baseline_dir, plan_path, output, progress=lambda _: None
        )
    assert (output / "comparison.json").read_text(encoding="utf-8") == saved


def test_new_training_boundary_purges_crossing_series_and_late_labels():
    from betbot.experiments import earlier_partition

    rows = history_rows(300)
    rows[149]["series_id"] = rows[150]["series_id"] = 9999
    rows[148]["duration"] = 99999
    train, calibration = earlier_partition(rows, list(range(300)), 0.5)
    assert not {148, 149, 150} & set(train + calibration)
    assert not {rows[i]["series_id"] for i in train} & {rows[i]["series_id"] for i in calibration}
    assert max(rows[i]["start_time"] + rows[i]["duration"] + 60 for i in train) < min(
        rows[i]["start_time"] for i in calibration
    )


def test_monthly_retraining_cannot_use_the_current_or_future_month_results():
    from betbot.domain import timestamp

    rows = history_rows(900)
    for i, row in enumerate(rows):
        row["start_time"] = 1767225600 + i * 21600  # Synthetic Jan-Aug 2026, six-hour spacing.
    features = build_examples(rows)
    split = chronological_split(rows)
    candidate = {
        "kind": "linear",
        "features": "classic",
        "C": 0.15,
        "monthly_update": True,
        "monthly_training_fraction": 0.9,
    }
    original = fit_candidate(candidate, rows, features, split, split[2])
    first_month = timestamp(rows[split[2][0]]["start_time"]).month
    selected = [
        j for j, i in enumerate(split[2]) if timestamp(rows[i]["start_time"]).month == first_month
    ]
    for i in split[2]:
        rows[i]["radiant_win"] = not rows[i]["radiant_win"]
    actual = fit_candidate(candidate, rows, features, split, split[2])
    assert actual[selected] == pytest.approx(original[selected])


def test_accuracy_objective_keeps_probability_guard_and_both_fold_accuracy_guard():
    plan = {
        "primary_metric": "accuracy",
        "minimum_accuracy_improvement": 0.005,
        "maximum_brier_regression": 0.002,
        "candidates": [{"name": n} for n in ("baseline", "loss_bad", "accuracy_unstable", "ok")],
    }

    def metric(wins, brier):
        return {"wins": wins, "count": 100, "brier": brier}

    values = {
        "baseline": [metric(60, 0.23), metric(60, 0.24)],
        "loss_bad": [metric(70, 0.25), metric(70, 0.25)],
        "accuracy_unstable": [metric(59, 0.23), metric(70, 0.24)],
        "ok": [metric(61, 0.231), metric(62, 0.241)],
    }
    assert select_candidate(values, plan) == "ok"
    values["ok"] = [metric(60, 0.23), metric(60, 0.24)]
    assert select_candidate(values, plan) is None


@pytest.mark.parametrize("mode", ["raw", "score_only", "team", "player"])
def test_calibration_modes_do_not_fit_current_or_future_test_labels(mode):
    rows = history_rows(300)
    features = build_examples(rows)
    split = chronological_split(rows)
    candidate = {"kind": "linear", "features": "classic", "C": 0.15, "calibration": mode}
    original = fit_candidate(candidate, rows, features, split, split[2])
    for i in split[2]:
        rows[i]["radiant_win"] = not rows[i]["radiant_win"]
    assert fit_candidate(candidate, rows, features, split, split[2]) == pytest.approx(original)
