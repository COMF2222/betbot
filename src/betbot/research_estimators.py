"""Experimental learners, isolated from the published predictor and its model file."""

from collections import Counter

import numpy as np


def dense_numeric(x):
    return x.toarray() if hasattr(x, "toarray") else x


def csr32(x):
    value = x.tocsr(copy=True)
    if max(value.shape) >= 2**31 or value.nnz >= 2**31:
        raise ValueError("Research matrix exceeds the safe SAGA index limit")
    value.indices = value.indices.astype(np.int32)
    value.indptr = value.indptr.astype(np.int32)
    return value


def balance_weights(rows, indices, weights, mode):
    if not mode:
        return weights
    if mode == "league":
        groups = [rows[i]["leagueid"] for i in indices]
    elif mode == "series":
        groups = [
            (rows[i]["leagueid"], str(rows[i].get("series_id") or f"map:{rows[i]['match_id']}"))
            for i in indices
        ]
    elif mode == "patch":
        groups = [rows[i].get("patch") for i in indices]
    else:
        raise ValueError("Unknown sample balance")
    counts = Counter(groups)
    # Square-root reweighting for domains; exact inverse for correlated maps of a series.
    power = 1 if mode == "series" else 0.5
    factors = np.asarray([counts[g] ** (-power) for g in groups])
    factors /= factors.mean()
    factors = np.clip(factors, 0.25, 4)
    result = weights * factors
    return result / result.mean()


class RatingsOffsetClassifier:
    def __init__(self, rating_columns, C=0.15):
        self.rating_columns, self.C = rating_columns, C

    def fit(self, x, y, sample_weight=None):
        from scipy.optimize import minimize
        from scipy.special import expit
        from sklearn.linear_model import LogisticRegression

        weights = np.ones(len(y)) if sample_weight is None else sample_weight
        ratings = LogisticRegression(C=self.C, max_iter=1500, random_state=42)
        ratings.fit(x[:, self.rating_columns], y, sample_weight=weights)
        self.draft_columns = [i for i in range(x.shape[1]) if i not in self.rating_columns]
        draft = x[:, self.draft_columns]
        offset = ratings.decision_function(x[:, self.rating_columns])

        def objective(params):
            score = offset + draft @ params[:-1] + params[-1]
            residual = weights * (expit(score) - y)
            loss = weights @ (np.logaddexp(0, score) - y * score) + params[:-1] @ params[:-1] / (
                2 * self.C
            )
            gradient = np.concatenate(
                (np.asarray(draft.T @ residual).ravel() + params[:-1] / self.C, [residual.sum()])
            )
            return loss, gradient

        fitted = minimize(
            objective,
            np.zeros(draft.shape[1] + 1),
            jac=True,
            method="L-BFGS-B",
            options={"maxiter": 1000, "ftol": 1e-10},
        )
        if not fitted.success:
            raise ValueError(f"Draft residual optimization did not converge: {fitted.message}")
        self.coef_ = np.zeros((1, x.shape[1]))
        self.coef_[0, self.rating_columns] = ratings.coef_[0]
        self.coef_[0, self.draft_columns] = fitted.x[:-1]
        self.intercept_ = ratings.intercept_ + fitted.x[-1]
        return self

    def decision_function(self, x):
        return np.asarray(x @ self.coef_[0] + self.intercept_[0]).ravel()

    def predict_proba(self, x):
        from scipy.special import expit

        values = expit(self.decision_function(x))
        return np.column_stack((1 - values, values))


def estimator_for(candidate, names):
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import FunctionTransformer, SplineTransformer, StandardScaler

    kind = candidate["kind"]
    if kind == "linear":
        return LogisticRegression(C=candidate["C"], max_iter=1500, random_state=42), {}
    if kind == "elastic":
        # Installed/locked sklearn 1.9: l1_ratio selects elastic net without deprecated penalty.
        return Pipeline(
            [
                ("indices", FunctionTransformer(csr32)),
                (
                    "clf",
                    LogisticRegression(
                        C=candidate["C"],
                        l1_ratio=0.5,
                        solver="saga",
                        max_iter=3000,
                        random_state=42,
                    ),
                ),
            ]
        ), {"sample_weight_parameter": "clf__sample_weight"}
    if kind == "extra_trees":
        return ExtraTreesClassifier(
            n_estimators=300,
            max_depth=12,
            min_samples_leaf=40,
            max_features=0.6,
            criterion="log_loss",
            n_jobs=1,
            random_state=42,
        ), {}
    if kind == "mlp":
        return Pipeline(
            [
                ("scale", StandardScaler()),
                (
                    "clf",
                    MLPClassifier(
                        hidden_layer_sizes=(32,),
                        alpha=20,
                        solver="lbfgs",
                        max_iter=500,
                        max_fun=2000,
                        early_stopping=False,
                        random_state=42,
                    ),
                ),
            ]
        ), {"sample_weight_parameter": "clf__sample_weight"}
    if kind in ("offset", "spline"):
        columns = [
            i
            for i, name in enumerate(names)
            if name in ("team_elo", "player_elo", "hero_experience")
        ]
        if kind == "offset":
            return RatingsOffsetClassifier(columns, C=candidate["C"]), {}
        rest = [i for i in range(len(names)) if i not in columns]
        transformer = ColumnTransformer(
            [
                (
                    "curves",
                    Pipeline(
                        [
                            ("dense", FunctionTransformer(dense_numeric)),
                            (
                                "spline",
                                SplineTransformer(
                                    n_knots=5, degree=2, knots="quantile", extrapolation="linear"
                                ),
                            ),
                        ]
                    ),
                    columns,
                ),
                ("draft", "passthrough", rest),
            ],
            sparse_threshold=1.0,
        )
        return Pipeline(
            [
                ("shape", transformer),
                ("clf", LogisticRegression(C=candidate["C"], max_iter=1500, random_state=42)),
            ]
        ), {"sample_weight_parameter": "clf__sample_weight"}
    if kind == "hist_gradient":
        return HistGradientBoostingClassifier(
            learning_rate=0.05,
            l2_regularization=20,
            early_stopping=False,
            random_state=42,
            **{
                k: candidate[k]
                for k in ("max_depth", "max_leaf_nodes", "min_samples_leaf", "max_iter")
            },
        ), {}
    raise ValueError(f"Unknown estimator kind: {kind}")


def raw_scores(estimator, x):
    if hasattr(estimator, "decision_function"):
        return estimator.decision_function(x)
    p = np.clip(estimator.predict_proba(x)[:, 1], 1e-8, 1 - 1e-8)
    return np.log(p) - np.log1p(-p)
