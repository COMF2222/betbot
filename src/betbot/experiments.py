"""Offline, reproducible model research. Nothing here publishes or replaces live models."""

import csv
import hashlib
import heapq
import json
import math
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
    return {
        key: value if key.startswith("patch:") or key == "role_coverage" else -value
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
    if name == "interactions":
        return features
    if name == "compact":
        return {key: value for key, value in features.items() if not key.startswith("pair:")}
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


def fit_candidate(candidate, rows, features, split, predict_indices):
    from scipy.sparse import vstack
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    from threadpoolctl import threadpool_limits

    train, calibration, _ = split
    view = candidate["features"]
    vectorizer = DictVectorizer(sparse=candidate["kind"] == "linear")
    train_features = [feature_view(features[i], view) for i in train]
    x = vectorizer.fit_transform(train_features)
    y = np.asarray([int(row["radiant_win"]) for row in rows])
    train_y = y[train]
    weights = training_weights(rows, train, candidate.get("half_life_days"))
    if candidate.get("mirror"):
        other = vectorizer.transform([swap_features(row) for row in train_features])
        x, train_y, weights = (
            vstack((x, other)),
            np.concatenate((train_y, 1 - train_y)),
            np.concatenate((weights, weights)) / 2,
        )
    if candidate["kind"] == "linear":
        estimator = LogisticRegression(C=candidate["C"], max_iter=1500, random_state=42)
    else:
        estimator = HistGradientBoostingClassifier(
            learning_rate=0.05,
            l2_regularization=20,
            early_stopping=False,
            random_state=42,
            **{
                k: candidate[k]
                for k in ("max_depth", "max_leaf_nodes", "min_samples_leaf", "max_iter")
            },
        )
    with threadpool_limits(limits=1):
        estimator.fit(x, train_y, sample_weight=weights)

        def calibration_inputs(indices):
            scores = estimator.decision_function(
                vectorizer.transform([feature_view(features[i], view) for i in indices])
            )
            return np.column_stack(
                (
                    scores,
                    [features[i]["team_elo"] * math.log(10) for i in indices],
                    [features[i]["player_elo"] for i in indices],
                )
            )

        calibrated = LogisticRegression(C=1.0, random_state=42)
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
        mean_brier = np.mean([r["brier"] for r in values])
        improvement = np.mean([r["brier"] for r in baseline]) - mean_brier
        stable = all(r["brier"] <= old["brier"] for r, old in zip(values, baseline, strict=True))
        accuracy = sum(r["wins"] for r in values) / sum(r["count"] for r in values)
        original_accuracy = sum(r["wins"] for r in baseline) / sum(r["count"] for r in baseline)
        if (
            improvement >= plan["minimum_brier_improvement"]
            and stable
            and accuracy >= original_accuracy
        ):
            eligible.append((mean_brier, name))
    return min(eligible)[1] if eligible else None


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
    input_path: Path, baseline_dir: Path, plan_path: Path, output: Path, progress=print
):
    from .backtest import score_event

    def emit(value):
        progress(json.dumps(value, ensure_ascii=False))

    output.mkdir(parents=True, exist_ok=True)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
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
        "data_sha256": hashlib.sha256(data).hexdigest(),
        "history_maps_before_period": before_count,
        "requested_start": baseline["requested_start"],
        "requested_end": baseline["requested_end"],
        "heldout_scope": baseline["evaluation_scope"],
        "holdout_already_examined_in_previous_work": True,
    }
    (output / "protocol.json").write_text(
        json.dumps(protocol, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # No candidate sees outcomes for the current map while these inputs are built.
    with (output / "blind-features.jsonl").open("w", encoding="utf-8") as stream:
        features = build_examples(rows, stream, emit)
    records = {c["name"]: [] for c in plan["candidates"]}
    for fold, prefix in enumerate(plan["development_prefixes"]):
        count = int(before_count * prefix)
        split = chronological_split(rows[:count])
        test = split[2]
        actual = np.asarray([int(rows[i]["radiant_win"]) for i in test])
        for candidate in plan["candidates"]:
            probabilities = fit_candidate(candidate, rows, features, split, test)
            # Save forecasts before combining them with held-out targets in a report.
            save_predictions(
                output / f"development-{fold}-{candidate['name']}.csv", rows, test, probabilities
            )
            result = {
                "fold": fold,
                "test_start": rows[test[0]]["start_time"],
                "test_end": rows[test[-1]]["start_time"],
                **probability_metrics(actual, probabilities),
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
        for name in ("baseline", selected):
            candidate = next(c for c in plan["candidates"] if c["name"] == name)
            probabilities = fit_candidate(candidate, rows, features, final_split, indices)
            save_predictions(output / f"tier1-{name}.csv", rows, indices, probabilities)
            predictions[name] = probabilities
        actual = np.asarray([int(rows[i]["radiant_win"]) for i in indices])
        final = {
            "baseline": probability_metrics(actual, predictions["baseline"]),
            "candidate": probability_metrics(actual, predictions[selected]),
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
