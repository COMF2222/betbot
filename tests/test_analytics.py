import copy
from dataclasses import replace

from betbot.analytics import DraftAnalytics, historical_roles
from betbot.domain import Player
from betbot.model import History, historical_draft
from betbot.providers import match_context, patch_for_time


def context(draft):
    return {
        "accounts": [p.account_id for p in draft.radiant + draft.dire],
        "slots": [0, 1, 2, 3, 4, 128, 129, 130, 131, 132],
        "lane_roles": [1, 2, 3, 3, 1] * 2,
        "gpms": [700, 600, 500, 250, 200] * 2,
        "gold10": [4500, 4000, 3500, 2000, 1800, 4400, 3800, 3300, 1900, 1700],
        "xp10": [6000] * 10,
    }


def test_roles_and_mid_stats_use_past_maps_only(draft):
    analytics = DraftAnalytics()
    past = context(draft)
    assert historical_roles(past)[draft.radiant[1].account_id] == 2
    assert analytics.describe(draft, "radiant")["role_coverage"] == 0
    for _ in range(25):
        analytics.update(draft, True, 0.5, past)
    details = analytics.describe(draft, "radiant")
    assert details["role_coverage"] == 10
    assert len(details["lanes"]) == 2
    assert details["mid"]["gold10_count"] == 25
    assert details["mid"]["gold10_mean_radiant"] == 200
    before = analytics.features(draft)
    # Measurements from a new, unfinished game have no access to the predictor.
    new_context = {**past, "gpms": list(reversed(past["gpms"]))}
    historical_roles(new_context)
    assert analytics.features(draft) == before
    assert analytics.describe(replace(draft, patch=999), "radiant")["mid"] is None


def test_roles_are_unknown_for_new_players_and_ambiguous_positions(draft):
    provided = replace(draft, radiant=tuple(replace(p, position=2) for p in draft.radiant))
    analytics = DraftAnalytics()
    assert not any(r["position"] for r in analytics.assignments(provided)["radiant"])
    assert analytics.role(Player(99999, 1))["position"] is None


def test_history_state_does_not_mutate_model_artifact(draft):
    history = History()
    history.update(draft, True, context(draft))
    raw = history.export()
    frozen = copy.deepcopy(raw)
    History(raw).update(draft, False, context(draft))
    assert raw == frozen


def test_current_replay_fields_never_enter_historical_draft(draft):
    from test_model import history_rows

    row = history_rows()[0]
    row.update(context(draft))
    first = historical_draft(row)
    row.update(
        {
            "radiant_win": not row["radiant_win"],
            "duration": 11000,
            "gold10": [999999] * 10,
            "lane_roles": [2] * 10,
            "gpms": [9999] * 10,
        }
    )
    assert historical_draft(row) == first
    assert all(p.position is None for p in first.radiant + first.dire)


def test_checkpoints_are_aligned_to_time_and_patch_is_not_from_future():
    raw = {
        "players": [
            {
                "account_id": 1,
                "player_slot": 0,
                "times": [0, 600, 660],
                "gold_t": [0, 4000, 4500],
                "xp_t": [0, 5000],
            }
        ]
    }
    assert match_context(raw)["gold10"] == [4000]
    assert match_context({"players": [{"times": [0, 660], "gold_t": [0, 4500]}]})["gold10"] == [
        None
    ]
    patches = [
        {"id": 60, "date": "2026-03-24T00:00:00Z"},
        {"id": 61, "date": "2030-01-01T00:00:00Z"},
    ]
    assert patch_for_time(patches, "2026-10-10T00:00:00Z") == 60
