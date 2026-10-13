import importlib.util
import sqlite3
import tarfile
from pathlib import Path


def test_private_backup_copies_committed_wal_and_rotates_after_success(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "backup_server", Path(__file__).resolve().parents[1] / "scripts/backup_server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root, destination = tmp_path / "project", tmp_path / "backups"
    (root / "data").mkdir(parents=True)
    (root / ".env").write_text("TELEGRAM_BOT_TOKEN=private-value\n")
    (root / "data/model.json").write_text("model-data")
    (root / "data/runtime-heartbeat.json").write_text("stale-heartbeat")
    live = sqlite3.connect(root / "data/betbot.sqlite3")
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE test(value TEXT)")
    live.execute("INSERT INTO test VALUES ('latest committed row')")
    live.commit()
    try:
        first = module.backup(root, destination, keep=1)
        second = module.backup(root, destination, keep=1)
        assert second.exists() and not first.exists()
        with tarfile.open(second) as archive:
            assert not any(
                "heartbeat" in name or name.endswith(("-wal", "-shm"))
                for name in archive.getnames()
            )
            assert archive.getmember(".env").mode == 0o600
            assert archive.extractfile(".env").read() == (root / ".env").read_bytes()
            restored = tmp_path / "restored.sqlite3"
            restored.write_bytes(archive.extractfile("data/betbot.sqlite3").read())
        connection = sqlite3.connect(restored)
        assert connection.execute("SELECT value FROM test").fetchone()[0] == "latest committed row"
        connection.close()
    finally:
        live.close()
