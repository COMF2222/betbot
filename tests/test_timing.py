import json
from dataclasses import replace
from datetime import timedelta

import pytest

from betbot.domain import Prediction
from betbot.presentation import caption, result_text
from betbot.reports import entries_from_rows
from betbot.timing import draft_timing, prediction_timing, separate_timing, timing_note


def test_unknown_clock_is_not_evidence_of_a_late_start(prediction):
    p = replace(prediction, draft=replace(prediction.draft, clock_known=False), late_analysis=True)
    assert prediction_timing(p) == "unverified"
    assert timing_note(p) == "Время начала карты не подтверждено."
    assert "с задержкой" not in caption(p)
    assert separate_timing(p)  # Unknown timing must not inflate early-forecast results.


def test_delayed_top_snapshot_cannot_claim_an_early_prediction(prediction):
    # The old production assumption recorded this as undelayed and early.
    p = replace(
        prediction,
        draft=replace(
            prediction.draft,
            source_name="Steam + OpenDota",
            game_time=-62,
            delay_seconds=900,
            clock_is_delayed=False,
        ),
        late_analysis=False,
    )
    payload = json.dumps(p.to_dict())
    loaded = Prediction.from_dict(json.loads(payload))
    assert prediction_timing(loaded) == "late"
    assert timing_note(loaded) == "Данные DotaTV · задержка источника 15 мин."
    assert timing_note(loaded) in caption(loaded)
    assert timing_note(loaded) in result_text(loaded, {"radiant_win": True})
    row = {"payload": payload, "result": json.dumps({"radiant_win": True})}
    assert not entries_from_rows([row])
    assert len(entries_from_rows([row], late_analysis=True)) == 1
    assert json.dumps(p.to_dict()) == payload


def test_verified_direct_clock_does_not_add_unrelated_broadcast_delay(prediction):
    d = replace(
        prediction.draft,
        source_name="Verified direct feed",
        game_time=-30,
        delay_seconds=900,
        clock_is_delayed=False,
    )
    assert draft_timing(d) == "early"
    assert not timing_note(replace(prediction, draft=d))
    assert (
        draft_timing(
            replace(d, source_updated_at=d.observed_at - timedelta(seconds=130)), now=d.observed_at
        )
        == "late"
    )


@pytest.mark.parametrize("status", ["early", "late", "unverified"])
def test_saved_stage_does_not_change_when_the_post_is_read_later(prediction, status):
    p = replace(prediction, timing_status=status)
    assert prediction_timing(Prediction.from_dict(p.to_dict())) == status
