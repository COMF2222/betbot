from dataclasses import replace

import pytest

from betbot.demo import demo_prediction
from betbot.domain import utcnow


@pytest.fixture
def draft():
    now = utcnow()
    return replace(
        demo_prediction().draft,
        match_id="1234567890",
        league_id=123,
        series_id="456",
        observed_at=now,
        source_updated_at=now,
        game_time=-100,
        delay_seconds=0,
    )


@pytest.fixture
def prediction(draft):
    return replace(
        demo_prediction(),
        draft=draft,
        model_version="test-model",
        quotes=[],
        publication_ready=True,
    )
