import importlib.util
import json
import sqlite3
import tarfile
from pathlib import Path

from betbot.config import Settings


def test_export_keeps_live_wal_data_and_excludes_vpn_and_runtime_secrets(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/package_server.py"
    spec = importlib.util.spec_from_file_location("package_server_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "project"
    root.mkdir()
    for name in (
        "Dockerfile",
        "compose.yaml",
        "compose.mihomo.yaml",
        ".dockerignore",
        ".env.example",
        "deploy.env.example",
        "deploy.sh",
        "pyproject.toml",
        "requirements.txt",
        "uv.lock",
        "README.md",
    ):
        (root / name).write_text("source")
    for name in ("src", "docs", "config", "scripts", "data", ".secrets"):
        (root / name).mkdir()
    (root / "docs/deploy-mihomo.md").write_text("Server instructions")
    (root / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=private-bot-value\nSTEAM_API_KEY=private-steam-value\nPROXY_URL=http://127.0.0.1:2080\n"
    )
    (root / ".secrets/vpn-subscription-url.txt").write_text("private-subscription")
    (root / "data/model.json").write_text(json.dumps({"version": "test-model"}))
    (root / "data/runtime-heartbeat.json").write_text("old-heartbeat")
    settings = Settings(_env_file=None, data_dir=root / "data", model_path=root / "data/model.json")
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "Settings", lambda: settings)
    live = sqlite3.connect(root / "data/betbot.sqlite3")
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE saved(value TEXT)")
    live.execute("INSERT INTO saved VALUES ('latest committed data')")
    live.commit()
    output = tmp_path / "archive.tar.gz"
    module.package(output, True)
    live.close()
    with tarfile.open(output) as archive:
        names = archive.getnames()
        assert not any(
            ".secrets" in n or "heartbeat" in n or "-wal" in n or "-shm" in n for n in names
        )
        env = archive.extractfile("betbot-server/.env").read().decode()
        assert "private-steam-value" in env and "2080" not in env and "DATA_DIR=data" in env
        assert archive.getmember("betbot-server/.env").mode == 0o600
        database = tmp_path / "copied.sqlite3"
        database.write_bytes(archive.extractfile("betbot-server/data/betbot.sqlite3").read())
        conn = sqlite3.connect(database)
        assert conn.execute("SELECT value FROM saved").fetchone()[0] == "latest committed data"
        conn.close()
    assert output.with_suffix(".gz.sha256").exists()
