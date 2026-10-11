import html
import re
from dataclasses import replace

from betbot.presentation import caption, explanation


def test_admin_hero_samples_are_visible_without_expanding_public_post(prediction):
    samples = {}
    for side in ("radiant", "dire"):
        samples[side] = [
            {
                "account_id": p.account_id,
                "count": 60,
                "wins": 40,
                "win_rate": 40 / 60,
                "pick_share": 0.2,
            }
            for p in getattr(prediction.draft, side)
        ]
    p = replace(prediction, analysis={**prediction.analysis, "comfort": samples})
    text = explanation(p, {})
    assert text.count("40/60 побед (67%)") == 10
    assert text.count("20% его про-карт") == 10
    assert "40/60" not in caption(p)
    # Limit is applied by Telegram after parsing HTML.
    assert len(html.unescape(re.sub(r"<[^>]*>", "", text))) < 4096
