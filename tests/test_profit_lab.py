import json
from dataclasses import replace
from datetime import timedelta

import pytest

from betbot.domain import Quote, timestamp, utcnow
from betbot.profit_lab import PROTOCOL_HASH, ProfitLab, decisions, summarize
from betbot.storage import Store


@pytest.fixture
def priced(prediction):
    now = utcnow()
    quotes = [
        Quote(
            "Polymarket",
            prediction.draft.match_id,
            "map_winner",
            side,
            reference,
            now,
            None,
            prediction.draft.map_number,
            price_kind="market",
            gross=True,
            market_id="map-test",
            ask_odds=buy,
        )
        for side, reference, buy in (("radiant", 1.80, 1.75), ("dire", 2.25, 2.10))
    ]
    return replace(
        prediction,
        radiant_probability=0.64,
        draft_probability=0.61,
        quotes=quotes,
        timing_status="early",
        late_analysis=False,
    )


@pytest.fixture
def lab(tmp_path):
    store = Store(tmp_path / "lab.sqlite3")
    yield ProfitLab(store)
    store.close()


def add(lab, p):
    lab.store.observe(p.draft, "observed")
    identity = lab.store.add_forecast(p)
    delivery = lab.store.begin_delivery(identity, "forecast", -100)
    lab.store.finish_delivery(delivery, "sent", 123)
    return identity


def test_value_selection_uses_original_buy_price_and_cost_stress(priced):
    picks = decisions(priced, utcnow())
    assert picks["value"]["take"]
    assert picks["value"]["odds"] == 1.75
    assert picks["value"]["net_edge"] == pytest.approx(0.64 * 1.75 / 1.02 - 1)
    expensive = replace(
        priced,
        quotes=[replace(q, ask_odds=1.40) if q.outcome == "radiant" else q for q in priced.quotes],
    )
    assert not decisions(expensive, utcnow())["value"]["take"]


@pytest.mark.parametrize(
    "change",
    [
        {"ask_odds": None},
        {"ask_odds": float("nan")},
        {"source": "Fonbet"},
        {"map_number": 99},
        {"match_id": "other"},
        {"price_origin": "history"},
        {"observed_at": utcnow() - timedelta(seconds=40)},
        {"observed_at": utcnow() + timedelta(minutes=1)},
    ],
)
def test_missing_wrong_map_stale_or_future_pair_never_becomes_an_entry(priced, change):
    p = replace(priced, quotes=[replace(priced.quotes[0], **change), priced.quotes[1]])
    assert all(not pick["take"] for pick in decisions(p, utcnow()).values())


def test_market_control_can_pick_opponent_without_mixing_team_prices(priced):
    p = replace(
        priced,
        quotes=[
            replace(priced.quotes[0], odds=3.0, ask_odds=2.8),
            replace(priced.quotes[1], odds=1.5, ask_odds=1.4),
        ],
    )
    picks = decisions(p, utcnow())
    assert picks["model_all"]["side"] == "radiant" and picks["model_all"]["odds"] == 2.8
    assert picks["market_all"]["side"] == "dire" and picks["market_all"]["odds"] == 1.4
    assert not picks["market_agrees"]["take"]


def test_consensus_needs_timely_shadow_of_same_model_and_draft(priced):
    when = utcnow()
    shadow = {
        "compared_to": priced.model_version,
        "draft_observed_at": priced.draft.observed_at.isoformat(),
        "recorded_at": when.isoformat(),
        "radiant_probability": 0.62,
    }
    pick = decisions(priced, when, shadow)["consensus"]
    assert pick["take"] and pick["selection_probability"] == 0.62
    for patch in (
        {"compared_to": "other"},
        {"recorded_at": (when + timedelta(seconds=1)).isoformat()},
        {"draft_observed_at": (priced.draft.observed_at - timedelta(seconds=1)).isoformat()},
        {"radiant_probability": 0.4},
    ):
        assert not decisions(priced, when, {**shadow, **patch})["consensus"]["take"]


def test_forward_run_does_not_backfill_and_survives_restart(lab, priced, tmp_path):
    old = add(lab, priced)
    run = lab.start()
    assert run["first_forecast_id"] == old + 1
    lab.record(old)
    assert not lab.entries()[1]
    new = replace(
        priced,
        draft=replace(priced.draft, match_id="1234567891"),
        quotes=[replace(q, match_id="1234567891") for q in priced.quotes],
    )
    identity = add(lab, new)
    lab.record(identity)
    before = lab.entries()[1][0]
    assert before["decisions"]["model_all"]["take"] and before["radiant_win"] is None
    lab.store.result(new.draft.match_id, {"radiant_win": False})
    lab.record(identity)
    after = lab.entries()[1][0]
    assert before["decisions"] == after["decisions"]
    assert after["radiant_win"] is False
    restored = Store(tmp_path / "lab.sqlite3")
    try:
        assert ProfitLab(restored).start() == run
        assert len(ProfitLab(restored).entries()[1]) == 1
    finally:
        restored.close()


def test_known_result_and_old_attached_prices_cannot_create_forward_bets(lab, priced):
    lab.start()
    p = replace(priced, quotes=[])
    identity = add(lab, p)
    lab.store.set(f"quote_attached:{identity}:-100", [q.to_dict() for q in priced.quotes])
    lab.record(identity)
    assert all(not pick["take"] for pick in lab.entries()[1][0]["decisions"].values())
    later = replace(priced, draft=replace(priced.draft, match_id="1234567891"))
    second = add(lab, later)
    lab.store.result(later.draft.match_id, {"radiant_win": True})
    lab.record(second)
    assert len(lab.entries()[1]) == 1


def test_delayed_capture_records_a_skip_instead_of_backdating_entry(lab, priced, monkeypatch):
    lab.start()
    identity = add(lab, priced)
    when = timestamp(lab.store.forecast(identity)["created_at"])
    monkeypatch.setattr("betbot.profit_lab.utcnow", lambda: when + timedelta(minutes=2))
    lab.record(identity)
    entry = lab.entries()[1][0]
    assert all(pick["reason"] == "recording_late" for pick in entry["decisions"].values())


def test_contemporaneous_shadow_keeps_its_real_timestamp(lab, priced, monkeypatch):
    lab.start()
    identity = add(lab, priced)
    when = timestamp(lab.store.forecast(identity)["created_at"])
    shadow = {
        "compared_to": priced.model_version,
        "draft_observed_at": priced.draft.observed_at.isoformat(),
        "recorded_at": (when + timedelta(seconds=1)).isoformat(),
        "radiant_probability": 0.62,
    }
    lab.store.set(f"shadow_forecast:{priced.draft.match_id}", shadow)
    monkeypatch.setattr("betbot.profit_lab.utcnow", lambda: when + timedelta(seconds=2))
    lab.record(identity)
    entry = lab.entries()[1][0]
    assert entry["decisions"]["consensus"]["take"]
    assert entry["shadow_snapshot"]["recorded_at"] == shadow["recorded_at"]


def test_settlement_uses_unit_stake_and_reports_pending_separately(lab, priced):
    lab.start()
    identity = add(lab, priced)
    lab.record(identity)
    report = summarize(lab.entries()[1], "forward")
    assert report["policies"][0]["pending"] == 1
    assert report["policies"][0]["roi"] is None
    lab.store.result(priced.draft.match_id, {"radiant_win": True})
    report = summarize(lab.entries()[1], "forward")
    control = report["policies"][0]
    assert control["roi"] == pytest.approx(1.75 / 1.02 - 1)
    assert control["stress_5pct_roi"] == pytest.approx(1.75 / 1.05 - 1)
    assert not control["review_candidate"]
    assert not lab.store.db.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0]
    assert len(PROTOCOL_HASH) == 64
    saved = lab.store.get(f"profit_lab_decision:{PROTOCOL_HASH}:{identity}")
    assert "radiant_win" not in json.dumps(saved)
