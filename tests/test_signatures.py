from dataclasses import replace
from datetime import timedelta

import pytest

from betbot.domain import Player
from betbot.model import History
from betbot.signatures import SignatureHistory


def test_familiarity_needs_past_games_and_unknown_accounts_have_no_credit(draft):
    history = SignatureHistory()
    assert all(value == 0 for value in history.features(draft).values())
    past = replace(draft, observed_at=draft.observed_at - timedelta(days=1))
    for _ in range(30):
        history.update(past, True, 0.5)
    values = history.player_values(draft.radiant[0], draft)
    assert values["experience"] > 0.7 and values["preference"] > 0.4
    assert all(value == 0 for value in history.player_values(Player(0, 1), draft).values())
    switched = replace(draft.radiant[0], hero_id=9999)
    assert history.player_values(switched, draft)["experience"] == 0
    assert SignatureHistory(history.export()).features(draft) == history.features(draft)


def test_signature_adjusts_for_player_and_hero_baseline_and_opponents(draft):
    player = draft.radiant[0]
    key = f"comfort:{player.account_id}:{player.hero_id}"
    strong = SignatureHistory({"counts": {key: [100, 70, 70.0]}})
    surprise = SignatureHistory({"counts": {key: [100, 70, 40.0]}})
    assert strong.player_values(player, draft)["performance"] == 0
    assert surprise.player_values(player, draft)["performance"] > 0
    surprise.counts[f"player:{player.account_id}"] = [200, 150, 100.0]
    surprise.counts[f"hero:{player.hero_id}"] = [200, 150, 100.0]
    assert surprise.player_values(player, draft)["performance"] < 0
    one = SignatureHistory({"counts": {key: [1, 1, 0.5]}})
    assert one.player_values(player, draft)["performance"] < 0.025


def test_recency_and_patch_do_not_transfer_old_patch_bonus(draft):
    draft = replace(draft, patch=1)
    history = SignatureHistory()
    for _ in range(40):
        history.update(replace(draft, observed_at=draft.observed_at - timedelta(days=1)), True, 0.5)
    new_patch = replace(draft, patch=2)
    assert history.player_values(draft.radiant[0], new_patch)["patch"] == 0
    assert (
        history.features(replace(draft, observed_at=draft.observed_at + timedelta(days=365)))[
            "signature_recent"
        ]
        < history.features(draft)["signature_recent"]
    )
    later = replace(draft, observed_at=draft.observed_at + timedelta(seconds=30))
    reversed_sides = replace(later, radiant=draft.dire, dire=draft.radiant)
    assert history.features(reversed_sides) == pytest.approx(
        {k: -v for k, v in history.features(later).items()}
    )


def test_classic_model_features_stay_identical_after_signature_statistics(draft):
    history = History()
    past = replace(draft, observed_at=draft.observed_at - timedelta(days=1))
    history.update(past, True)
    before = history.features(draft)
    for _ in range(100):
        history.signatures.update(past, True, 0.5)
    assert history.features(draft) == before
    candidate = history.features(draft, feature_set="signatures")
    assert set(before).issubset(candidate)
    assert len(candidate) == len(before) + 5
    assert not any(k.startswith("signature_") for k in history.features(draft, combined=False))


def test_comfort_sample_is_specific_to_actual_account_and_hero(draft):
    history = History()
    past = replace(draft, observed_at=draft.observed_at - timedelta(days=1))
    history.update(past, True)
    history.update(past, False)
    sample = history.hero_comfort(draft)["radiant"][0]
    assert sample["count"] == 2 and sample["wins"] == 1
    assert sample["win_rate"] == sample["smoothed_win_rate"] == 0.5
    assert sample["pick_share"] == 1
    changed = replace(draft, radiant=(replace(draft.radiant[0], hero_id=9999), *draft.radiant[1:]))
    unseen = history.hero_comfort(changed)["radiant"][0]
    assert unseen["count"] == 0 and unseen["win_rate"] is None
    assert unseen["smoothed_win_rate"] == 0.5
