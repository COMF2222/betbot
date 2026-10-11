"""Causal draft statistics. Only completed past games update this state."""

import itertools
from collections import Counter

from .domain import Draft, Player


def historical_roles(row: dict) -> dict[int, int]:
    """Approximate old roles from old lane/farm observations, never from current-game input."""
    slots, accounts = row.get("slots", []), row.get("accounts", [])
    lanes, gpms = row.get("lane_roles", []), row.get("gpms", [])
    if not (len(slots) == len(accounts) == len(lanes) == len(gpms)):
        return {}
    result = {}
    for radiant in (True, False):
        indices = [
            i
            for i, slot in enumerate(slots)
            if (int(slot) < 128) == radiant and accounts[i] and isinstance(gpms[i], (int, float))
        ]
        if len(indices) != 5:
            continue
        farmers = set(sorted(indices, key=lambda i: gpms[i], reverse=True)[:3])
        for lane, core, support in ((1, 1, 5), (2, 2, None), (3, 3, 4)):
            same_lane = [i for i in indices if lanes[i] == lane]
            cores = [i for i in same_lane if i in farmers]
            if cores:
                winner = max(cores, key=lambda i: gpms[i])
                result[int(accounts[winner])] = core
            if support:
                supports = [i for i in same_lane if i not in farmers]
                if supports:
                    winner = min(supports, key=lambda i: gpms[i])
                    result[int(accounts[winner])] = support
    return result


class DraftAnalytics:
    def __init__(self, raw: dict | None = None):
        raw = raw or {}
        self.roles = dict(raw.get("roles", {}))
        self.counters = dict(raw.get("counters", {}))
        self.pairs = dict(raw.get("pairs", {}))
        self.lanes = dict(raw.get("lanes", {}))
        self.mids = dict(raw.get("mids", {}))
        self.latest_patch = raw.get("latest_patch")

    def export(self):
        return {
            "roles": self.roles,
            "counters": self.counters,
            "pairs": self.pairs,
            "lanes": self.lanes,
            "mids": self.mids,
            "latest_patch": self.latest_patch,
        }

    @staticmethod
    def record(bucket, key, won, expected, gold=None, xp=None):
        stats = bucket.setdefault(
            key,
            {
                "n": 0,
                "wins": 0,
                "expected": 0.0,
                "gold_sum": 0.0,
                "gold_n": 0,
                "xp_sum": 0.0,
                "xp_n": 0,
            },
        )
        stats["n"] += 1
        stats["wins"] += int(won)
        stats["expected"] += expected
        if gold is not None:
            stats["gold_sum"] += max(-8000, min(8000, gold))
            stats["gold_n"] += 1
        if xp is not None:
            stats["xp_sum"] += max(-12000, min(12000, xp))
            stats["xp_n"] += 1

    @staticmethod
    def prefix(draft):
        return str(draft.patch) if draft.patch is not None else "unknown"

    def role(self, player: Player) -> dict:
        if player.position in (1, 2, 3, 4, 5):
            return {"position": player.position, "confidence": 1.0, "source": "provided"}
        observations = self.roles.get(str(player.account_id), [])
        if not observations:
            return {"position": None, "confidence": 0.0, "source": "unknown"}
        position, count = Counter(observations).most_common(1)[0]
        confidence = count / len(observations)
        return {
            "position": position if len(observations) >= 5 and confidence >= 0.7 else None,
            "confidence": confidence,
            "source": "past_lane_and_farm",
        }

    def assignments(self, draft: Draft) -> dict:
        result = {}
        for side, players in (("radiant", draft.radiant), ("dire", draft.dire)):
            rows = [
                {"account_id": p.account_id, "hero_id": p.hero_id, **self.role(p)} for p in players
            ]
            for position in range(1, 6):
                duplicate = [r for r in rows if r["position"] == position]
                if len(duplicate) > 1:
                    provided = [r for r in duplicate if r["source"] == "provided"]
                    for row in duplicate:
                        if len(provided) != 1 or row is not provided[0]:
                            row["position"] = None
                            row["source"] = "ambiguous"
            result[side] = rows
        return result

    @staticmethod
    def positioned(rows):
        return {r["position"]: r["hero_id"] for r in rows if r["position"]}

    @staticmethod
    def excess(stats, prior=40):
        return (stats["wins"] - stats["expected"]) / (stats["n"] + prior) if stats else 0.0

    def features(self, draft):
        prefix = self.prefix(draft)
        counter = []
        for r in draft.radiant:
            for d in draft.dire:
                a, b = sorted((r.hero_id, d.hero_id))
                stats = self.counters.get(f"{prefix}:{a}:{b}")
                counter.append(self.excess(stats) * (1 if r.hero_id == a else -1))
        pair = []
        for players, sign in ((draft.radiant, 1), (draft.dire, -1)):
            for a, b in itertools.combinations(sorted(p.hero_id for p in players), 2):
                pair.append(sign * self.excess(self.pairs.get(f"{prefix}:{a}:{b}")))
        roles = self.assignments(draft)
        rs, ds = self.positioned(roles["radiant"]), self.positioned(roles["dire"])
        lanes = []
        for positions, sign in ((rs, 1), (ds, -1)):
            for core, support in ((1, 5), (3, 4)):
                if core in positions and support in positions:
                    stats = self.lanes.get(
                        f"{prefix}:{core}:{positions[core]}:{positions[support]}"
                    )
                    lanes.append(sign * self.excess(stats, 30))
        mid_excess = mid_gold = 0.0
        if 2 in rs and 2 in ds:
            a, b = sorted((rs[2], ds[2]))
            stats = self.mids.get(f"{prefix}:{a}:{b}")
            sign = 1 if rs[2] == a else -1
            mid_excess = sign * self.excess(stats, 30)
            if stats and stats["gold_n"]:
                mid_gold = sign * stats["gold_sum"] / (stats["gold_n"] + 30) / 1000
        # One unit means ten percentage points of shrunk excess map win rate.
        return {
            "counter_advantage": 10 * sum(counter) / max(1, len(counter)),
            "pair_advantage": sum(pair),
            "lane_synergy": 5 * sum(lanes),
            "mid_matchup": 10 * mid_excess,
            "mid_gold10": mid_gold,
        }

    def update(self, draft, won, expected, context=None):
        context = context or {}
        prefix = self.prefix(draft)
        if draft.patch is not None:
            self.latest_patch = draft.patch
        for r in draft.radiant:
            for d in draft.dire:
                a, b = sorted((r.hero_id, d.hero_id))
                lower_radiant = r.hero_id == a
                self.record(
                    self.counters,
                    f"{prefix}:{a}:{b}",
                    won if lower_radiant else not won,
                    expected if lower_radiant else 1 - expected,
                )
        for players, outcome, chance in (
            (draft.radiant, won, expected),
            (draft.dire, not won, 1 - expected),
        ):
            for a, b in itertools.combinations(sorted(p.hero_id for p in players), 2):
                self.record(self.pairs, f"{prefix}:{a}:{b}", outcome, chance)
        actual = historical_roles(context)
        if not actual:
            return
        for account, position in actual.items():
            self.roles[str(account)] = (self.roles.get(str(account), []) + [position])[-20:]
        positions = [
            {actual[p.account_id]: p for p in players if p.account_id in actual}
            for players in (draft.radiant, draft.dire)
        ]
        checkpoints = {}
        for i, account in enumerate(context.get("accounts", [])):
            if account:
                checkpoints[int(account)] = {
                    name: (context.get(name) or [])[i] if i < len(context.get(name) or []) else None
                    for name in ("gold10", "xp10")
                }

        def difference(own, enemy, field):
            left = [checkpoints.get(p.account_id, {}).get(field) for p in own]
            right = [checkpoints.get(p.account_id, {}).get(field) for p in enemy]
            return (
                sum(left) - sum(right)
                if left and right and all(isinstance(v, (int, float)) for v in left + right)
                else None
            )

        if 2 in positions[0] and 2 in positions[1]:
            r, d = positions[0][2], positions[1][2]
            a, b = sorted((r.hero_id, d.hero_id))
            lower_radiant = r.hero_id == a
            own, enemy = ([r], [d]) if lower_radiant else ([d], [r])
            self.record(
                self.mids,
                f"{prefix}:{a}:{b}",
                won if lower_radiant else not won,
                expected if lower_radiant else 1 - expected,
                difference(own, enemy, "gold10"),
                difference(own, enemy, "xp10"),
            )
        for index, outcome, chance in ((0, won, expected), (1, not won, 1 - expected)):
            own, enemy = positions[index], positions[1 - index]
            for core, support, opposing_core, opposing_support in ((1, 5, 3, 4), (3, 4, 1, 5)):
                if core in own and support in own:
                    foes = [enemy[p] for p in (opposing_core, opposing_support) if p in enemy]
                    allies = [own[core], own[support]]
                    gold = difference(allies, foes, "gold10") if len(foes) == 2 else None
                    xp = difference(allies, foes, "xp10") if len(foes) == 2 else None
                    self.record(
                        self.lanes,
                        f"{prefix}:{core}:{own[core].hero_id}:{own[support].hero_id}",
                        outcome,
                        chance,
                        gold,
                        xp,
                    )

    def describe(self, draft, favorite):
        prefix = self.prefix(draft)
        roles = self.assignments(draft)
        favored = draft.radiant if favorite == "radiant" else draft.dire
        enemies = draft.dire if favorite == "radiant" else draft.radiant
        counters = []
        for player in favored:
            for enemy in enemies:
                a, b = sorted((player.hero_id, enemy.hero_id))
                stats = self.counters.get(f"{prefix}:{a}:{b}")
                if stats and stats["n"] >= 20:
                    sign = 1 if player.hero_id == a else -1
                    wins = stats["wins"] if sign == 1 else stats["n"] - stats["wins"]
                    counters.append(
                        {
                            "hero": player.hero_id,
                            "enemy": enemy.hero_id,
                            "count": stats["n"],
                            "win_rate": wins / stats["n"],
                            "excess": sign * self.excess(stats),
                        }
                    )
        counters.sort(key=lambda x: x["excess"], reverse=True)
        pairs = []
        for side, players, sign in (
            (favorite, favored, 1),
            ("dire" if favorite == "radiant" else "radiant", enemies, -1),
        ):
            for a, b in itertools.combinations(sorted(p.hero_id for p in players), 2):
                stats = self.pairs.get(f"{prefix}:{a}:{b}")
                if stats and stats["n"] >= 20:
                    pairs.append(
                        {
                            "side": side,
                            "heroes": [a, b],
                            "count": stats["n"],
                            "win_rate": stats["wins"] / stats["n"],
                            "advantage": sign * self.excess(stats),
                        }
                    )
        pairs.sort(key=lambda item: item["advantage"], reverse=True)
        lane_details = []
        positioned = self.positioned(roles[favorite])
        for core, support in ((1, 5), (3, 4)):
            if core in positioned and support in positioned:
                stats = self.lanes.get(f"{prefix}:{core}:{positioned[core]}:{positioned[support]}")
                if stats and stats["n"] >= 10:
                    lane_details.append(
                        {
                            "core": positioned[core],
                            "support": positioned[support],
                            "positions": [core, support],
                            "count": stats["n"],
                            "map_win_rate": stats["wins"] / stats["n"],
                            "gold10_mean": stats["gold_sum"] / stats["gold_n"]
                            if stats["gold_n"] >= 10
                            else None,
                            "gold10_count": stats["gold_n"],
                        }
                    )
        rs, ds = self.positioned(roles["radiant"]), self.positioned(roles["dire"])
        mid = None
        if 2 in rs and 2 in ds:
            a, b = sorted((rs[2], ds[2]))
            stats = self.mids.get(f"{prefix}:{a}:{b}")
            if stats and stats["n"] >= 10:
                sign = 1 if rs[2] == a else -1
                mid = {
                    "radiant_hero": rs[2],
                    "dire_hero": ds[2],
                    "count": stats["n"],
                    "gold10_mean_radiant": sign * stats["gold_sum"] / stats["gold_n"]
                    if stats["gold_n"] >= 10
                    else None,
                    "gold10_count": stats["gold_n"],
                }
        return {
            "roles": roles,
            "role_coverage": sum(
                r["position"] is not None for side in roles.values() for r in side
            ),
            "patch": draft.patch,
            "counters": counters[:2],
            "pairs": pairs[:3],
            "lanes": lane_details,
            "mid": mid,
        }
