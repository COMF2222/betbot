"""Recent form from completed games; identifiers and time come from the blind draft."""

import math

from .domain import Draft


class FormHistory:
    def __init__(self, raw=None):
        self.records = dict(raw or {})

    @staticmethod
    def keys(team_id, players):
        keys = {"team": [f"team:{team_id}"], "player": [], "comfort": [], "lineup": []}
        for player in players:
            if player.account_id:
                keys["player"].append(f"player:{player.account_id}")
                keys["comfort"].append(f"comfort:{player.account_id}:{player.hero_id}")
        accounts = sorted(p.account_id for p in players if p.account_id)
        if len(accounts) == 5 and len(set(accounts)) == 5:
            keys["lineup"] = ["lineup:" + ":".join(map(str, accounts))]
        return keys

    def value(self, key, observed_at):
        weighted, count = 0.0, 0.0
        for when, residual in self.records.get(key, []):
            if when >= observed_at:
                continue
            # Half of a game's weight remains after 30 days; at most 20 past games.
            weight = math.exp(-math.log(2) * (observed_at - when) / (30 * 86400))
            weighted += weight * residual
            count += weight
        return weighted / (count + 8)

    def features(self, draft: Draft):
        values = {name: 0.0 for name in ("team", "player", "comfort", "lineup")}
        when = draft.observed_at.timestamp()
        for team, players, sign in (
            (draft.radiant_id, draft.radiant, 1),
            (draft.dire_id, draft.dire, -1),
        ):
            for name, keys in self.keys(team, players).items():
                values[name] += (
                    sign * sum(self.value(key, when) for key in keys) / max(1, len(keys))
                )
        return {f"recent_{name}_form": value * 5 for name, value in values.items()}

    def update(self, draft, won, expected):
        when = draft.observed_at.timestamp()
        for team, players, outcome, chance in (
            (draft.radiant_id, draft.radiant, won, expected),
            (draft.dire_id, draft.dire, not won, 1 - expected),
        ):
            for keys in self.keys(team, players).values():
                for key in keys:
                    self.records[key] = (
                        self.records.get(key, []) + [[when, int(outcome) - chance]]
                    )[-20:]


class PlayerSkills:
    """Player ratings compared to the five actual opponents, independent of team IDs."""

    def __init__(self, raw=None):
        self.ratings = dict(raw or {})

    def mean(self, accounts):
        values = [self.ratings.get(str(account), 1500.0) for account in accounts if account]
        return sum(values) / len(values) if values else 1500.0

    def features(self, draft, analytics):
        radiant = [p.account_id for p in draft.radiant]
        dire = [p.account_id for p in draft.dire]
        features = {"lineup_player_elo": (self.mean(radiant) - self.mean(dire)) / 400}
        roles = analytics.assignments(draft)
        positions = {
            side: {r["position"]: r["account_id"] for r in rows if r["position"]}
            for side, rows in roles.items()
        }
        for key, selected in (("core_player_elo", (1, 2, 3)), ("mid_player_elo", (2,))):
            matched = [p for p in selected if p in positions["radiant"] and p in positions["dire"]]
            features[key] = (
                (
                    self.mean([positions["radiant"][p] for p in matched])
                    - self.mean([positions["dire"][p] for p in matched])
                )
                / 400
                if matched
                else 0.0
            )
        return features

    def update(self, draft, won):
        radiant = [p.account_id for p in draft.radiant]
        dire = [p.account_id for p in draft.dire]
        expected = 1 / (1 + 10 ** ((self.mean(dire) - self.mean(radiant)) / 400))
        change = 20 * (int(won) - expected)
        for accounts, sign in ((radiant, 1), (dire, -1)):
            for account in accounts:
                if account:
                    key = str(account)
                    self.ratings[key] = self.ratings.get(key, 1500.0) + sign * change
