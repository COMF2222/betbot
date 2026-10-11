"""Causal within-series context for offline research; no live-model integration."""

import heapq
import math
from collections import defaultdict

from .domain import Draft
from .model import historical_draft

EVEN_SERIES_FEATURES = frozenset(
    ("series:has_previous", "series:previous_maps", "series:since_last")
)
BASIC_SERIES_FEATURES = frozenset(
    ("series:has_previous", "series:score_difference", "series:last_result")
)
SERIES_FEATURES = (
    *sorted(EVEN_SERIES_FEATURES),
    "series:score_difference",
    "series:last_result",
    "series:surprise",
    "series:roster_retention",
    "series:hero_repeat",
)


class SeriesContext:
    """Use exact league/series/team identities and only already available results."""

    def __init__(self):
        self.records = defaultdict(dict)

    @staticmethod
    def key(draft: Draft):
        if not draft.series_id or draft.series_id == "0" or not draft.complete:
            return None
        return (draft.league_id, draft.series_id, *sorted((draft.radiant_id, draft.dire_id)))

    def update(self, draft, won, available_at, expected):
        key = self.key(draft)
        if key:
            self.records[key][draft.match_id] = {
                "start": draft.observed_at.timestamp(),
                "available": available_at,
                "radiant_id": draft.radiant_id,
                "winner": draft.radiant_id if won else draft.dire_id,
                "expected": expected,
                "rosters": {
                    team: {p.account_id: p.hero_id for p in players if p.account_id}
                    for team, players in (
                        (draft.radiant_id, draft.radiant),
                        (draft.dire_id, draft.dire),
                    )
                },
            }

    def features(self, draft):
        result = dict.fromkeys(SERIES_FEATURES, 0.0)
        key, now = self.key(draft), draft.observed_at.timestamp()
        if key is None:
            return result
        past = sorted(
            (
                r
                for match_id, r in self.records.get(key, {}).items()
                if match_id != draft.match_id
                and r["available"] < now
                and 0 < now - r["start"] <= 48 * 3600
            ),
            key=lambda r: r["start"],
        )
        # More than four prior maps cannot be reliably interpreted as a new BO5 map.
        # Neither future series length nor a guessed ID is used to repair bad identities.
        if not past or len(past) > 4:
            return result
        signs = [1 if r["winner"] == draft.radiant_id else -1 for r in past]
        result.update(
            {
                "series:has_previous": 1.0,
                "series:previous_maps": len(past) / 4,
                "series:score_difference": sum(signs) / (len(past) + 2),
                "series:last_result": float(signs[-1]),
                "series:since_last": min((now - past[-1]["available"]) / 86400, 1.0),
                "series:surprise": sum(
                    int(r["winner"] == draft.radiant_id)
                    - (r["expected"] if r["radiant_id"] == draft.radiant_id else 1 - r["expected"])
                    for r in past
                )
                / (len(past) + 2),
            }
        )
        for team, players, sign in (
            (draft.radiant_id, draft.radiant, 1),
            (draft.dire_id, draft.dire, -1),
        ):
            current = {p.account_id: p.hero_id for p in players if p.account_id}
            previous = past[-1]["rosters"][team]
            if len(current) == len(previous) == 5:
                result["series:roster_retention"] += (
                    sign * len(current.keys() & previous.keys()) / 5
                )
                result["series:hero_repeat"] += (
                    sign
                    * sum(previous.get(account) == hero for account, hero in current.items())
                    / 5
                )
        return result


def augment_series_features(rows, features):
    """Add context to a causal feature cache without changing its baseline features."""
    history, waiting, result = SeriesContext(), [], []
    for i, (row, values) in enumerate(zip(rows, features, strict=True)):
        start = int(row["start_time"])
        while waiting and waiting[0][0] < start:
            available, previous = heapq.heappop(waiting)
            old = rows[previous]
            expected = 1 / (1 + 10 ** (-features[previous]["team_elo"]))
            history.update(historical_draft(old), old["radiant_win"], available, expected)
        draft = historical_draft(row)
        result.append(
            {
                **{k: v for k, v in values.items() if not k.startswith("series:")},
                **history.features(draft),
            }
        )
        heapq.heappush(waiting, (start + int(row["duration"]) + 60, i))
    if any(not math.isfinite(v) for row in result for v in row.values()):
        raise ValueError("Non-finite research features")
    return result
