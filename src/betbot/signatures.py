"""Hero familiarity and adjusted performance from completed pro maps only."""

import math

from .domain import Draft


class SignatureHistory:
    def __init__(self, raw=None):
        raw = raw or {}
        self.counts = dict(raw.get("counts", {}))
        self.recent = dict(raw.get("recent", {}))

    def export(self):
        return {"counts": self.counts, "recent": self.recent}

    def stats(self, key):
        # Maps, wins, and pre-map expected wins against those opponents.
        return self.counts.get(key, [0, 0, 0.0])

    @staticmethod
    def excess(stats, prior=20):
        n, wins, expected = stats
        return (wins - expected) / (n + prior)

    def recent_stats(self, key, when):
        previous = self.recent.get(key)
        if not previous:
            return [0.0, 0.0, 0.0]
        at, n, wins, expected = previous
        weight = math.exp(-math.log(2) * max(0, when - at) / (60 * 86400))
        return [n * weight, wins * weight, expected * weight]

    def player_values(self, player, draft):
        if not player.account_id:
            return dict.fromkeys(
                ("experience", "preference", "performance", "recent", "patch"), 0.0
            )
        player_key = f"player:{player.account_id}"
        hero_key = f"hero:{player.hero_id}"
        key = f"comfort:{player.account_id}:{player.hero_id}"
        n = self.stats(key)[0]
        confidence = n / (n + 20)
        values = {
            "experience": min(math.log1p(n) / math.log(101), 1.0),
            "preference": n / (self.stats(player_key)[0] + 12) * confidence,
            "performance": self.excess(self.stats(key))
            - confidence
            * (self.excess(self.stats(player_key)) + self.excess(self.stats(hero_key))),
            "recent": self.excess(self.recent_stats(key, draft.observed_at.timestamp())),
            "patch": 0.0,
        }
        if draft.patch is not None:
            prefix = f"patch:{draft.patch}:"
            stats = self.stats(prefix + key)
            certainty = stats[0] / (stats[0] + 20)
            values["patch"] = self.excess(stats) - certainty * (
                self.excess(self.stats(prefix + player_key))
                + self.excess(self.stats(prefix + hero_key))
            )
        return values

    def features(self, draft: Draft):
        values = dict.fromkeys(("experience", "preference", "performance", "recent", "patch"), 0.0)
        for players, sign in ((draft.radiant, 1), (draft.dire, -1)):
            # Unknown players contribute zero instead of changing the denominator.
            for player in players:
                for key, value in self.player_values(player, draft).items():
                    values[key] += sign * value / 5
        return {f"signature_{key}": value for key, value in values.items()}

    def update(self, draft, won, expected):
        when = draft.observed_at.timestamp()
        for players, outcome, chance in (
            (draft.radiant, won, expected),
            (draft.dire, not won, 1 - expected),
        ):
            for player in players:
                if not player.account_id:
                    continue
                keys = [
                    f"player:{player.account_id}",
                    f"hero:{player.hero_id}",
                    f"comfort:{player.account_id}:{player.hero_id}",
                ]
                if draft.patch is not None:
                    keys += [f"patch:{draft.patch}:{key}" for key in keys.copy()]
                for key in keys:
                    n, wins, previous_expected = self.stats(key)
                    self.counts[key] = [n + 1, wins + int(outcome), previous_expected + chance]
                key = keys[2]
                # Games can finish in a different order than they started.
                at = max(when, self.recent.get(key, [when])[0])
                n, wins, previous_expected = self.recent_stats(key, at)
                weight = math.exp(-math.log(2) * (at - when) / (60 * 86400))
                self.recent[key] = [
                    at,
                    n + weight,
                    wins + weight * int(outcome),
                    previous_expected + weight * chance,
                ]
