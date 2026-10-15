"""Frozen paper selection experiments. No orders, bankroll mutations or later prices."""

import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import timedelta

from .domain import Prediction, timestamp, utcnow
from .presentation import market_snapshot

POLICIES = {
    "model_all": "Все фавориты модели",
    "market_all": "Все фавориты рынка",
    "value": "Перевес модели ≥5%",
    "consensus": "Elo + Glicko и перевес",
    "draft_agrees": "Игроки + драфт согласны",
    "market_agrees": "Модель + рынок согласны",
    "blend": "Смесь модели и рынка 50/50",
    "strong": "Модель ≥60% и перевес",
}
PROTOCOL = {
    "version": "profit-lab-v1",
    "policies": POLICIES,
    "minimum_odds": 1.30,
    "unit_stake": 1,
    "cost_stress": 0.02,
    "secondary_cost_stress": 0.05,
    "minimum_net_edge": 0.05,
    "strong_probability": 0.60,
    "blend_model_weight": 0.50,
    "quote_max_age_seconds": 30,
    "minimum_forward_trades": 100,
    "minimum_forward_clusters": 30,
    "familywise_alpha": 0.05,
    "bootstrap_samples": 5000,
    "bootstrap_seed": 71237,
    "automatic_promotion": False,
    "scope": "Polymarket original paired buy prices; early and delayed kept separate",
    "cost_note": "2% and 5% are stress assumptions, not measured market fees or fill costs",
}
PROTOCOL_HASH = hashlib.sha256(
    json.dumps(PROTOCOL, ensure_ascii=False, sort_keys=True).encode()
).hexdigest()


def shadow_probability(p, shadow, when):
    if not shadow or shadow.get("compared_to") != p.model_version:
        return None
    observed = timestamp(shadow.get("draft_observed_at"))
    recorded = timestamp(shadow.get("recorded_at"))
    value = shadow.get("radiant_probability")
    if (
        observed != p.draft.observed_at
        or not recorded
        or not observed <= recorded <= when
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value < 1
    ):
        return None
    return value


def decisions(p, when, shadow=None, shadow_as_of=None):
    """Inputs contain only the first forecast and observations available by `when`."""
    result = {key: {"take": False, "reason": "no_original_buy_pair"} for key in POLICIES}
    if (
        not p.draft.map_number
        or not math.isfinite(p.radiant_probability)
        or not 0 < p.radiant_probability < 1
    ):
        return result
    valid = [
        q
        for q in p.quotes
        if q.source == "Polymarket"
        and q.price_kind == "market"
        and q.price_origin == "live"
        and q.line is None
        and q.observed_at
        and q.observed_at <= when
        and q.fresh(when, PROTOCOL["quote_max_age_seconds"])
    ]
    own, other, market_p = market_snapshot(
        replace(p, quotes=valid, analysis={**p.analysis, "initial_odds_source": "Polymarket"}),
        PROTOCOL["quote_max_age_seconds"],
        when,
    )
    if not own or not other or market_p is None:
        return result
    for quote in (own, other):
        if (
            not quote.ask_odds
            or not math.isfinite(quote.ask_odds)
            or not 1 < quote.ask_odds <= 1_000_000
        ):
            return result
    quotes = {own.outcome: own, other.outcome: other}
    # The pair's displayed probabilities can sum to 0.99 or 1.01; normalize once.
    weights = {side: 1 / q.odds for side, q in quotes.items()}
    total = sum(weights.values())
    market = {side: value / total for side, value in weights.items()}
    model = {"radiant": p.radiant_probability, "dire": 1 - p.radiant_probability}
    favorite = p.favorite
    market_favorite = "radiant" if market["radiant"] >= market["dire"] else "dire"
    alternate = shadow_probability(p, shadow, shadow_as_of or when)
    for key in POLICIES:
        side = market_favorite if key == "market_all" else favorite
        probability = model[side]
        reason = ""
        if key == "consensus":
            if alternate is None:
                reason = "no_timely_shadow"
            elif (alternate >= 0.5) != (side == "radiant"):
                reason = "models_disagree"
            else:
                probability = min(probability, alternate if side == "radiant" else 1 - alternate)
        elif key == "draft_agrees" and (p.draft_probability >= 0.5) != (side == "radiant"):
            reason = "draft_disagrees"
        elif key == "market_agrees" and side != market_favorite:
            reason = "market_disagrees"
        elif key == "blend":
            probability = 0.5 * probability + 0.5 * market[side]
        elif key == "strong" and probability < PROTOCOL["strong_probability"]:
            reason = "weak_model"
        quote = quotes[side]
        odds = round(quote.ask_odds, 2)
        net_odds = odds / (1 + PROTOCOL["cost_stress"])
        edge = probability * net_odds - 1
        if not reason and odds < PROTOCOL["minimum_odds"]:
            reason = "below_minimum_odds"
        if (
            not reason
            and key not in ("model_all", "market_all")
            and edge < PROTOCOL["minimum_net_edge"]
        ):
            reason = "insufficient_net_edge"
        result[key] = {
            "take": not reason,
            "reason": reason,
            "side": side,
            "odds": odds,
            "model_probability": model[side],
            "selection_probability": probability,
            "market_probability": market[side],
            "net_edge": edge,
            "quote": quote.to_dict(),
        }
    return result


def cluster_key(p):
    if p.draft.series_id and str(p.draft.series_id) != "0":
        return f"{p.draft.league_id}:{p.draft.series_id}"
    teams = sorted(
        (str(p.draft.radiant_id or p.draft.radiant_name), str(p.draft.dire_id or p.draft.dire_name))
    )
    # Without a true series ID, merging repeat meetings is safer than treating
    # maps around midnight as independent series.
    return f"{p.draft.league_id}:{'|'.join(teams)}"


def summarize(entries, phase="historical"):
    rows = []
    for key, label in POLICIES.items():
        selected = [
            (entry, entry["decisions"][key]) for entry in entries if entry["decisions"][key]["take"]
        ]
        resolved = [
            (entry, pick) for entry, pick in selected if isinstance(entry.get("radiant_win"), bool)
        ]
        clusters = defaultdict(list)
        returns = {0: [], 0.02: [], 0.05: []}
        wins = 0
        for entry, pick in resolved:
            won = entry["radiant_win"] == (pick["side"] == "radiant")
            wins += won
            for cost in returns:
                returns[cost].append(pick["odds"] / (1 + cost) - 1 if won else -1)
            clusters[entry["cluster"]].append(returns[0.02][-1])
        n = len(resolved)
        intervals = None
        if len(clusters) >= 3:
            rng = random.Random(PROTOCOL["bootstrap_seed"])
            values = [(sum(items), len(items)) for items in clusters.values()]
            samples = []
            for _ in range(PROTOCOL["bootstrap_samples"]):
                draw = rng.choices(values, k=len(values))
                samples.append(sum(item[0] for item in draw) / sum(item[1] for item in draw))
            samples.sort()
            # Bonferroni family interval across the eight fixed policies.
            tail = PROTOCOL["familywise_alpha"] / len(POLICIES) / 2
            intervals = [
                samples[int(tail * len(samples))],
                samples[min(len(samples) - 1, int((1 - tail) * len(samples)))],
            ]
        profit = sum(returns[0.02])
        early = [entry for entry, _ in resolved if entry["timing"] == "early"]
        rows.append(
            {
                "policy": key,
                "name": label,
                "selected": len(selected),
                "settled": n,
                "pending": len(selected) - n,
                "wins": wins,
                "profit_units": profit,
                "roi": profit / n if n else None,
                "gross_roi": sum(returns[0]) / n if n else None,
                "stress_5pct_roi": sum(returns[0.05]) / n if n else None,
                "clusters": len(clusters),
                "family_ci": intervals,
                "early_settled": len(early),
                "skip_reasons": dict(
                    Counter(
                        entry["decisions"][key]["reason"]
                        for entry in entries
                        if not entry["decisions"][key]["take"]
                    )
                ),
                "review_candidate": bool(
                    phase == "forward"
                    and n >= PROTOCOL["minimum_forward_trades"]
                    and len(clusters) >= PROTOCOL["minimum_forward_clusters"]
                    and intervals
                    and intervals[0] > 0
                    and sum(returns[0.05]) > 0
                    and len(early) == n
                ),
            }
        )
    return {
        "phase": phase,
        "observations": len(entries),
        "timing": dict(Counter(entry["timing"] for entry in entries)),
        "policies": rows,
    }


class ProfitLab:
    def __init__(self, store):
        self.store = store

    def start(self):
        key = f"profit_lab_run:{PROTOCOL_HASH}"
        run = self.store.get(key)
        if not run:
            cutoff = self.store.db.execute("SELECT COALESCE(MAX(id),0) FROM forecasts").fetchone()[
                0
            ]
            run = {
                "id": PROTOCOL_HASH,
                "protocol": PROTOCOL,
                "started_at": utcnow().isoformat(),
                "first_forecast_id": cutoff + 1,
            }
            self.store.set(key, run)
        return run

    def record(self, forecast_id):
        run = self.start()
        key = f"profit_lab_decision:{run['id']}:{forecast_id}"
        if forecast_id < run["first_forecast_id"] or self.store.get(key):
            return
        forecast = self.store.forecast(forecast_id)
        if not forecast or forecast["result"]:
            return
        prediction = Prediction.from_dict(json.loads(forecast["payload"]))
        when = timestamp(forecast["created_at"])
        shadow = self.store.get(f"shadow_forecast:{prediction.draft.match_id}")
        decision_at = utcnow()
        # A shadow computed immediately after add_forecast is still contemporaneous.
        if (
            shadow
            and timestamp(shadow.get("recorded_at"))
            and timestamp(shadow["recorded_at"]) > when + timedelta(seconds=30)
        ):
            shadow = None
        # Quote time and shadow time are recorded separately; neither is backdated.
        picks = decisions(prediction, when, shadow, shadow_as_of=decision_at)
        self.store.set(
            key,
            {
                "forecast_id": forecast_id,
                "match_id": prediction.draft.match_id,
                "recorded_at": decision_at.isoformat(),
                "created_at": when.isoformat(),
                "timing": "early"
                if prediction.timing_status == "early" and not prediction.late_analysis
                else "delayed_or_unverified",
                "cluster": cluster_key(prediction),
                "decisions": picks,
            },
        )

    def entries(self):
        run = self.start()
        records = self.store.db.execute(
            "SELECT value FROM settings WHERE key LIKE ? ORDER BY key",
            (f"profit_lab_decision:{run['id']}:%",),
        ).fetchall()
        entries = []
        for row in records:
            entry = json.loads(row[0])
            forecast = self.store.forecast(entry["forecast_id"])
            if not forecast or forecast["state"] != "published":
                continue
            result = json.loads(forecast["result"]) if forecast["result"] else {}
            entries.append({**entry, "radiant_win": result.get("radiant_win")})
        return run, sorted(entries, key=lambda entry: entry["forecast_id"])
