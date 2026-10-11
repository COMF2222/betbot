import copy
from dataclasses import replace

import pytest
from test_model import history_rows

from betbot.experiments import build_examples, feature_view, swap_features
from betbot.model import historical_draft
from betbot.series_context import SeriesContext, augment_series_features


def test_context_uses_only_previous_completed_map_and_preserves_baseline():
    rows = history_rows(10)
    features = build_examples(rows)
    context = augment_series_features(rows, features)
    assert context[0]["series:has_previous"] == 0
    assert context[1]["series:has_previous"] == 1
    assert context[1]["series:last_result"] == -1
    assert context[2]["series:has_previous"] == 0  # A new exact series ID.
    for original, extended in zip(features, context, strict=True):
        assert feature_view(original, "classic") == feature_view(extended, "classic")
        assert feature_view(original, "compact") == feature_view(extended, "compact")
    altered = copy.deepcopy(rows)
    for row in altered[1:]:
        row["radiant_win"] = not row["radiant_win"]
        row["duration"] = 9999
        row["gold10"] = [999999] * 10
    new = augment_series_features(altered, features)
    assert new[:2] == context[:2]


def test_exact_completion_buffer_is_not_available_until_after_boundary():
    rows = history_rows(3)
    start = rows[0]["start_time"]
    rows[0]["duration"] = 600
    rows[1]["start_time"], rows[2]["start_time"] = start + 660, start + 661
    rows[2]["series_id"] = rows[0]["series_id"]
    features = augment_series_features(rows, build_examples(rows))
    assert features[1]["series:has_previous"] == 0
    assert features[2]["series:has_previous"] == 1


def test_series_identity_requires_league_series_teams_and_recent_map(draft):
    history = SeriesContext()
    now = draft.observed_at.timestamp()
    history.update(draft, True, now + 1860, 0.7)
    later = replace(draft, match_id="other", observed_at=draft.observed_at.replace())
    from datetime import timedelta

    later = replace(later, observed_at=later.observed_at + timedelta(hours=2))
    assert history.features(later)["series:has_previous"] == 1
    for changed in (
        replace(later, league_id=draft.league_id + 1),
        replace(later, series_id="another"),
        replace(later, series_id=""),
        replace(later, series_id="0"),
        replace(later, dire_id=draft.dire_id + 999),
        replace(later, observed_at=draft.observed_at + timedelta(hours=49)),
        replace(later, match_id=draft.match_id),
    ):
        assert history.features(changed)["series:has_previous"] == 0
    assert len(history.records) == 1  # Missing identities never create guessed records.


def test_side_swap_reverses_score_and_surprise_but_preserves_presence(draft):
    from datetime import timedelta

    history = SeriesContext()
    history.update(draft, True, draft.observed_at.timestamp() + 1860, 0.7)
    later = replace(draft, match_id="other", observed_at=draft.observed_at + timedelta(hours=2))
    original = history.features(later)
    other = history.features(
        replace(
            later,
            radiant=draft.dire,
            dire=draft.radiant,
            radiant_id=draft.dire_id,
            dire_id=draft.radiant_id,
        )
    )
    assert original["series:surprise"] == pytest.approx(0.1)
    assert other == pytest.approx(swap_features(original))
    assert original["series:hero_repeat"] == 0


def test_duplicate_or_malformed_series_does_not_create_a_phantom_bo7():
    rows = history_rows(6)
    history = SeriesContext()
    for row in rows[:5]:
        row["series_id"] = 99
        draft = historical_draft(row)
        history.update(draft, row["radiant_win"], row["start_time"] + 1860, 0.5)
        history.update(draft, row["radiant_win"], row["start_time"] + 1860, 0.5)
    rows[5]["series_id"] = 99
    assert len(next(iter(history.records.values()))) == 5
    assert history.features(historical_draft(rows[5]))["series:has_previous"] == 0
