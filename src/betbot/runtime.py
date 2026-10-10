"""Container startup, proxy readiness and a local worker health check."""

import asyncio
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

import httpx

from .config import Settings
from .domain import timestamp, utcnow
from .network import route_label


def heartbeat(settings, ok=True):
    path = settings.data_dir / "runtime-heartbeat.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"pid": os.getpid(), "at": utcnow().isoformat(), "ok": ok}), encoding="utf-8"
    )
    temporary.replace(path)


def healthy(settings):
    try:
        state = json.loads(
            (settings.data_dir / "runtime-heartbeat.json").read_text(encoding="utf-8")
        )
        moment = timestamp(state["at"])
        age = (utcnow() - moment).total_seconds()
        # The pid must still exist; a copied snapshot cannot make a new container healthy.
        pid = int(state["pid"])
        if pid <= 0 or not (
            Path(f"/proc/{pid}").exists()
            if sys.platform.startswith("linux")
            else pid == os.getpid()
        ):
            return False
        return bool(state["ok"] and 0 <= age <= max(180, settings.poll_seconds * 3 + 60))
    except (OSError, ValueError, KeyError, TypeError):
        return False


async def wait_proxy(proxy, seconds):
    deadline = time.monotonic() + seconds
    async with httpx.AsyncClient(
        proxy=proxy, trust_env=False, timeout=8, follow_redirects=False
    ) as client:
        while True:
            try:
                response = await client.get("https://api.telegram.org")
                if response.status_code < 500 and response.status_code != 407:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("VPN-прокси не готов; проверьте Mihomo и его mixed-port.")
            await asyncio.sleep(min(3, max(0, deadline - time.monotonic())))


def main():
    settings = Settings()
    command = sys.argv[1:] or ["betbot", "run"]
    if command == ["health"]:
        raise SystemExit(0 if healthy(settings) else 1)
    if os.getenv("PROXY_REQUIRED", "").lower() in ("1", "true", "yes"):
        if not settings.proxy_url:
            print("PROXY_REQUIRED: задайте PROXY_URL; запуск остановлен.", file=sys.stderr)
            raise SystemExit(1)
        print(f"Ожидание VPN-прокси: {route_label(settings.proxy_url)}", flush=True)
        try:
            asyncio.run(wait_proxy(settings.proxy_url, int(os.getenv("PROXY_WAIT_SECONDS", "180"))))
        except (RuntimeError, ValueError) as exc:
            # Exception values may contain credentials; expose only a fixed diagnostic.
            print(
                f"VPN-прокси недоступен ({type(exc).__name__}). Проверьте deploy.env и Mihomo.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
    if command == ["preflight"]:
        from .cli import doctor

        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(doctor(settings, False))
        audit = json.loads((settings.data_dir / "source-audit.json").read_text(encoding="utf-8"))
        checks = {
            name: bool(audit["sources"].get(name, {}).get("ok"))
            for name in ("Telegram", "OpenDota", "Polymarket")
        }
        checks["settings"] = bool(settings.telegram_bot_token and settings.admins)
        checks["model"] = bool(audit["model_exists"])
        print(json.dumps(checks, ensure_ascii=False), flush=True)
        raise SystemExit(0 if all(checks.values()) else 1)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
