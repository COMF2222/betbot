"""New causal research inputs. Only completed earlier maps may update this state."""

import heapq
import itertools
import math
from collections import Counter, defaultdict

from .backtest import score_event
from .glicko import Glicko, glicko_g
from .glicko import glicko_update as glicko_update
from .model import historical_draft

EVEN_EVIDENCE_FEATURES = frozenset(("evidence:roster_min", "evidence:tier1"))
EVIDENCE_VIEWS = frozenset(
    (
        "glicko_team",
        "glicko_player",
        "decayed_ratings",
        "event_form",
        "h2h_form",
        "supported_pairs",
        "supported_triples",
        "player_hero_identity",
        "team_hero_identity",
        "counter_extremes",
    )
)


def evidence_view(features, base, name):
    result = dict(base)
    if name in ("glicko_team", "glicko_player"):
        side = name.removeprefix("glicko_")
        result[f"{side}_elo"] = features[f"evidence:{name}"]
        result[f"evidence:{name}_uncertainty"] = features[f"evidence:{name}_uncertainty"]
    elif name == "decayed_ratings":
        result["team_elo"] = features["evidence:decayed_team"]
        result["player_elo"] = features["evidence:decayed_player"]
    elif name in ("event_form", "h2h_form", "counter_extremes"):
        keys = (
            (
                f"evidence:{name.removesuffix('_form')}_residual",
                f"evidence:{name.removesuffix('_form')}_winrate",
            )
            if name.endswith("_form")
            else tuple(
                f"evidence:{k}_{part}"
                for k in ("counter", "synergy")
                for part in ("strongest", "weakest")
            )
        )
        result.update({k: features[k] for k in keys})
    else:
        prefix = {
            "supported_pairs": "support_pair:",
            "supported_triples": "triple:",
            "player_hero_identity": "identity_player_hero:",
            "team_hero_identity": "identity_team_hero:",
        }[name]
        if name == "supported_pairs":
            result = {k: v for k, v in result.items() if not k.startswith("pair:")}
        result.update(
            {
                (k.replace("support_pair:", "pair:") if name == "supported_pairs" else k): v
                for k, v in features.items()
                if k.startswith(prefix)
            }
        )
    return result


class EvidenceHistory:
    def __init__(self):
        self.teams, self.players = Glicko(), Glicko()
        self.decaying = {}
        self.rosters = {}
        self.pairs, self.triples, self.player_heroes, self.team_heroes = (
            Counter() for _ in range(4)
        )
        self.counters, self.synergies = {}, {}
        self.events, self.head_to_head = defaultdict(list), defaultdict(list)

    def decayed_rating(self, key, when):
        rating, last = self.decaying.get(key, (1500.0, when))
        return 1500 + (rating - 1500) * 2 ** (-max(0.0, when - last) / (180 * 86400))

    @staticmethod
    def roster(players):
        return frozenset(p.account_id for p in players if p.account_id)

    @staticmethod
    def retention(old, current):
        return len(old & current) / 5 if len(old) == len(current) == 5 else 0.0

    @staticmethod
    def residual(stats):
        return (stats[1] - stats[2]) / (stats[0] + 40) if stats else 0.0

    @staticmethod
    def record(bucket, key, won, expected):
        old = bucket.setdefault(key, [0, 0.0, 0.0])
        old[0] += 1
        old[1] += int(won)
        old[2] += expected

    def features(self, draft):
        now, prefix = draft.observed_at.timestamp(), str(draft.patch)
        rteam, dteam = self.teams.get(draft.radiant_id, now), self.teams.get(draft.dire_id, now)
        rplayer, dplayer = (
            self.players.squad(draft.radiant, now),
            self.players.squad(draft.dire, now),
        )
        result = {}
        for name, r, d in (("team", rteam, dteam), ("player", rplayer, dplayer)):
            result[f"evidence:glicko_{name}"] = (
                (r[0] - d[0]) / 400 * glicko_g(math.hypot(r[1], d[1]))
            )
            result[f"evidence:glicko_{name}_uncertainty"] = (r[1] - d[1]) / 350
        retentions = []
        event_residual = event_wins = 0.0
        rsynergies, dsynergies = [], []
        for team, players, sign in (
            (draft.radiant_id, draft.radiant, 1),
            (draft.dire_id, draft.dire, -1),
        ):
            current = self.roster(players)
            retention = self.retention(self.rosters.get(team, frozenset()), current)
            retentions.append(retention)
            result["evidence:decayed_team"] = (
                result.get("evidence:decayed_team", 0.0)
                + sign * self.decayed_rating(("team", team), now) / 400
            )
            mean_player = (
                sum(
                    self.decayed_rating(("player", p.account_id), now) if p.account_id else 1500
                    for p in players
                )
                / 5
            )
            result["evidence:decayed_player"] = (
                result.get("evidence:decayed_player", 0.0) + sign * mean_player / 400
            )
            heroes = sorted(p.hero_id for p in players)
            for pair in itertools.combinations(heroes, 2):
                n = self.pairs[pair]
                result["support_pair:" + ":".join(map(str, pair))] = sign * 0.5 * n / (n + 40)
                (rsynergies if sign == 1 else dsynergies).append(
                    self.residual(self.synergies.get((prefix, *pair)))
                )
            for triple in itertools.combinations(heroes, 3):
                n = self.triples[triple]
                if n >= 12:
                    result["triple:" + ":".join(map(str, triple))] = sign * 0.25 * n / (n + 30)
            for p in players:
                if p.account_id:
                    n = self.player_heroes[p.account_id, p.hero_id]
                    if n >= 8:
                        result[f"identity_player_hero:{p.account_id}:{p.hero_id}"] = (
                            sign * 0.5 * n / (n + 20)
                        )
                n = self.team_heroes[team, p.hero_id]
                if n >= 8 and retention >= 0.6:
                    result[f"identity_team_hero:{team}:{p.hero_id}"] = (
                        sign * 0.5 * n / (n + 20) * retention
                    )
            total = residual = wins = 0.0
            for when, won, expected, roster in self.events.get((draft.league_id, team), []):
                age = now - when
                if 0 < age <= 90 * 86400 and self.retention(roster, current) >= 0.6:
                    weight = 2 ** (-age / (14 * 86400))
                    total += weight
                    residual += weight * (won - expected)
                    wins += weight * (won - 0.5)
            event_residual += sign * residual / (total + 8)
            event_wins += sign * wins / (total + 8)
        result["evidence:roster_min"] = min(retentions)
        result["evidence:event_residual"] = 5 * event_residual
        result["evidence:event_winrate"] = 5 * event_wins
        total = residual = wins = 0.0
        current = {
            draft.radiant_id: self.roster(draft.radiant),
            draft.dire_id: self.roster(draft.dire),
        }
        key = tuple(sorted(current))
        for when, radiant, won, expected, rosters in self.head_to_head.get(key, []):
            age = now - when
            retention = min(self.retention(rosters[t], current[t]) for t in key)
            if 0 < age <= 180 * 86400 and retention >= 0.6:
                weight = 2 ** (-age / (60 * 86400)) * retention
                sign = 1 if radiant == draft.radiant_id else -1
                total += weight
                residual += weight * sign * (won - expected)
                wins += weight * sign * (won - 0.5)
        result["evidence:h2h_residual"], result["evidence:h2h_winrate"] = (
            5 * residual / (total + 8),
            5 * wins / (total + 8),
        )
        matrix = []
        for r in draft.radiant:
            line = []
            for d in draft.dire:
                a, b = sorted((r.hero_id, d.hero_id))
                line.append(
                    self.residual(self.counters.get((prefix, a, b))) * (1 if r.hero_id == a else -1)
                )
            matrix.append(line)
        rvalues = [sum(line) / 5 for line in matrix]
        dvalues = [-sum(line[i] for line in matrix) / 5 for i in range(5)]
        for name, r, d in (("counter", rvalues, dvalues), ("synergy", rsynergies, dsynergies)):
            result[f"evidence:{name}_strongest"] = 10 * (max(r) - max(d))
            result[f"evidence:{name}_weakest"] = 10 * (min(r) - min(d))
        return result

    def update(self, draft, won, expected, available):
        rid, did = draft.radiant_id, draft.dire_id
        own, other = self.teams.get(rid, available), self.teams.get(did, available)
        self.teams.set_result(rid, own, other, won, available)
        self.teams.set_result(did, other, own, not won, available)
        rs, ds = (
            self.players.squad(draft.radiant, available),
            self.players.squad(draft.dire, available),
        )
        own_players = {
            p.account_id: self.players.get(p.account_id, available)
            for p in draft.radiant + draft.dire
            if p.account_id
        }
        duplicate = self.roster(draft.radiant) & self.roster(draft.dire)
        for players, opposing, outcome in ((draft.radiant, ds, won), (draft.dire, rs, not won)):
            for p in players:
                if p.account_id and p.account_id not in duplicate:
                    self.players.set_result(
                        p.account_id, own_players[p.account_id], opposing, outcome, available
                    )
        rt, dt = (self.decayed_rating(("team", t), available) for t in (rid, did))
        pt, pd = (
            sum(
                self.decayed_rating(("player", p.account_id), available) if p.account_id else 1500
                for p in side
            )
            / 5
            for side in (draft.radiant, draft.dire)
        )
        texpected = 1 / (1 + 10 ** ((dt - rt) / 400))
        pexpected = 1 / (1 + 10 ** ((pd - pt) / 400))
        rosters = {rid: self.roster(draft.radiant), did: self.roster(draft.dire)}
        prefix = str(draft.patch)
        for team, players, outcome, chance, sign, rating in (
            (rid, draft.radiant, won, expected, 1, rt),
            (did, draft.dire, not won, 1 - expected, -1, dt),
        ):
            self.decaying["team", team] = rating + sign * 24 * (int(won) - texpected), available
            for p in players:
                if p.account_id and p.account_id not in duplicate:
                    old = self.decayed_rating(("player", p.account_id), available)
                    self.decaying["player", p.account_id] = (
                        old + sign * 20 * (int(won) - pexpected),
                        available,
                    )
                    self.player_heroes[p.account_id, p.hero_id] += 1
                self.team_heroes[team, p.hero_id] += 1
            self.rosters[team] = rosters[team]
            self.events[draft.league_id, team] = (
                self.events[draft.league_id, team]
                + [(available, int(outcome), chance, rosters[team])]
            )[-50:]
            heroes = sorted(p.hero_id for p in players)
            self.pairs.update(itertools.combinations(heroes, 2))
            self.triples.update(itertools.combinations(heroes, 3))
            for pair in itertools.combinations(heroes, 2):
                self.record(self.synergies, (prefix, *pair), outcome, chance)
        for r in draft.radiant:
            for d in draft.dire:
                a, b = sorted((r.hero_id, d.hero_id))
                lower = r.hero_id == a
                self.record(
                    self.counters,
                    (prefix, a, b),
                    won if lower else not won,
                    expected if lower else 1 - expected,
                )
        key = tuple(sorted((rid, did)))
        self.head_to_head[key] = (
            self.head_to_head[key] + [(available, rid, int(won), expected, rosters)]
        )[-20:]


def augment_evidence_features(rows, features, target_scope=None, progress=None):
    history, waiting, result = EvidenceHistory(), [], []
    for i, (row, original) in enumerate(zip(rows, features, strict=True)):
        now = int(row["start_time"])
        while waiting and waiting[0][0] < now:
            available, previous = heapq.heappop(waiting)
            old = rows[previous]
            expected = 1 / (1 + 10 ** (-features[previous]["team_elo"]))
            history.update(historical_draft(old), old["radiant_win"], expected, available)
        draft = historical_draft(row)
        values = history.features(draft)
        values["evidence:tier1"] = float(score_event(row, target_scope)) if target_scope else 0.0
        result.append({**original, **values})
        heapq.heappush(waiting, (now + int(row["duration"]) + 60, i))
        if progress and (i + 1) % 2000 == 0:
            progress({"evidence_features_built": i + 1, "total": len(rows)})
    return result
