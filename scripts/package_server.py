"""Build a private server archive with a consistent SQLite backup, no VPN credentials."""

import argparse
import contextlib
import hashlib
import json
import re
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from betbot.config import Settings
from betbot.domain import utcnow

ROOT = Path(__file__).resolve().parents[1]


def normalized_env(text):
    values = {
        "DATA_DIR": "data",
        "MODEL_PATH": "data/model.json",
        "PROXY_URL": "",
        "TELEGRAM_PROXY_URL": "",
    }
    for key, value in values.items():
        line = f"{key}={value}"
        if re.search(r"(?m)^" + key + "=", text):
            text = re.sub(r"(?m)^" + key + r"=.*$", line, text)
        else:
            text += "\n" + line
    return text


def package(output, include_env=False):
    settings = Settings()
    if not settings.model_path.exists():
        raise ValueError("Сначала нужна обученная модель.")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="server-package-", dir=output.parent) as directory:
        stage = Path(directory) / "betbot-server"
        stage.mkdir()
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
            "src",
            "docs",
            "config",
            "scripts",
        ):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(
                    source, stage / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
                )
            else:
                shutil.copy2(source, stage / name)
        if include_env:
            (stage / ".env").write_text(
                normalized_env((ROOT / ".env").read_text(encoding="utf-8")),
                encoding="utf-8",
                newline="\n",
            )
        data = stage / "data"
        data.mkdir()
        shutil.copy2(settings.model_path, data / "model.json")
        for name in ("model-report.json", "heroes.json", "heroes", "reports"):
            source = settings.data_dir / name
            if source.is_dir():
                shutil.copytree(source, data / name)
            elif source.exists():
                shutil.copy2(source, data / name)
        original = settings.data_dir / "betbot.sqlite3"
        if original.exists():
            with (
                contextlib.closing(
                    sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True)
                ) as src,
                contextlib.closing(sqlite3.connect(data / "betbot.sqlite3")) as dst,
            ):
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Проверка копии базы не пройдена.")
        (stage / "SERVER-START.md").write_text(
            (ROOT / "docs/deploy-mihomo.md").read_text(encoding="utf-8"), encoding="utf-8"
        )
        model = json.loads((data / "model.json").read_text(encoding="utf-8"))
        manifest = {
            "created_at": utcnow().isoformat(),
            "model_version": model["version"],
            "contains_telegram_credentials": include_env,
            "contains_vpn_credentials": False,
            "database": "SQLite backup API",
            "files": {
                p.relative_to(stage).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in stage.rglob("*")
                if p.is_file()
            },
        }
        (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        def permissions(info):
            info.uid = info.gid = 10001 if "/data" in info.name else 0
            info.uname = info.gname = ""
            private = "/data" in info.name or info.name.endswith("/.env")
            info.mode = (
                (0o700 if private else 0o755)
                if info.isdir() or info.name.endswith("deploy.sh")
                else (0o600 if private else 0o644)
            )
            return info

        with tarfile.open(output, "w:gz") as archive:
            archive.add(stage, arcname="betbot-server", filter=permissions)
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{checksum}  {output.name}\n", encoding="utf-8"
    )
    return {
        "archive": str(output),
        "megabytes": round(output.stat().st_size / 1024**2, 1),
        "private_credentials": include_env,
        "sha256": checksum,
    }


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--output", type=Path, default=ROOT / "artifacts/betbot-server.tar.gz")
    args.add_argument(
        "--include-env",
        action="store_true",
        help="Include private Telegram settings; never share the archive publicly",
    )
    parsed = args.parse_args()
    print(json.dumps(package(parsed.output, parsed.include_env), ensure_ascii=False))
