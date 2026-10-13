import json

from betbot.storage import Store


def test_archived_publication_counts_without_replying_or_republishing(tmp_path, prediction):
    store = Store(tmp_path / "ledger.sqlite3")
    store.observe(prediction.draft, "observed")
    identity = store.add_forecast(prediction)
    delivery = store.begin_delivery(identity, "forecast", -100)
    store.finish_delivery(delivery, "sent", 42)
    with store.db:
        store.db.execute(
            "UPDATE deliveries SET state='archived',message_id=NULL WHERE id=?", (delivery,)
        )
        store.db.execute("UPDATE forecasts SET message_id=NULL WHERE id=?", (identity,))
    rows = store.published_between("2000", "2099", destination=-100)
    assert len(rows) == 1 and rows[0]["id"] == identity
    assert not store.published_between("2000", "2099", destination=-200)
    assert not store.publications()
    assert not store.pending_publications()
    assert store.begin_delivery(identity, "forecast", -100) is None
    assert store.forecast(identity)["state"] == "published"
    store.close()


def test_duplicate_prediction_and_result_are_immutable(tmp_path, prediction):
    store = Store(tmp_path / "ledger.sqlite3")
    store.observe(prediction.draft, "observed")
    forecast = store.add_forecast(prediction)
    assert store.add_forecast(prediction) is None
    store.result(prediction.draft.match_id, {"radiant_win": True})
    store.result(prediction.draft.match_id, {"radiant_win": False})
    assert json.loads(store.forecast(forecast)["result"])["radiant_win"] is True
    assert json.loads(store.forecast(forecast)["payload"])["radiant_probability"] == 0.58
    store.close()


def test_crash_during_send_becomes_uncertain_without_resend(tmp_path, prediction):
    path = tmp_path / "ledger.sqlite3"
    store = Store(path)
    store.observe(prediction.draft, "observed")
    forecast = store.add_forecast(prediction)
    assert store.begin_delivery(forecast, "forecast", -100, 17)
    store.close()
    store = Store(path)
    assert store.forecast(forecast)["state"] == "uncertain"
    assert store.begin_delivery(forecast, "forecast", -100) is None
    assert store.confirm_delivery(forecast, "forecast", 123)
    assert store.forecast(forecast)["message_id"] == 123
    assert store.forecast(forecast)["topic_id"] == 17
    store.close()


def test_reply_delivery_is_not_duplicated_and_skips_are_visible(tmp_path, prediction):
    store = Store(tmp_path / "ledger.sqlite3")
    store.observe(prediction.draft, "skipped", "no signal")
    forecast = store.add_forecast(prediction)
    delivery = store.begin_delivery(forecast, "forecast", -100)
    store.finish_delivery(delivery, "sent", 42)
    store.result(prediction.draft.match_id, {"radiant_win": False, "duration": 2400})
    delivery = store.begin_delivery(forecast, "result", -100)
    store.finish_delivery(delivery, "sent", 43)
    assert store.begin_delivery(forecast, "result", -100) is None
    assert store.forecast(forecast)["result_sent"] == 1
    summary = store.summary()
    assert summary["published"] == summary["settled"] == summary["losses"] == 1
    assert summary["wins"] == 0
    store.close()


def test_new_preview_preserves_old_snapshot_and_cannot_replace_published(tmp_path, prediction):
    store = Store(tmp_path / "ledger.sqlite3")
    store.observe(prediction.draft, "observed")
    old_id = store.add_forecast(prediction)
    assert store.supersede_preview(prediction.draft.match_id)
    prediction.radiant_probability = 0.61
    new_id = store.add_forecast(prediction)
    assert new_id != old_id
    assert json.loads(store.forecast(old_id)["payload"])["radiant_probability"] == 0.58
    delivery = store.begin_delivery(new_id, "forecast", -100)
    store.finish_delivery(delivery, "sent", 1)
    assert not store.supersede_preview(prediction.draft.match_id)
    store.close()


def test_restart_keeps_other_target_planned_without_retrying_ambiguous_send(tmp_path, prediction):
    path = tmp_path / "state.db"
    store = Store(path)
    store.observe(prediction.draft, "observed")
    identity = store.add_forecast(prediction)
    targets = [{"chat_id": -100, "topic_id": 9}, {"chat_id": -200, "topic_id": None}]
    store.plan_publication(identity, targets)
    store.begin_delivery(identity, "forecast", -100, 9)
    store.close()
    store = Store(path)
    remaining = store.plan_publication(identity, targets)
    assert len(remaining) == 1 and remaining[0]["destination"] == -200
    assert store.begin_delivery(identity, "forecast", -100, 9) is None
    delivery = store.begin_delivery(identity, "forecast", -200)
    store.finish_delivery(delivery, "sent", 201)
    assert store.forecast(identity)["state"] == "published"
    assert store.summary()["published"] == 1
    store.close()


def test_confirmation_requires_destination_if_multiple_sends_are_ambiguous(tmp_path, prediction):
    store = Store(tmp_path / "state.db")
    store.observe(prediction.draft, "observed")
    identity = store.add_forecast(prediction)
    for target in (-100, -200):
        delivery = store.begin_delivery(identity, "forecast", target)
        store.finish_delivery(delivery, "uncertain")
    assert not store.confirm_delivery(identity, "forecast", 42)
    assert store.confirm_delivery(identity, "forecast", 42, -200)
    assert len(store.uncertain()) == 1 and store.uncertain()[0]["destination"] == -100
    store.close()


def test_old_delivery_schema_preserves_original_topic_on_migration(tmp_path, prediction):
    path = tmp_path / "state.db"
    store = Store(path)
    store.observe(prediction.draft, "observed")
    identity = store.add_forecast(prediction)
    delivery = store.begin_delivery(identity, "forecast", -100, 17)
    store.finish_delivery(delivery, "sent", 42)
    with store.db:
        store.db.execute("ALTER TABLE deliveries DROP COLUMN topic_id")
    store.close()
    restored = Store(path)
    posts = restored.publications(identity)
    assert posts[0]["post_id"] == 42 and posts[0]["destination_topic_id"] == 17
    assert restored.begin_delivery(identity, "forecast", -100) is None
    restored.close()
