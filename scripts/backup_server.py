"""Private, consistent server backups; usable with only the Python standard library."""

import argparse
import contextlib
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def backup(root, destination, keep=7):
    root, destination = root.resolve(), destination.resolve()
    if keep < 1 or destination == root or root in destination.parents:
        raise ValueError("Use an external backup directory and keep at least one copy")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    name = "betbot-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".tar.gz"
    final = destination / name
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=destination) as directory:
        stage = Path(directory)
        database = stage / "betbot.sqlite3"
        with (
            contextlib.closing(
                sqlite3.connect((root / "data/betbot.sqlite3").as_uri() + "?mode=ro", uri=True)
            ) as source,
            contextlib.closing(sqlite3.connect(database)) as target,
        ):
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup database failed integrity check")

        def permissions(member):
            member.uid = member.gid = 10001 if member.name.startswith("data/") else 0
            member.uname = member.gname = ""
            member.mode = 0o700 if member.isdir() else 0o600
            return member

        temporary = stage / name
        with tarfile.open(temporary, "w:gz") as archive:
            for filename in (".env", "deploy.env"):
                if (root / filename).is_file():
                    archive.add(root / filename, arcname=filename, filter=permissions)
            for file in sorted((root / "data").rglob("*")):
                if (
                    not file.is_file()
                    or file.is_symlink()
                    or file.name
                    in {
                        "betbot.sqlite3",
                        "betbot.sqlite3-wal",
                        "betbot.sqlite3-shm",
                        "runtime-heartbeat.json",
                        "runtime-heartbeat.tmp",
                    }
                ):
                    continue
                archive.add(file, arcname=file.relative_to(root).as_posix(), filter=permissions)
            archive.add(database, arcname="data/betbot.sqlite3", filter=permissions)
            revision = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
            )
            manifest = stage / "BACKUP.json"
            manifest.write_text(
                json.dumps(
                    {
                        "created_at": datetime.now(UTC).isoformat(),
                        "source_revision": revision.stdout.strip()
                        if revision.returncode == 0
                        else None,
                        "contains_credentials": True,
                        "database": "SQLite backup API",
                    }
                ),
                encoding="utf-8",
            )
            archive.add(manifest, arcname="BACKUP.json", filter=permissions)
        os.chmod(temporary, 0o600)
        temporary.replace(final)
    # Rotate only regular archives in this dedicated directory, after success.
    previous = sorted(
        file
        for file in destination.glob("betbot-*.tar.gz")
        if file.is_file() and not file.is_symlink()
    )
    for file in previous[:-keep]:
        file.unlink()
    return final


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--destination", type=Path, default=Path("/root/betbot-backups"))
    parser.add_argument("--keep", type=int, default=7)
    args = parser.parse_args()
    print(backup(args.root, args.destination, args.keep))
