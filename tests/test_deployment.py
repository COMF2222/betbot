import json
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from betbot.config import Settings
from betbot.deployment import mihomo_summary
from betbot.domain import utcnow
from betbot.runtime import healthy, heartbeat, wait_proxy


def test_worker_health_rejects_missing_stale_and_failed_heartbeat(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert not healthy(settings)
    heartbeat(settings)
    assert healthy(settings)
    heartbeat(settings, False)
    assert not healthy(settings)
    path = tmp_path / "runtime-heartbeat.json"
    path.write_text(
        json.dumps(
            {"pid": os.getpid(), "at": (utcnow() - timedelta(hours=1)).isoformat(), "ok": True}
        )
    )
    assert not healthy(settings)


def test_copied_heartbeat_cannot_claim_another_dead_worker_is_healthy(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    (tmp_path / "runtime-heartbeat.json").write_text(
        json.dumps({"pid": 999999999, "at": utcnow().isoformat(), "ok": True})
    )
    assert not healthy(settings)


def test_mihomo_inspection_reports_groups_without_leaking_nodes_or_subscription():
    source = """mode: global
mixed-port: 7890
proxies:
  - name: SECRET_NODE
    type: vless
    uuid: SECRET_PASSWORD
proxy-groups:
  - name: PRIVATE_GROUP_NAME
    type: fallback
    proxies: [SECRET_NODE]
  - name: AUTO
    type: url-test
proxy-providers:
  sensitive:
    url: https://secret.example/private-subscription
"""
    info = mihomo_summary(source)
    assert info["automatic_groups_present"] and info["group_types"] == ["fallback", "url-test"]
    text = json.dumps(info)
    assert (
        "SECRET" not in text and "private-subscription" not in text and "PRIVATE_GROUP" not in text
    )


def test_a_select_group_does_not_claim_automatic_failover():
    assert not mihomo_summary("proxy-groups:\n  - name: VPN\n    type: select\n")[
        "automatic_groups_present"
    ]


async def test_proxy_readiness_retries_auth_failure_without_direct_fallback(monkeypatch):
    get = AsyncMock(
        side_effect=[SimpleNamespace(status_code=407), SimpleNamespace(status_code=200)]
    )

    class Client:
        def __init__(self, **kwargs):
            assert (
                kwargs["proxy"] == "http://user:password@localhost:7890" and not kwargs["trust_env"]
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args):
            return await get(*args)

    monkeypatch.setattr("betbot.runtime.httpx.AsyncClient", Client)
    monkeypatch.setattr("betbot.runtime.asyncio.sleep", AsyncMock())
    await wait_proxy("http://user:password@localhost:7890", 20)
    assert get.await_count == 2


async def test_proxy_readiness_timeout_stops_startup(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args):
            return SimpleNamespace(status_code=503)

    monkeypatch.setattr("betbot.runtime.httpx.AsyncClient", Client)
    with pytest.raises(RuntimeError, match="VPN-прокси"):
        await wait_proxy("http://localhost:7890", 0)
