"""Print only non-secret Mihomo routing metadata from a local config on stdin."""

import json
import re
import sys


def mihomo_summary(source):
    if source.lstrip().startswith("{"):
        data = json.loads(source)
        groups = [g.get("type") for g in data.get("proxy-groups", [])]
        ports = {key: data[key] for key in ("mixed-port", "port", "socks-port") if key in data}
        mode = data.get("mode")
    else:
        ports = {}
        for key in ("mixed-port", "port", "socks-port"):
            found = re.search(r"(?m)^" + key + r":\s*(\d+)", source)
            if found:
                ports[key] = int(found[1])
        found = re.search(r"(?m)^mode:\s*[\"']?(\w+)", source)
        mode = found[1] if found else None
        block = re.search(r"(?ms)^proxy-groups:\s*\n(.*?)(?=^[a-zA-Z][\w-]*:|\Z)", source)
        groups = re.findall(r"\btype:\s*[\"']?([\w-]+)", block[1]) if block else []
    allowed_types = {"select", "url-test", "fallback", "load-balance", "relay"}
    groups = sorted({g for g in groups if g in allowed_types})
    return {
        "ports": {k: int(v) for k, v in ports.items()},
        "mode": mode if mode in ("rule", "global", "direct") else "unknown",
        "group_types": groups,
        "automatic_groups_present": bool({"fallback", "url-test"} & set(groups)),
        "note": "Типы из конфига; фактически выбранная группа и её узел здесь не определяются.",
    }


if __name__ == "__main__":
    try:
        print(json.dumps(mihomo_summary(sys.stdin.read()), ensure_ascii=False))
    except (ValueError, TypeError):
        print(
            "Не удалось прочитать параметры Mihomo; содержимое конфигурации не выводится.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
