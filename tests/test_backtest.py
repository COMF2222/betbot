from datetime import UTC, datetime

from betbot.backtest import evaluate, months_before
from betbot.domain import Prediction, utcnow
from betbot.model import History
from betbot.presentation import MOSCOW


class BlindModel:
    def __init__(self):
        self.raw = {
            "history": History().export(),
            "models": {"frozen": 1},
            "version": "test",
            "history_cutoff": "2020-01-01T00:00:00Z",
        }
        self.inputs = []

    def predict(self, draft, history=None, history_as_of=""):
        assert not hasattr(draft, "radiant_win")
        assert not hasattr(draft, "duration")
        self.inputs.append(draft.to_dict())
        features = (history or History(self.raw["history"])).features(draft)
        p = 1 / (1 + 10 ** (-features["team_elo"]))
        return Prediction(draft, p, p, "test", [], "test")


def rows():
    start = int(utcnow().timestamp()) - 10 * 3600
    return [
        {
            "match_id": i + 1,
            "start_time": start + offset,
            "radiant_team_id": 1,
            "dire_team_id": 2,
            "radiant_win": True,
            "duration": 7200,
            "leagueid": 123,
            "series_id": i + 1,
            "heroes": [1, 13, 29, 5, 30, 54, 11, 2, 20, 26],
            "accounts": list(range(1, 11)),
            "slots": [0, 1, 2, 3, 4, 128, 129, 130, 131, 132],
        }
        for i, offset in enumerate((0, 60, 12000))
    ]


def test_current_results_are_separate_and_static_predictions_do_not_change(tmp_path):
    original = rows()
    flipped = [{**r, "radiant_win": False} for r in original]
    first, second = BlindModel(), BlindModel()
    report_a = evaluate(first, original, tmp_path / "a", online_history=False)
    report_b = evaluate(second, flipped, tmp_path / "b", online_history=False)
    assert first.inputs == second.inputs
    assert report_a["all_maps"]["wins"] == 3
    assert report_b["all_maps"]["wins"] == 0
    assert (tmp_path / "a" / "predictions.jsonl").read_text() == (
        tmp_path / "b" / "predictions.jsonl"
    ).read_text()
    assert "radiant_win" not in (tmp_path / "a" / "blind-inputs.jsonl").read_text()
    assert "radiant_win" not in (tmp_path / "a" / "predictions.jsonl").read_text()


def test_parallel_games_do_not_see_outcome_until_it_finishes(tmp_path):
    import json

    model = BlindModel()
    evaluate(model, rows(), tmp_path, online_history=True)
    predictions = [
        json.loads(line) for line in (tmp_path / "predictions.jsonl").read_text().splitlines()
    ]
    assert predictions[0]["radiant_probability"] == predictions[1]["radiant_probability"] == 0.5
    assert predictions[2]["radiant_probability"] > 0.5


def test_four_calendar_months_handle_month_end():
    assert months_before(datetime(2026, 10, 31), 4) == datetime(2026, 6, 30)


def test_tier_filter_uses_event_dates_and_does_not_score_qualifiers(tmp_path):
    from betbot.backtest import score_event

    source = rows()
    # UTC and Moscow have different dates here, regardless of the test host's zone.
    source[0]["start_time"] = int(datetime(2026, 10, 13, 22, 30, tzinfo=UTC).timestamp())
    day = datetime.fromtimestamp(source[0]["start_time"], MOSCOW).date().isoformat()
    assert day == "2026-10-14"
    scope = {"events": [{"league_id": 123, "start_date": day, "end_date": day}]}
    assert score_event(source[0], scope)
    assert not score_event(
        source[0],
        {"events": [{"league_id": 123, "start_date": "2026-10-13", "end_date": "2026-10-13"}]},
    )
    assert not score_event({**source[0], "leagueid": 999}, scope)
    assert not score_event({**source[0], "start_time": source[0]["start_time"] + 86400 * 10}, scope)
