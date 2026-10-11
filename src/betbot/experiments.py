"""Offline, reproducible model research. Nothing here publishes or replaces live models."""

import csv
import hashlib
import heapq
import json
import math
import warnings
from collections import defaultdict
from pathlib import Path

import numpy as np

from .domain import Draft, timestamp
from .model import History, chronological_split, historical_draft


def research_features(history: History, draft: Draft) -> dict[str, float]:
    features = history.features(draft, feature_set="players_form")
    features.update(history.analytics.features(draft))
    features.update(history.signatures.features(draft))
    roles = history.analytics.assignments(draft)
    for radiant in draft.radiant:
        for dire in draft.dire:
            a, b = sorted((radiant.hero_id, dire.hero_id))
            features[f"counter:{a}:{b}"] = 0.2 if radiant.hero_id == a else -0.2
    positions = {side: history.analytics.positioned(roles[side]) for side in ("radiant", "dire")}
    if 2 in positions["radiant"] and 2 in positions["dire"]:
        r, d = positions["radiant"][2], positions["dire"][2]
        a, b = sorted((r, d))
        features[f"mid_counter:{a}:{b}"] = 0.5 if r == a else -0.5
    for side, sign in (("radiant", 1), ("dire", -1)):
        for core, support in ((1, 5), (3, 4)):
            assigned = positions[side]
            if core in assigned and support in assigned:
                features[f"lane_pair:{core}:{assigned[core]}:{assigned[support]}"] = sign * 0.5
    differences = defaultdict(float)
    coverage = 0
    for side, sign in (("radiant", 1), ("dire", -1)):
        players = getattr(draft, side)
        known = [history.players.get(str(p.account_id), 1500.0) for p in players if p.account_id]
        if known:
            differences["weakest_player_elo"] += sign * min(known) / 400
            differences["strongest_player_elo"] += sign * max(known) / 400
            differences["player_elo_spread"] += sign * (max(known) - min(known)) / 400
        positions = {r["account_id"]: r["position"] for r in roles[side] if r["position"]}
        coverage += len(positions)
        values = []
        for player in players:
            wins, n = history.player_heroes.get(f"{player.account_id}:{player.hero_id}", [0, 0])
            experience = (wins + 3) / (n + 6) - 0.5 if player.account_id else 0.0
            values.append(experience)
            position = positions.get(player.account_id)
            if position in (1, 2, 3):
                differences[f"position_{position}_experience"] += sign * experience
        differences["weakest_hero_experience"] += sign * min(values)
        differences["strongest_hero_experience"] += sign * max(values)
    features.update(differences)
    features["role_coverage"] = coverage / 10
    # Odd in the side difference: swapping both teams must reverse the advantage.
    for key in (
        "hero_experience",
        "recent_player_form",
        "counter_advantage",
        "position_1_experience",
        "position_2_experience",
    ):
        features[f"interaction:{key}:elo_gap"] = features.get(key, 0) * abs(features["team_elo"])
    return features


def swap_features(features):
    from .evidence import EVEN_EVIDENCE_FEATURES
    from .series_context import EVEN_SERIES_FEATURES

    return {
        key: value
        if key.startswith("patch:")
        or key == "role_coverage"
        or key in EVEN_SERIES_FEATURES
        or key in EVEN_EVIDENCE_FEATURES
        else -value
        for key, value in features.items()
    }


def feature_view(features, name):
    base = {
        key: value
        for key, value in features.items()
        if key.startswith(("hero:", "pair:", "patch:"))
        or key in ("team_elo", "player_elo", "hero_experience")
    }
    if name == "classic":
        return base
    from .evidence import EVIDENCE_VIEWS, evidence_view

    if name in EVIDENCE_VIEWS:
        return evidence_view(features, base, name)
    if name == "tier1_ratings":
        return {
            **base,
            **{key: value for key, value in features.items() if key.startswith("tier1:")},
        }
    if name in ("series_basic", "series_full", "series_compact"):
        from .series_context import BASIC_SERIES_FEATURES

        context = {
            key: value
            for key, value in features.items()
            if key.startswith("series:")
            and (name != "series_basic" or key in BASIC_SERIES_FEATURES)
        }
        original = feature_view(features, "compact") if name == "series_compact" else base
        return {**original, **context}
    direct = ("counter:", "mid_counter:", "lane_pair:")
    if name in ("counters", "role_counters"):
        prefixes = direct if name == "role_counters" else ("counter:",)
        return {
            **base,
            **{key: value for key, value in features.items() if key.startswith(prefixes)},
        }
    if name == "interactions":
        return {
            key: value
            for key, value in features.items()
            if not key.startswith(
                (*direct, "series:", "tier1:", "evidence:", "support_pair:", "triple:", "identity_")
            )
        }
    if name == "compact":
        return {
            key: value
            for key, value in features.items()
            if not key.startswith(
                (
                    "pair:",
                    *direct,
                    "series:",
                    "tier1:",
                    "evidence:",
                    "support_pair:",
                    "triple:",
                    "identity_",
                )
            )
        }
    raise ValueError(f"Unknown research feature view: {name}")


def build_examples(rows, output=None, progress=None):
    history = History()
    waiting, features = [], []
    for i, row in enumerate(rows):
        start = int(row["start_time"])
        while waiting and waiting[0][0] < start:
            _, previous = heapq.heappop(waiting)
            old = rows[previous]
            history.update(historical_draft(old), old["radiant_win"], old)
        # Draft intentionally excludes the current winner, lanes, economy and duration.
        draft = historical_draft(row)
        values = research_features(history, draft)
        features.append(values)
        if output:
            output.write(
                json.dumps(
                    {
                        "match_id": draft.match_id,
                        "observed_at": draft.observed_at.isoformat(),
                        "features": values,
                    }
                )
                + "\n"
            )
        heapq.heappush(waiting, (start + int(row["duration"]) + 60, i))
        if progress and (i + 1) % 2000 == 0:
            progress({"features_built": i + 1, "total": len(rows)})
    return features


def training_weights(rows, indices, half_life_days=None):
    if not half_life_days:
        return np.ones(len(indices))
    newest = max(int(rows[i]["start_time"]) for i in indices)
    values = np.asarray(
        [
            math.exp(
                -math.log(2) * (newest - int(rows[i]["start_time"])) / (half_life_days * 86400)
            )
            for i in indices
        ]
    )
    # Keep the regularization strength comparable to the unweighted control.
    return values / values.mean()


def earlier_partition(rows, indices, fraction):
    """Split only available earlier labels, purging series and late completions."""
    if not 0.5 <= fraction <= 0.95:
        raise ValueError("Invalid chronological training fraction")
    indices = sorted(indices, key=lambda i: (rows[i]["start_time"], rows[i]["match_id"]))
    boundary = int(rows[indices[int(len(indices) * fraction)]]["start_time"])
    sides = defaultdict(set)
    for i in indices:
        row = rows[i]
        key = f"series:{row['series_id']}" if row.get("series_id") else f"match:{row['match_id']}"
        sides[key].add(int(row["start_time"]) < boundary)
    train, calibration = [], []
    for i in indices:
        row = rows[i]
        key = f"series:{row['series_id']}" if row.get("series_id") else f"match:{row['match_id']}"
        if len(sides[key]) != 1:
            continue
        if int(row["start_time"]) < boundary:
            if int(row["start_time"]) + int(row["duration"]) + 60 < boundary:
                train.append(i)
        else:
            calibration.append(i)
    return train, calibration


def candidate_partition(candidate, rows, split, target_scope=None, features=None):
    from .backtest import score_event

    train, calibration, _ = split
    if candidate.get("earlier_training_fraction"):
        train, calibration = earlier_partition(
            rows, train + calibration, candidate["earlier_training_fraction"]
        )
    if candidate.get("training_days"):
        boundary = min(int(rows[i]["start_time"]) for i in calibration)
        train = [
            i
            for i in train
            if int(rows[i]["start_time"]) >= boundary - candidate["training_days"] * 86400
        ]
    if (
        any(candidate.get(k) for k in ("target_training", "target_calibration", "tier_multiplier"))
        and not target_scope
    ):
        raise ValueError("A verified target scope is required for domain experiments")
    if candidate.get("target_training"):
        train = [i for i in train if score_event(rows[i], target_scope)]
    if candidate.get("target_calibration"):
        calibration = [i for i in calibration if score_event(rows[i], target_scope)]
    if candidate.get("exclude_training_leagues"):
        excluded = set(candidate["exclude_training_leagues"])
        train = [i for i in train if rows[i]["leagueid"] not in excluded]
    if candidate.get("exclude_calibration_leagues"):
        excluded = set(candidate["exclude_calibration_leagues"])
        calibration = [i for i in calibration if rows[i]["leagueid"] not in excluded]
    if candidate.get("roster_stable_training"):
        if features is None:
            raise ValueError("Causal roster metadata is required")
        train = [
            i
            for i in train
            if features[i]["evidence:roster_min"] >= candidate["roster_stable_training"]
        ]
    if len(train) < 100 or len(calibration) < 50:
        raise ValueError("Insufficient earlier maps for training or calibration")
    return train, calibration


def fit_candidate(
    candidate, rows, features, split, predict_indices, target_scope=None, diagnostics=None
):
    if candidate.get("monthly_update"):
        from .presentation import MOSCOW

        months = defaultdict(list)
        for position, i in enumerate(predict_indices):
            moment = timestamp(rows[i]["start_time"]).astimezone(MOSCOW)
            months[moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)].append(
                (position, i)
            )
        result = np.zeros(len(predict_indices))
        static = {
            k: v
            for k, v in candidate.items()
            if k not in ("monthly_update", "monthly_training_fraction")
        }
        for j, (month, group) in enumerate(sorted(months.items())):
            partition = split
            if j:
                available = [
                    i
                    for i, row in enumerate(rows)
                    if int(row["start_time"]) + int(row["duration"]) + 60 < month.timestamp()
                ]
                train, calibration = earlier_partition(
                    rows, available, candidate["monthly_training_fraction"]
                )
                partition = (train, calibration, [])
            predictions = fit_candidate(
                static, rows, features, partition, [i for _, i in group], target_scope
            )
            result[[position for position, _ in group]] = predictions
        return result
    if candidate["kind"] == "blend":
        members = candidate["members"]
        weights = np.asarray(candidate["blend_weights"], dtype=float)
        if len(weights) != len(members) or (weights < 0).any() or weights.sum() <= 0:
            raise ValueError("Invalid predeclared blend weights")
        predictions = [
            fit_candidate(member, rows, features, split, predict_indices, target_scope)
            for member in members
        ]
        return np.average(predictions, axis=0, weights=weights)
    from scipy.sparse import vstack
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    from threadpoolctl import threadpool_limits

    from .backtest import score_event
    from .research_estimators import balance_weights, estimator_for, raw_scores

    train, calibration = candidate_partition(candidate, rows, split, target_scope, features)
    view = candidate["features"]
    vectorizer = DictVectorizer(
        sparse=candidate["kind"] in ("linear", "elastic", "spline", "offset")
    )
    train_features = [feature_view(features[i], view) for i in train]
    x = vectorizer.fit_transform(train_features)
    y = np.asarray([int(row["radiant_win"]) for row in rows])
    train_y = y[train]
    weights = training_weights(rows, train, candidate.get("half_life_days"))
    weights = balance_weights(rows, train, weights, candidate.get("sample_balance"))
    if candidate.get("tier_multiplier"):
        weights *= np.asarray(
            [
                candidate["tier_multiplier"] if score_event(rows[i], target_scope) else 1
                for i in train
            ]
        )
        weights /= weights.mean()
    if candidate.get("mirror"):
        other = vectorizer.transform([swap_features(row) for row in train_features])
        x, train_y, weights = (
            vstack((x, other)),
            np.concatenate((train_y, 1 - train_y)),
            np.concatenate((weights, weights)) / 2,
        )
    estimator, fitting = estimator_for(candidate, vectorizer.get_feature_names_out())
    with threadpool_limits(limits=1):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            estimator.fit(
                x, train_y, **{fitting.get("sample_weight_parameter", "sample_weight"): weights}
            )
        if diagnostics is not None:
            diagnostics["fit_warnings"] = [str(w.message) for w in caught]

        def calibration_inputs(indices):
            scores = raw_scores(
                estimator, vectorizer.transform([feature_view(features[i], view) for i in indices])
            )
            mode = candidate.get("calibration", "combined")
            if mode not in ("combined", "score_only", "team", "player", "raw", "pooled_tier1"):
                raise ValueError("Unknown calibration mode")
            columns = [scores]
            calibration_features = [
                feature_view(features[i], view)
                if candidate.get("calibration_ratings") == "view"
                else features[i]
                for i in indices
            ]
            if mode in ("combined", "team", "pooled_tier1"):
                columns.append([f["team_elo"] * math.log(10) for f in calibration_features])
            if mode in ("combined", "player", "pooled_tier1"):
                columns.append([f["player_elo"] for f in calibration_features])
            if mode == "pooled_tier1":
                flag = np.asarray([features[i]["evidence:tier1"] for i in indices])
                columns.extend(
                    (
                        flag,
                        flag * scores,
                        flag * np.asarray(columns[1]),
                        flag * np.asarray(columns[2]),
                    )
                )
            return np.column_stack(columns)

        if candidate.get("calibration") == "raw":
            return estimator.predict_proba(
                vectorizer.transform([feature_view(features[i], view) for i in predict_indices])
            )[:, 1]

        calibrated = LogisticRegression(
            C=0.2 if candidate.get("calibration") == "pooled_tier1" else 1.0,
            random_state=42,
            max_iter=1500 if candidate.get("calibration") == "pooled_tier1" else 100,
        )
        calibrated.fit(calibration_inputs(calibration), y[calibration])
        return calibrated.predict_proba(calibration_inputs(predict_indices))[:, 1]


def probability_metrics(actual, probabilities):
    from sklearn.metrics import log_loss

    actual, probabilities = np.asarray(actual), np.asarray(probabilities)
    return {
        "count": len(actual),
        "wins": int(((probabilities >= 0.5) == actual).sum()),
        "accuracy": float(((probabilities >= 0.5) == actual).mean()),
        "brier": float(((probabilities - actual) ** 2).mean()),
        "log_loss": float(log_loss(actual, probabilities, labels=[0, 1])),
    }


def grouped_interval(rows, actual, original, candidate, draws=3000):
    groups = defaultdict(list)
    for row, y, old, new in zip(rows, actual, original, candidate, strict=True):
        key = f"series:{row['series_id']}" if row.get("series_id") else f"match:{row['match_id']}"
        groups[key].append(
            ((new - y) ** 2 - (old - y) ** 2, int((new >= 0.5) == y) - int((old >= 0.5) == y))
        )
    blocks = [np.asarray(v) for v in groups.values()]
    rng = np.random.default_rng(42)
    samples = [
        np.concatenate([blocks[i] for i in rng.integers(0, len(blocks), len(blocks))]).mean(axis=0)
        for _ in range(draws)
    ]
    limits = np.quantile(samples, [0.025, 0.975], axis=0)
    return {
        "series_blocks": len(blocks),
        "brier_difference_95_interval": limits[:, 0].tolist(),
        "accuracy_difference_95_interval": limits[:, 1].tolist(),
    }


def select_candidate(records, plan):
    names = [c["name"] for c in plan["candidates"] if c["name"] != "baseline"]
    baseline = records["baseline"]
    eligible = []
    for name in names:
        values = records[name]
        if plan.get("require_convergence") and any(r.get("fit_warnings") for r in values):
            continue
        mean_brier = np.mean([r["brier"] for r in values])
        improvement = np.mean([r["brier"] for r in baseline]) - mean_brier
        stable = all(r["brier"] <= old["brier"] for r, old in zip(values, baseline, strict=True))
        accuracy = sum(r["wins"] for r in values) / sum(r["count"] for r in values)
        original_accuracy = sum(r["wins"] for r in baseline) / sum(r["count"] for r in baseline)
        if plan.get("primary_metric") == "accuracy":
            accurate_in_each = all(
                r["wins"] / r["count"] >= old["wins"] / old["count"]
                for r, old in zip(values, baseline, strict=True)
            )
            probability_guard = all(
                r["brier"] <= old["brier"] + plan["maximum_brier_regression"]
                for r, old in zip(values, baseline, strict=True)
            )
            if (
                accuracy - original_accuracy >= plan["minimum_accuracy_improvement"]
                and accurate_in_each
                and probability_guard
            ):
                eligible.append((-accuracy, mean_brier, name))
        elif (
            improvement >= plan["minimum_brier_improvement"]
            and stable
            and accuracy >= original_accuracy
        ):
            eligible.append((mean_brier, name))
    return min(eligible)[-1] if eligible else None


def save_predictions(path, rows, indices, probabilities):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["match_id", "series_id", "league_id", "observed_at", "radiant_probability"],
        )
        writer.writeheader()
        for i, p in zip(indices, probabilities, strict=True):
            row = rows[i]
            writer.writerow(
                {
                    "match_id": row["match_id"],
                    "series_id": row.get("series_id", ""),
                    "league_id": row["leagueid"],
                    "observed_at": timestamp(row["start_time"]).isoformat(),
                    "radiant_probability": p,
                }
            )


def run_experiments(
    input_path: Path,
    baseline_dir: Path,
    plan_path: Path,
    output: Path,
    progress=print,
    feature_cache=None,
    group=None,
):
    from .backtest import score_event

    def emit(value):
        progress(json.dumps(value, ensure_ascii=False))

    output.mkdir(parents=True, exist_ok=True)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if group:
        if group not in {c.get("group") for c in plan["candidates"]}:
            raise ValueError("Unknown preregistered candidate group")
        plan["candidates"] = [
            c for c in plan["candidates"] if c["name"] == "baseline" or c.get("group") == group
        ]
        plan["requested_group"] = group

    def scope(name):
        return (
            json.loads((plan_path.parent / plan[name]).read_text(encoding="utf-8"))
            if plan.get(name)
            else None
        )

    development_scope, target_scope = scope("development_scope_path"), scope("training_scope_path")
    data = input_path.read_bytes()
    baseline = json.loads((baseline_dir / "report.json").read_text(encoding="utf-8"))
    raw = json.loads(data)
    rows = raw["rows"] if isinstance(raw, dict) else raw
    rows = sorted(
        {str(row["match_id"]): row for row in rows if historical_draft(row)}.values(),
        key=lambda r: (int(r["start_time"]), int(r["match_id"])),
    )
    cutoff, end = (
        int(timestamp(baseline[k]).timestamp()) for k in ("requested_start", "requested_end")
    )
    sides = defaultdict(set)
    for row in rows:
        key = f"series:{row['series_id']}" if row.get("series_id") else f"match:{row['match_id']}"
        sides[key].add(int(row["start_time"]) >= cutoff)
    usable = []
    for row in rows:
        key = f"series:{row['series_id']}" if row.get("series_id") else f"match:{row['match_id']}"
        start, finish = int(row["start_time"]), int(row["start_time"]) + int(row["duration"]) + 60
        if len(sides[key]) == 1 and (finish < cutoff or cutoff <= start < end and finish <= end):
            usable.append(row)
    rows = usable
    before_count = sum(int(row["start_time"]) < cutoff for row in rows)
    assert before_count == baseline["history_maps_before_period"]
    protocol = {
        "plan": plan,
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "data_sha256": hashlib.sha256(data).hexdigest(),
        "history_maps_before_period": before_count,
        "requested_start": baseline["requested_start"],
        "requested_end": baseline["requested_end"],
        "heldout_scope": baseline["evaluation_scope"],
        "development_scope": development_scope,
        "training_scope": target_scope,
        "holdout_already_examined_in_previous_work": True,
        "estimator_implementation_sha256": hashlib.sha256(
            Path(__file__).with_name("research_estimators.py").read_bytes()
        ).hexdigest(),
    }
    if plan.get("evidence_features"):
        protocol["evidence_implementation_sha256"] = hashlib.sha256(
            Path(__file__).with_name("evidence.py").read_bytes()
        ).hexdigest()
    if plan.get("series_context"):
        protocol["series_implementation_sha256"] = hashlib.sha256(
            Path(__file__).with_name("series_context.py").read_bytes()
        ).hexdigest()
    if plan.get("tier1_ratings"):
        if not target_scope:
            raise ValueError("Tier 1 rating features require a verified scope")
        protocol["target_implementation_sha256"] = hashlib.sha256(
            Path(__file__).with_name("target_history.py").read_bytes()
        ).hexdigest()
    if feature_cache:
        protocol["feature_cache_sha256"] = hashlib.sha256(feature_cache.read_bytes()).hexdigest()
    previous_protocol = output / "protocol.json"
    if previous_protocol.exists():
        previous = json.loads(previous_protocol.read_text(encoding="utf-8"))
        if any(
            previous.get(k) != protocol.get(k)
            for k in (
                "plan_sha256",
                "implementation_sha256",
                "data_sha256",
                "development_scope",
                "training_scope",
                "series_implementation_sha256",
                "target_implementation_sha256",
                "feature_cache_sha256",
                "estimator_implementation_sha256",
                "evidence_implementation_sha256",
                "plan",
            )
        ):
            raise ValueError(
                "This output already contains a different experiment; use a new directory"
            )
        if (output / "comparison.json").exists():
            emit({"cached_completed_experiment": str(output)})
            return json.loads((output / "comparison.json").read_text(encoding="utf-8"))
    (output / "protocol.json").write_text(
        json.dumps(protocol, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # No candidate sees outcomes for the current map while these inputs are built.
    if feature_cache:
        cached_protocol = json.loads(
            (feature_cache.parent / "protocol.json").read_text(encoding="utf-8")
        )
        if cached_protocol["data_sha256"] != protocol["data_sha256"]:
            raise ValueError("Feature cache uses a different input history")
        cached = [
            json.loads(line) for line in feature_cache.read_text(encoding="utf-8").splitlines()
        ]
        assert len(cached) == len(rows)
        assert all(
            str(item["match_id"]) == str(row["match_id"])
            for item, row in zip(cached, rows, strict=True)
        )
        features = [item["features"] for item in cached]
        emit({"features_reused": len(features)})
    else:
        with (output / "blind-features.jsonl").open("w", encoding="utf-8") as stream:
            features = build_examples(rows, stream, emit)
    if plan.get("series_context"):
        from .series_context import augment_series_features

        features = augment_series_features(rows, features)
        with (output / "blind-series-features.jsonl").open("w", encoding="utf-8") as stream:
            for row, values in zip(rows, features, strict=True):
                stream.write(
                    json.dumps(
                        {
                            "match_id": str(row["match_id"]),
                            "observed_at": timestamp(row["start_time"]).isoformat(),
                            "features": values,
                        }
                    )
                    + "\n"
                )
        emit(
            {
                "series_context_available": sum(r["series:has_previous"] > 0 for r in features),
                "total": len(features),
            }
        )
    if plan.get("tier1_ratings"):
        from .target_history import augment_target_features

        combined_scope = {"events": target_scope["events"] + baseline["evaluation_scope"]["events"]}
        features = augment_target_features(rows, features, combined_scope)
        with (output / "blind-tier1-features.jsonl").open("w", encoding="utf-8") as stream:
            for row, values in zip(rows, features, strict=True):
                stream.write(
                    json.dumps(
                        {
                            "match_id": str(row["match_id"]),
                            "observed_at": timestamp(row["start_time"]).isoformat(),
                            "features": values,
                        }
                    )
                    + "\n"
                )
        emit({"tier1_rating_features": len(features)})
    if plan.get("evidence_features"):
        from .evidence import augment_evidence_features

        combined_scope = {
            "events": (target_scope or {}).get("events", [])
            + (baseline["evaluation_scope"] or {}).get("events", [])
        }
        features = augment_evidence_features(rows, features, combined_scope, emit)
        with (output / "blind-evidence-features.jsonl").open("w", encoding="utf-8") as stream:
            for row, values in zip(rows, features, strict=True):
                stream.write(
                    json.dumps(
                        {
                            "match_id": str(row["match_id"]),
                            "observed_at": timestamp(row["start_time"]).isoformat(),
                            "features": values,
                        }
                    )
                    + "\n"
                )
    records = {c["name"]: [] for c in plan["candidates"]}
    for fold, prefix in enumerate(plan["development_prefixes"]):
        count = int(before_count * prefix)
        split = chronological_split(rows[:count])
        test = [i for i in split[2] if score_event(rows[i], development_scope)]
        actual = np.asarray([int(rows[i]["radiant_win"]) for i in test])
        for candidate in plan["candidates"]:
            diagnostics = {}
            probabilities = fit_candidate(
                candidate, rows, features, split, test, target_scope, diagnostics=diagnostics
            )
            # Save forecasts before combining them with held-out targets in a report.
            save_predictions(
                output / f"development-{fold}-{candidate['name']}.csv", rows, test, probabilities
            )
            result = {
                "fold": fold,
                "test_start": rows[test[0]]["start_time"],
                "test_end": rows[test[-1]]["start_time"],
                **probability_metrics(actual, probabilities),
                **diagnostics,
                "training_maps": len(
                    candidate_partition(candidate, rows, split, target_scope, features)[0]
                ),
                "calibration_maps": len(
                    candidate_partition(candidate, rows, split, target_scope, features)[1]
                ),
            }
            records[candidate["name"]].append(result)
            (output / "development.json").write_text(
                json.dumps(records, indent=2), encoding="utf-8"
            )
            emit({"candidate": candidate["name"], "development": result})
    selected = select_candidate(records, plan)
    # This decision is written before any new four-month predictions are scored.
    (output / "selected.json").write_text(
        json.dumps({"candidate": selected, "automatic_promotion": False}), encoding="utf-8"
    )
    emit({"locked_candidate": selected})
    final = None
    if selected:
        full_split = chronological_split(rows[:before_count])
        # Use the same 60/20/20 recipe as the established frozen baseline.
        # The earlier development-test labels never become calibration targets.
        final_split = full_split
        indices = [
            i
            for i in range(before_count, len(rows))
            if score_event(rows[i], baseline["evaluation_scope"])
        ]
        predictions = {}
        final_diagnostics = {}
        for name in ("baseline", selected):
            candidate = next(c for c in plan["candidates"] if c["name"] == name)
            final_diagnostics[name] = {}
            probabilities = fit_candidate(
                candidate,
                rows,
                features,
                final_split,
                indices,
                target_scope,
                diagnostics=final_diagnostics[name],
            )
            save_predictions(output / f"tier1-{name}.csv", rows, indices, probabilities)
            predictions[name] = probabilities
        actual = np.asarray([int(rows[i]["radiant_win"]) for i in indices])
        with (baseline_dir / "results.csv").open(encoding="utf-8-sig") as stream:
            established = {
                str(r["match_id"]): float(r["radiant_probability"]) for r in csv.DictReader(stream)
            }
        assert {str(rows[i]["match_id"]) for i in indices} == set(established)
        assert np.allclose(
            predictions["baseline"],
            [established[str(rows[i]["match_id"])] for i in indices],
            atol=1e-10,
            rtol=1e-10,
        )
        final = {
            "baseline": probability_metrics(actual, predictions["baseline"]),
            "candidate": probability_metrics(actual, predictions[selected]),
            "fit_diagnostics": final_diagnostics,
            **grouped_interval(
                [rows[i] for i in indices], actual, predictions["baseline"], predictions[selected]
            ),
        }
        # Compare the exact recipe change; also show the established frozen baseline.
        final["established_baseline"] = baseline["all_maps"]
        final["full_coverage"] = len(indices) == baseline["all_maps"]["count"]
        assert final["full_coverage"]
        final["by_league"] = []
        for league in sorted({rows[i]["leagueid"] for i in indices}):
            positions = [j for j, i in enumerate(indices) if rows[i]["leagueid"] == league]
            final["by_league"].append(
                {
                    "league_id": league,
                    "baseline": probability_metrics(
                        actual[positions], predictions["baseline"][positions]
                    ),
                    "candidate": probability_metrics(
                        actual[positions], predictions[selected][positions]
                    ),
                }
            )
        emit({"tier1": final})
    summary = {
        "plan": plan["name"],
        "development": records,
        "selected_candidate": selected,
        "tier1": final,
        "promotion_recommended": False,
        "promoted": False,
    }
    (output / "comparison.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary
