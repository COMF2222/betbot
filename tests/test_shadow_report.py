import importlib.util
import json
from datetime import timedelta
from pathlib import Path

from betbot.domain import utcnow
from betbot.storage import Store

spec = importlib.util.spec_from_file_location(
    "shadow_report", Path(__file__).parents[1] / "scripts/shadow_report.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def save_case(store, prediction, recorded, confirmed, finish):
    draft = prediction.draft
    store.observe(draft, "observed")
    store.add_forecast(prediction)
    store.set(
        f"shadow_forecast:{draft.match_id}",
        {
            "compared_to": prediction.model_version,
            "model_version": "candidate",
            "draft_observed_at": draft.observed_at.isoformat(),
            "recorded_at": recorded.isoformat(),
            "history_as_of": (draft.observed_at - timedelta(hours=1)).isoformat(),
            "radiant_probability": 0.4,
            "draft_probability": 0.6,
            "threshold": 0.55,
        },
    )
    store.result(
        draft.match_id,
        {
            "radiant_win": True,
            "start_time": int((finish - timedelta(minutes=30)).timestamp()),
            "duration": 1800,
            "confirmed_at": confirmed.isoformat(),
        },
    )


def test_shadow_report_compares_same_maps_without_writing_database(tmp_path, prediction):
    path = tmp_path / "ledger.sqlite3"
    store = Store(path)
    now = utcnow()
    save_case(store, prediction, now, now + timedelta(hours=2), now + timedelta(hours=1))
    before = list(store.db.execute("SELECT key,value FROM settings"))
    report = module.compare(path)
    case = report["comparisons"][0]
    assert case["active"]["wins"] == 1 and case["shadow"]["wins"] == 0
    assert case["active"]["maps"] == case["shadow"]["maps"] == 1
    assert case["disagreements"] == 1 and case["pending"] == 0
    assert not case["automatic_promotion"]
    assert list(store.db.execute("SELECT key,value FROM settings")) == before
    store.close()


def test_shadow_report_rejects_predictions_recorded_after_actual_end(tmp_path, prediction):
    path = tmp_path / "ledger.sqlite3"
    store = Store(path)
    now = utcnow()
    save_case(store, prediction, now, now + timedelta(hours=2), now - timedelta(seconds=1))
    report = module.compare(path)
    assert report["comparisons"] == []
    assert report["exclusions"]["outcome_already_available_or_unverified_time"] == 1
    assert json.loads(store.forecasts()[0]["payload"])["radiant_probability"] == 0.58
    store.close()
