"""Glicko-1 team ratings with inactivity uncertainty and safe JSON persistence."""

import math

Q = math.log(10) / 400


def glicko_g(rd):
    return 1 / math.sqrt(1 + 3 * Q**2 * rd**2 / math.pi**2)


def glicko_update(rating, rd, opponents):
    information = residual = 0.0
    for opponent, opponent_rd, won in opponents:
        g = glicko_g(opponent_rd)
        expected = 1 / (1 + math.exp(max(-35, min(35, -Q * g * (rating - opponent)))))
        information += Q**2 * g**2 * expected * (1 - expected)
        residual += g * (int(won) - expected)
    variance = 1 / (1 / rd**2 + information)
    return rating + Q * variance * residual, math.sqrt(variance)


class Glicko:
    def __init__(self, raw=None):
        self.values = {}
        for key, values in (raw or {}).items():
            if (
                len(values) != 3
                or not all(math.isfinite(v) for v in values)
                or not 30 <= values[1] <= 350
            ):
                raise ValueError("Invalid Glicko history")
            self.values[int(key)] = tuple(values)

    def export(self):
        return {str(key): list(value) for key, value in self.values.items()}

    def get(self, identity, when):
        rating, rd, last = self.values.get(identity, (1500.0, 350.0, when))
        days = max(0.0, when - last) / 86400
        rd = min(350.0, math.sqrt(rd**2 + (350**2 - 50**2) / 180 * days))
        return rating, rd

    def set_result(self, identity, own, other, won, when):
        rating, rd = glicko_update(*own, [(*other, won)])
        self.values[identity] = rating, max(30.0, rd), when

    def update_teams(self, radiant, dire, won, available):
        own, other = self.get(radiant, available), self.get(dire, available)
        self.set_result(radiant, own, other, won, available)
        self.set_result(dire, other, own, not won, available)

    def team_features(self, radiant, dire, when):
        r, d = self.get(radiant, when), self.get(dire, when)
        return {
            "team_elo": (r[0] - d[0]) / 400 * glicko_g(math.hypot(r[1], d[1])),
            "evidence:glicko_team_uncertainty": (r[1] - d[1]) / 350,
        }

    def squad(self, players, when):
        values = [self.get(p.account_id, when) if p.account_id else (1500, 350) for p in players]
        return sum(r for r, _ in values) / 5, math.sqrt(sum(rd**2 for _, rd in values) / 5)
