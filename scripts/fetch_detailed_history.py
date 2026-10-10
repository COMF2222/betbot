"""Enrich the previously frozen sample without changing its match membership."""

import asyncio
import json
from pathlib import Path

from betbot.domain import utcnow
from betbot.providers import HTTP, OpenDota, patch_for_time


async def main():
    original = json.loads(Path("artifacts/backtest/dataset.json").read_text(encoding="utf-8"))[
        "rows"
    ]
    http = HTTP()
    try:
        provider = OpenDota(http)
        richer = await provider.training_data(20000)
        patches = await provider.patches()
    finally:
        await http.close()
    indexed = {str(r["match_id"]): r for r in richer}
    rows = []
    enriched = 0
    for row in original:
        extra = indexed.get(str(row["match_id"]), {})
        enriched += bool(extra)
        rows.append({**row, **extra, "patch": patch_for_time(patches, row["start_time"])})
    target = Path("data/detailed-history.json")
    target.write_text(
        json.dumps(
            {"source": "OpenDota Explorer", "obtained_at": utcnow().isoformat(), "rows": rows},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "rows": len(rows),
                "enriched": enriched,
                "lane_rows": sum(
                    bool(r.get("lane_roles")) and any(v is not None for v in r["lane_roles"])
                    for r in rows
                ),
                "gold10_rows": sum(
                    bool(r.get("gold10")) and any(v is not None for v in r["gold10"]) for r in rows
                ),
            }
        )
    )


asyncio.run(main())
