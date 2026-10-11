import copy
import hashlib
import heapq
import itertools
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

from .analytics import DraftAnalytics
from .domain import Draft, Player, Prediction, timestamp, utcnow
from .form import FormHistory, PlayerSkills
from .signatures import SignatureHistory

FEATURE_SETS = (
    "classic",
    "roster",
    "detailed",
    "form",
    "matchups",
    "adaptive",
    "players",
    "players_form",
    "signatures",
)


def sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-max(-35, min(35, value))))


class History:
    def __init__(self, raw: dict | None = None):
        raw = copy.deepcopy(raw or {})
        self.teams = defaultdict(lambda: 1500.0, raw.get("teams", {}))
        self.players = defaultdict(lambda: 1500.0, raw.get("players", {}))
        self.player_heroes = defaultdict(lambda: [0, 0], raw.get("player_heroes", {}))
        self.rosters = dict(raw.get("rosters", {}))
        self.games = int(raw.get("games", 0))
        self.analytics = DraftAnalytics(raw.get("analytics"))
        self.form = FormHistory(raw.get("form"))
        self.player_skills = PlayerSkills(raw.get("player_skills"))
        self.signatures = SignatureHistory(raw.get("signatures"))

    def export(self) -> dict:
        return {
            "teams": dict(self.teams),
            "players": dict(self.players),
            "player_heroes": dict(self.player_heroes),
            "games": self.games,
            "rosters": self.rosters,
            "analytics": self.analytics.export(),
            "form": self.form.records,
            "player_skills": self.player_skills.ratings,
            "signatures": self.signatures.export(),
        }

    def features(
        self, draft: Draft, combined: bool = True, feature_set: str = "classic"
    ) -> dict[str, float]:
        features: dict[str, float] = {}
        for players, sign in ((draft.radiant, 1), (draft.dire, -1)):
            heroes = sorted(p.hero_id for p in players)
            for hero in heroes:
                features[f"hero:{hero}"] = sign
            for a, b in itertools.combinations(heroes, 2):
                features[f"pair:{a}:{b}"] = sign * 0.5
        if draft.patch is not None:
            features[f"patch:{draft.patch}"] = 1
        if combined:
            features["team_elo"] = (
                self.teams[str(draft.radiant_id)] - self.teams[str(draft.dire_id)]
            ) / 400

            def mean_rating(players):
                ratings = [self.players[str(p.account_id)] for p in players if p.account_id]
                return sum(ratings) / len(ratings) if ratings else 1500.0

            features["player_elo"] = (mean_rating(draft.radiant) - mean_rating(draft.dire)) / 400

            def experience(players):
                values = [
                    self.player_heroes[f"{p.account_id}:{p.hero_id}"]
                    for p in players
                    if p.account_id
                ]
                return (
                    sum((wins + 3) / (games + 6) for wins, games in values) / len(values)
                    if values
                    else 0.5
                )

            features["hero_experience"] = experience(draft.radiant) - experience(draft.dire)
            if feature_set in ("roster", "form", "adaptive", "players_form"):

                def retention(team_id, players):
                    previous = set(self.rosters.get(str(team_id), []))
                    current = {p.account_id for p in players if p.account_id}
                    return len(previous & current) / len(current) if previous and current else 1.0

                r = retention(draft.radiant_id, draft.radiant)
                d = retention(draft.dire_id, draft.dire)
                rrating = r * self.teams[str(draft.radiant_id)] + (1 - r) * mean_rating(
                    draft.radiant
                )
                drating = d * self.teams[str(draft.dire_id)] + (1 - d) * mean_rating(draft.dire)
                features["roster_elo"] = (rrating - drating) / 400
                features["roster_retention"] = r - d
            if feature_set == "detailed":
                features.update(self.analytics.features(draft))
            if feature_set in ("form", "adaptive", "players_form"):
                features.update(self.form.features(draft))
            if feature_set in ("players", "players_form"):
                features.update(self.player_skills.features(draft, self.analytics))
            if feature_set == "signatures":
                features.update(self.signatures.features(draft))
            if feature_set in ("matchups", "adaptive"):
                detailed = self.analytics.features(draft)
                for key in ("counter_advantage", "pair_advantage"):
                    features[key] = detailed[key]
                if draft.patch is not None:
                    for players, sign in ((draft.radiant, 1), (draft.dire, -1)):
                        for player in players:
                            features[f"patch_hero:{draft.patch}:{player.hero_id}"] = sign
        return features

    def duration_features(self, draft: Draft) -> dict[str, float]:
        features = {f"hero:{p.hero_id}": 1.0 for p in draft.radiant + draft.dire}
        features["elo_gap"] = (
            abs(self.teams[str(draft.radiant_id)] - self.teams[str(draft.dire_id)]) / 400
        )
        if draft.patch is not None:
            features[f"patch:{draft.patch}"] = 1
        return features

    def hero_comfort(self, draft: Draft) -> dict:
        """Pro-map samples for the ten actual player/hero pairs, including misses."""
        accounts = {p.account_id for p in draft.radiant + draft.dire if p.account_id}
        totals = dict.fromkeys(accounts, 0)
        for key, (_, games) in self.player_heroes.items():
            account = int(key.split(":")[0])
            if account in totals:
                totals[account] += games
        result = {}
        for side in ("radiant", "dire"):
            result[side] = []
            for player in getattr(draft, side):
                wins, games = self.player_heroes.get(
                    f"{player.account_id}:{player.hero_id}", [0, 0]
                )
                result[side].append(
                    {
                        "account_id": player.account_id,
                        "hero_id": player.hero_id,
                        "name": player.name,
                        "count": games,
                        "wins": wins,
                        "win_rate": wins / games if games else None,
                        "smoothed_win_rate": (wins + 3) / (games + 6),
                        "pick_share": games / totals[player.account_id]
                        if totals.get(player.account_id)
                        else None,
                    }
                )
        return result

    def update(self, draft: Draft, radiant_win: bool, context: dict | None = None) -> None:
        rid, did = str(draft.radiant_id), str(draft.dire_id)
        expected = 1 / (1 + 10 ** ((self.teams[did] - self.teams[rid]) / 400))
        self.form.update(draft, radiant_win, expected)
        self.signatures.update(draft, radiant_win, expected)
        self.player_skills.update(draft, radiant_win)
        self.analytics.update(draft, radiant_win, expected, context)
        change = 24 * (int(radiant_win) - expected)
        self.teams[rid] += change
        self.teams[did] -= change
        self.rosters[rid] = [p.account_id for p in draft.radiant if p.account_id]
        self.rosters[did] = [p.account_id for p in draft.dire if p.account_id]
        for players, won in ((draft.radiant, radiant_win), (draft.dire, not radiant_win)):
            for p in players:
                if not p.account_id:
                    continue
                key = str(p.account_id)
                self.players[key] += (
                    (10 * (int(won) - expected))
                    if won == radiant_win
                    else (10 * (int(won) - (1 - expected)))
                )
                stats = self.player_heroes[f"{p.account_id}:{p.hero_id}"]
                stats[0] += int(won)
                stats[1] += 1
        self.games += 1


def historical_draft(row: dict) -> Draft | None:
    try:
        slots, heroes, accounts = row["slots"], row["heroes"], row["accounts"]
        if len(slots) != len(heroes) or len(heroes) != len(accounts):
            return None
        triples = list(zip(slots, heroes, accounts, strict=True))
        radiant = tuple(Player(int(a or 0), int(h or 0)) for s, h, a in triples if int(s) < 128)
        dire = tuple(Player(int(a or 0), int(h or 0)) for s, h, a in triples if int(s) >= 128)
        start = timestamp(int(row["start_time"]))
        draft = Draft(
            str(row["match_id"]),
            int(row["leagueid"]),
            int(row["radiant_team_id"]),
            int(row["dire_team_id"]),
            row.get("radiant_team_name") or "Radiant",
            row.get("dire_team_name") or "Dire",
            radiant,
            dire,
            start,
            start,
            0,
            str(row.get("series_id") or ""),
            patch=row.get("patch"),
        )
        if (
            not draft.complete
            or not isinstance(row.get("radiant_win"), bool)
            or not 300 <= int(row["duration"]) <= 12000
        ):
            return None
        return draft
    except (KeyError, ValueError, TypeError):
        return None


def chronological_split(rows: list[dict]) -> tuple[list[int], list[int], list[int]]:
    """Purge series that cross temporal boundaries and labels finishing after a boundary."""
    n = len(rows)
    if n < 150:
        raise ValueError("Для обучения нужно минимум 150 полных исторических карт")
    boundary_a = int(rows[int(n * 0.6)]["start_time"])
    boundary_b = int(rows[int(n * 0.8)]["start_time"])
    groups: dict[str, set[int]] = defaultdict(set)
    folds = []
    for row in rows:
        t = int(row["start_time"])
        fold = 0 if t < boundary_a else 1 if t < boundary_b else 2
        folds.append(fold)
        key = f"series:{row['series_id']}" if row.get("series_id") else f"match:{row['match_id']}"
        groups[key].add(fold)
    indices: list[list[int]] = [[], [], []]
    for i, row in enumerate(rows):
        key = f"series:{row['series_id']}" if row.get("series_id") else f"match:{row['match_id']}"
        fold = folds[i]
        if len(groups[key]) != 1:
            continue
        # Include a small result-availability buffer as well as actual game duration.
        finish = int(row["start_time"]) + int(row["duration"]) + 60
        if (fold == 0 and finish >= boundary_a) or (fold == 1 and finish >= boundary_b):
            continue
        indices[fold].append(i)
    if min(map(len, indices)) < 25:
        raise ValueError("После разделения по времени и сериям недостаточно независимых карт")
    return tuple(indices)


def linear_export(estimator, vectorizer) -> dict:
    weights = np.asarray(estimator.coef_).reshape(-1)
    return {
        "weights": {
            name: float(weight)
            for name, weight in zip(vectorizer.get_feature_names_out(), weights, strict=True)
        },
        "intercept": float(np.asarray(estimator.intercept_).reshape(-1)[0]),
    }


def linear(raw: dict, features: dict) -> float:
    return raw["intercept"] + sum(
        raw["weights"].get(name, 0) * value for name, value in features.items()
    )


def fit(rows: list[dict], output: Path, feature_set: str = "classic") -> dict:
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.metrics import brier_score_loss, log_loss

    if feature_set not in FEATURE_SETS:
        raise ValueError("Неизвестный набор признаков")

    unique = {str(r.get("match_id")): r for r in rows if historical_draft(r)}
    rows = sorted(unique.values(), key=lambda r: (int(r["start_time"]), int(r["match_id"])))
    now = int(utcnow().timestamp())
    rows = [r for r in rows if int(r["start_time"]) + int(r["duration"]) + 60 <= now]
    train, calibration, test = chronological_split(rows)
    history = History()
    combined, draft_only, duration_features, labels, durations = [], [], [], [], []
    completions = []
    for i, row in enumerate(rows):
        draft = historical_draft(row)
        start = int(row["start_time"])
        while completions and completions[0][0] < start:
            _, prior_i = heapq.heappop(completions)
            prior = rows[prior_i]
            history.update(historical_draft(prior), prior["radiant_win"], prior)
        combined.append(history.features(draft, feature_set=feature_set))
        draft_only.append(history.features(draft, combined=False))
        duration_features.append(history.duration_features(draft))
        labels.append(int(row["radiant_win"]))
        durations.append(int(row["duration"]))
        heapq.heappush(completions, (start + int(row["duration"]) + 60, i))
    # Only finalized past results populate the state used for future predictions.
    while completions and completions[0][0] <= now:
        _, prior_i = heapq.heappop(completions)
        history.update(historical_draft(rows[prior_i]), rows[prior_i]["radiant_win"], rows[prior_i])
    y = np.asarray(labels)
    artifacts = {}
    test_predictions = {}
    for name, features in (("combined", combined), ("draft", draft_only)):
        vectorizer = DictVectorizer()
        x_train = vectorizer.fit_transform([features[i] for i in train])
        if len(set(y[train])) < 2 or len(set(y[calibration])) < 2:
            raise ValueError("В обучении и калибровке должны быть оба исхода")
        estimator = LogisticRegression(C=0.15, max_iter=1500, random_state=42)
        estimator.fit(x_train, y[train])
        x_cal = vectorizer.transform([features[i] for i in calibration])

        def calibration_inputs(indices, scores, model_name=name):
            if model_name == "combined":
                return np.column_stack(
                    (
                        scores,
                        [combined[i].get("team_elo", 0) * math.log(10) for i in indices],
                        [combined[i].get("player_elo", 0) for i in indices],
                    )
                )
            return scores.reshape(-1, 1)

        cal = LogisticRegression(C=1.0, random_state=42)
        cal.fit(calibration_inputs(calibration, estimator.decision_function(x_cal)), y[calibration])
        x_test = vectorizer.transform([features[i] for i in test])
        prob = cal.predict_proba(calibration_inputs(test, estimator.decision_function(x_test)))[
            :, 1
        ]
        test_predictions[name] = prob
        artifacts[name] = {
            **linear_export(estimator, vectorizer),
            "calibration": {
                "weight": float(cal.coef_[0][0]),
                "intercept": float(cal.intercept_[0]),
                "elo_weight": float(cal.coef_[0][1]) if name == "combined" else 0.0,
                "player_weight": float(cal.coef_[0][2]) if name == "combined" else 0.0,
            },
        }
    vectorizer = DictVectorizer()
    ridge = Ridge(alpha=100, solver="lsqr")
    ridge.fit(
        vectorizer.fit_transform([duration_features[i] for i in train]),
        np.asarray(durations)[train],
    )
    residuals = np.asarray(durations)[calibration] - ridge.predict(
        vectorizer.transform([duration_features[i] for i in calibration])
    )
    duration_test = ridge.predict(vectorizer.transform([duration_features[i] for i in test]))
    baseline = np.asarray([1 / (1 + 10 ** (-combined[i].get("team_elo", 0))) for i in test])
    brier = float(brier_score_loss(y[test], test_predictions["combined"]))
    baseline_brier = float(brier_score_loss(y[test], baseline))
    bins = []
    for lower in np.arange(0, 1, 0.1):
        selected = (test_predictions["combined"] >= lower) & (
            test_predictions["combined"] < lower + 0.1
        )
        if selected.any():
            bins.append(
                {
                    "from": round(float(lower), 1),
                    "to": round(float(lower + 0.1), 1),
                    "count": int(selected.sum()),
                    "mean_probability": float(test_predictions["combined"][selected].mean()),
                    "observed_win_rate": float(y[test][selected].mean()),
                }
            )
    report = {
        "rows": len(rows),
        "feature_set": feature_set,
        "train": len(train),
        "calibration": len(calibration),
        "test": len(test),
        "purged": len(rows) - len(train) - len(calibration) - len(test),
        "first_start": timestamp(int(rows[0]["start_time"])).isoformat(),
        "last_start": timestamp(int(rows[-1]["start_time"])).isoformat(),
        "test_start": timestamp(int(rows[test[0]]["start_time"])).isoformat(),
        "brier": brier,
        "team_elo_brier": baseline_brier,
        "draft_brier": float(brier_score_loss(y[test], test_predictions["draft"])),
        "log_loss": float(log_loss(y[test], test_predictions["combined"])),
        "accuracy": float(((test_predictions["combined"] >= 0.5) == y[test]).mean()),
        "duration_mae_minutes": float(
            np.abs(np.asarray(durations)[test] - duration_test).mean() / 60
        ),
        "calibration_bins": bins,
        "reliability": {},
        "publication_ready": bool(len(test) >= 200 and brier < baseline_brier),
        "limitations": [
            "Результаты относятся к этой временной выборке, а не гарантируют будущую точность",
            "Роли и патч используются только при наличии этих полей",
            "Тоталы используют распределение ошибок калибровочного периода",
            "Исторических котировок нет: доходность не измерена",
        ],
    }
    for name, probabilities in test_predictions.items():
        confidence = np.maximum(probabilities, 1 - probabilities)
        correct = (probabilities >= 0.5) == y[test]
        buckets = []
        for lower, upper in (
            (0.5, 0.55),
            (0.55, 0.6),
            (0.6, 0.65),
            (0.65, 0.7),
            (0.7, 0.8),
            (0.8, 1.01),
        ):
            chosen = (confidence >= lower) & (confidence < upper)
            buckets.append(
                {
                    "from": lower,
                    "to": min(1, upper),
                    "count": int(chosen.sum()),
                    "success_rate": float(correct[chosen].mean()) if chosen.any() else None,
                }
            )
        report["reliability"][name] = buckets
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "ids": [r["match_id"] for r in rows],
                "models": artifacts,
                "duration": linear_export(ridge, vectorizer),
                "history": history.export(),
                "feature_set": feature_set,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:12]
    artifact = {
        "schema": 1,
        "version": f"{ {'classic': 'logistic-v2', 'roster': 'roster-v1', 'detailed': 'draft-v3', 'form': 'form-v1', 'matchups': 'matchups-v1', 'adaptive': 'adaptive-v1', 'players': 'players-v1', 'players_form': 'players-form-v1', 'signatures': 'signatures-v1'}[feature_set] }-{fingerprint}",
        "feature_set": feature_set,
        "created_at": utcnow().isoformat(),
        "history_cutoff": timestamp(
            max(int(r["start_time"]) + int(r["duration"]) + 60 for r in rows)
        ).isoformat(),
        "history": history.export(),
        "training_match_ids": [str(row["match_id"]) for row in rows],
        "models": artifacts,
        "duration": {**linear_export(ridge, vectorizer), "residuals": residuals.tolist()},
        "report": report,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        previous = json.loads(output.read_text(encoding="utf-8"))
        archive = output.parent / "models"
        archive.mkdir(exist_ok=True)
        (archive / (previous["version"] + ".json")).write_text(
            json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    output.with_name("model-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


class Model:
    def __init__(self, path: Path):
        self.path = path
        self.raw = None
        self.mtime = None
        self.reload()

    def reload(self):
        if not self.path.exists():
            self.raw = None
            return
        mtime = self.path.stat().st_mtime_ns
        if mtime != self.mtime:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("schema") != 1:
                raise ValueError("Неизвестная версия модели")
            self.raw, self.mtime = raw, mtime

    @property
    def ready(self) -> bool:
        return bool(self.raw)

    @property
    def publication_ready(self) -> bool:
        return bool(self.raw and self.raw["report"]["publication_ready"])

    def predict(
        self, draft: Draft, history: History | None = None, history_as_of: str = ""
    ) -> Prediction:
        self.reload()
        if not self.raw:
            raise ValueError("Модель ещё не обучена: выполните betbot train")
        if draft.match_id in self.raw.get("training_match_ids", []):
            raise ValueError("Результат этой карты уже присутствует в истории обучения")
        cutoff = timestamp(self.raw["history_cutoff"])
        if draft.observed_at < cutoff:
            raise ValueError("Эта модель обучена на данных позже момента карты")
        if history_as_of and timestamp(history_as_of) > draft.observed_at:
            raise ValueError("История содержит события позже момента карты")
        history = history or History(self.raw["history"])
        features = history.features(draft, feature_set=self.raw.get("feature_set", "classic"))
        probabilities = {}
        for name in ("combined", "draft"):
            spec = self.raw["models"][name]
            x = features if name == "combined" else history.features(draft, combined=False)
            probabilities[name] = sigmoid(
                spec["calibration"]["weight"] * linear(spec, x)
                + spec["calibration"].get("elo_weight", 0) * x.get("team_elo", 0) * math.log(10)
                + spec["calibration"].get("player_weight", 0) * x.get("player_elo", 0)
                + spec["calibration"]["intercept"]
            )
        favorite_radiant = probabilities["combined"] >= 0.5
        favorite_players = draft.radiant if favorite_radiant else draft.dire
        opponent_players = draft.dire if favorite_radiant else draft.radiant
        reasons = []
        elo_delta = features["team_elo"] * 400 * (1 if favorite_radiant else -1)
        reasons.append(
            f"Разница рейтинга команд по прошлым картам, фаворит минус соперник: {elo_delta:+.0f} Elo."
        )
        played = sum(
            history.player_heroes[f"{p.account_id}:{p.hero_id}"][1] for p in favorite_players
        )
        against = sum(
            history.player_heroes[f"{p.account_id}:{p.hero_id}"][1] for p in opponent_players
        )
        reasons.append(
            f"Про-карты игроков на выбранных героях в истории: {played} у фаворита, {against} у соперника."
        )
        spec = self.raw["models"]["combined"]
        contributions = sorted(
            (
                (
                    key,
                    value
                    * spec["weights"].get(key, 0)
                    * spec["calibration"]["weight"]
                    * (1 if favorite_radiant else -1),
                )
                for key, value in features.items()
                if key.startswith("hero:")
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        if contributions and contributions[0][1] > 0:
            reasons.append(
                f"Наиболее сильный положительный признак героев: hero:{contributions[0][0].split(':')[1]} (связь в модели, не гарантия контрпика)."
            )
        analysis = history.analytics.describe(draft, "radiant" if favorite_radiant else "dire")
        analysis["comfort"] = history.hero_comfort(draft)
        analysis["hero_experience_used_in_model"] = bool(spec["weights"].get("hero_experience", 0))
        reasons.append(
            f"Позиции по предыдущим играм определены для {analysis['role_coverage']} из 10 игроков; перестановки на текущей карте возможны."
        )
        for item in analysis["counters"]:
            reasons.append(
                f"hero:{item['hero']} против hero:{item['enemy']}: {item['win_rate']:.0%} побед на карте, {item['count']} прошлых про-карт текущего крупного патча. Это наблюдение, не причинный эффект контрпика."
            )
        for item in analysis["lanes"]:
            label = f"Связка позиций {item['positions'][0]}+{item['positions'][1]}: hero:{item['core']} + hero:{item['support']}"
            value = f"{item['map_win_rate']:.0%} побед на карте, {item['count']} прошлых карт"
            if item["gold10_mean"] is not None:
                value += f"; разница накопленного золота пары к 10-й минуте {item['gold10_mean']:+.0f}, {item['gold10_count']} измерений"
            reasons.append(f"{label}: {value}. Линия предполагается по прошлым ролям.")
        mid = analysis["mid"]
        if mid:
            value = f"Мид hero:{mid['radiant_hero']} против hero:{mid['dire_hero']}: {mid['count']} прошлых карт"
            if mid["gold10_mean_radiant"] is not None:
                value += f"; разница накопленного золота к 10-й минуте {mid['gold10_mean_radiant']:+.0f} у героя света, {mid['gold10_count']} измерений"
            reasons.append(
                value + ". Это экономика игрока к 10-й минуте, не вероятность выиграть линию."
            )
        feature_set = self.raw.get("feature_set", "classic")
        analysis["model_features"] = [
            key
            for key in features
            if key
            in (
                "counter_advantage",
                "pair_advantage",
                "lane_synergy",
                "mid_matchup",
                "mid_gold10",
                "recent_team_form",
                "recent_player_form",
                "recent_comfort_form",
                "recent_lineup_form",
                "lineup_player_elo",
                "core_player_elo",
                "mid_player_elo",
                "roster_elo",
            )
            or key.startswith("signature_")
        ]
        analysis["used_in_model"] = feature_set == "detailed"
        if feature_set in ("matchups", "adaptive"):
            reasons.append(
                "Модель использует агрегаты контрпиков и сочетаний с поправкой на силу команд. Статистика линий и мида остаётся справочной."
            )
        elif feature_set == "signatures":
            reasons.append(
                "Модель учитывает частоту выбора героя игроком, опыт, недавние результаты и текущий патч с поправкой на соперников и объём выборки."
            )
        elif not analysis["used_in_model"]:
            reasons.append(
                "Статистика контрпиков и линий приведена справочно; её отдельные признаки пока не прошли сравнительный тест модели."
            )
        duration = self.raw["duration"]
        center = max(300, linear(duration, history.duration_features(draft)))
        residuals = np.asarray(duration["residuals"])
        interval = (
            max(300, center + float(np.quantile(residuals, 0.1))),
            max(300, center + float(np.quantile(residuals, 0.9))),
        )
        return Prediction(
            draft,
            probabilities["combined"],
            probabilities["draft"],
            self.raw["version"],
            reasons,
            "оценка модели; частота успеха отдельно проверена на прошлом временном тесте",
            duration_seconds=center,
            duration_interval=interval,
            publication_ready=self.publication_ready,
            analysis=analysis,
            features=features,
            history_as_of=history_as_of or self.raw["history_cutoff"],
            reliability={
                name: next(
                    (
                        b
                        for b in self.raw["report"].get("reliability", {}).get(name, [])
                        if b["from"] <= max(probabilities[name], 1 - probabilities[name]) < b["to"]
                        or max(probabilities[name], 1 - probabilities[name]) == b["to"] == 1
                    ),
                    {},
                )
                for name in ("combined", "draft")
            },
        )

    def duration_probability(
        self, prediction: Prediction, line_minutes: float, outcome: str
    ) -> float:
        residuals = np.asarray(self.raw["duration"]["residuals"])
        samples = prediction.duration_seconds + residuals
        if outcome == "over":
            return float((samples > line_minutes * 60).mean())
        if outcome == "under":
            return float((samples < line_minutes * 60).mean())
        raise ValueError("Неизвестный исход тотала")
