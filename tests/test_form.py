from dataclasses import replace
from datetime import timedelta

import pytest

from betbot.domain import Player
from betbot.form import FormHistory, PlayerSkills
from betbot.model import History


def test_form_uses_only_earlier_finished_maps_and_roundtrips(draft):
    history = History()
    empty = history.features(draft, feature_set="form")
    assert empty["recent_player_form"] == 0
    past = replace(draft, observed_at=draft.observed_at - timedelta(days=1))
    history.update(past, True)
    current = history.features(draft, feature_set="form")
    assert current["recent_player_form"] > 0
    assert current["recent_comfort_form"] > 0
    assert current["recent_lineup_form"] > 0
    assert History(history.export()).features(draft, feature_set="form") == current
    # Even a mistakenly supplied future history entry cannot enter the recent-form features.
    history.form.update(
        replace(draft, observed_at=draft.observed_at + timedelta(days=1)), False, 0.5
    )
    assert history.form.features(draft)["recent_player_form"] == current["recent_player_form"]


def test_recent_form_decays_and_cannot_follow_a_renamed_team_to_new_players(draft):
    form = FormHistory()
    past = replace(draft, observed_at=draft.observed_at - timedelta(days=1))
    form.update(past, True, 0.5)
    today = form.features(draft)
    later = form.features(replace(draft, observed_at=draft.observed_at + timedelta(days=90)))
    assert 0 < later["recent_player_form"] < today["recent_player_form"]
    changed = replace(
        draft, radiant=tuple(Player(90000 + i, p.hero_id) for i, p in enumerate(draft.radiant))
    )
    assert form.features(changed)["recent_lineup_form"] < today["recent_lineup_form"]
    renamed = replace(draft, radiant_name="Entirely different public name")
    assert form.features(renamed) == today


def test_recent_form_has_bounded_history_and_unknown_accounts_are_neutral(draft):
    form = FormHistory()
    for i in range(35):
        form.update(
            replace(draft, observed_at=draft.observed_at - timedelta(days=36 - i)), True, 0.5
        )
    assert all(len(rows) == 20 for rows in form.records.values())
    anonymous = replace(
        draft,
        radiant=tuple(replace(p, account_id=0) for p in draft.radiant),
        dire=tuple(replace(p, account_id=0) for p in draft.dire),
    )
    assert form.features(anonymous)["recent_player_form"] == 0
    assert form.features(anonymous)["recent_lineup_form"] == 0


@pytest.mark.parametrize("feature_set", ["form", "matchups", "adaptive", "players", "players_form"])
def test_new_feature_sets_preserve_classic_and_require_no_current_statistics(draft, feature_set):
    history = History()
    classic = history.features(draft)
    expanded = history.features(replace(draft, patch=60), feature_set=feature_set)
    assert all(expanded[key] == value for key, value in classic.items())
    assert not any("current" in key or "score" in key for key in expanded)
    if feature_set in ("matchups", "adaptive"):
        assert "counter_advantage" in expanded
        assert f"patch_hero:60:{draft.radiant[0].hero_id}" in expanded


def test_player_skill_ratings_follow_accounts_across_team_changes(draft):
    skills = PlayerSkills()
    before = skills.features(draft, History().analytics)
    assert before["lineup_player_elo"] == 0
    skills.update(draft, True)
    first = skills.features(draft, History().analytics)
    assert first["lineup_player_elo"] > 0
    renamed = replace(draft, radiant_id=123456, dire_id=654321)
    assert skills.features(renamed, History().analytics) == first
    replaced = replace(
        draft, radiant=tuple(Player(90000 + i, p.hero_id) for i, p in enumerate(draft.radiant))
    )
    assert (
        skills.features(replaced, History().analytics)["lineup_player_elo"]
        < first["lineup_player_elo"]
    )
    restored = PlayerSkills(skills.ratings)
    assert restored.features(draft, History().analytics) == first
    assert first["mid_player_elo"] == 0  # Current roles cannot be invented.
