"""Earlier verified Tier 1 ratings for offline domain-shift experiments."""

import heapq

from .backtest import score_event
from .model import History, historical_draft


def augment_target_features(rows, features, scope):
    history, waiting, result = History(), [], []
    for i, (row, values) in enumerate(zip(rows, features, strict=True)):
        start = int(row["start_time"])
        while waiting and waiting[0][0] < start:
            _, previous = heapq.heappop(waiting)
            old = rows[previous]
            history.update(historical_draft(old), old["radiant_win"], old)
        draft = historical_draft(row)
        domain = history.features(draft)
        result.append(
            {
                **{k: v for k, v in values.items() if not k.startswith("tier1:")},
                **{f"tier1:{k}": domain[k] for k in ("team_elo", "player_elo", "hero_experience")},
            }
        )
        if score_event(row, scope):
            heapq.heappush(waiting, (start + int(row["duration"]) + 60, i))
    return result
