"""Wait for original odds before any post, without blocking other maps."""

import math
from dataclasses import replace
from datetime import timedelta

from .domain import timestamp, utcnow
from .presentation import market_snapshot
from .timing import draft_timing, source_delay


def original_pair_ready(prediction, max_age, now):
    check = replace(
        prediction,
        quotes=[
            q
            for q in prediction.quotes
            if q.source == "Polymarket" and q.price_origin == "live" and q.price_kind == "market"
        ],
        analysis={**prediction.analysis, "initial_odds_source": "Polymarket"},
    )
    own, other, _ = market_snapshot(check, max_age, now)
    return bool(
        own
        and other
        and all(
            q.ask_odds and math.isfinite(q.ask_odds) and 1 < q.ask_odds <= 1_000_000
            for q in (own, other)
        )
    )


def price_for_send(prediction, created_at, now, max_age):
    from .paper_bank import initial_quote

    return bool(
        initial_quote(prediction, created_at, max_age)
        and initial_quote(prediction, now, max_age)
        and market_snapshot(prediction, max_age, now)[0]
    )


def deadline(draft, now, settings):
    # Leave time for the final fetch and card generation before freshness expires.
    end = min(
        now + timedelta(seconds=settings.quote_wait_seconds),
        draft.observed_at + timedelta(seconds=max(0, settings.max_draft_age_seconds - 15)),
        (draft.source_updated_at or draft.observed_at)
        + timedelta(seconds=max(0, settings.max_draft_age_seconds - 15)),
    )
    if draft_timing(draft, settings.max_game_time_seconds, now) == "early":
        age = max(0, (now - draft.source_updated_at).total_seconds())
        remaining = settings.max_game_time_seconds - draft.game_time - source_delay(draft) - age
        end = min(end, now + timedelta(seconds=max(0, remaining - 15)))
    return max(now, end)


def defer(service, prediction):
    """A retry re-evaluates the live draft; no forecast or bet exists while waiting."""
    from .paper_bank import initial_quote, initial_source

    strict = service.settings.require_initial_odds and bool(service.bot)
    priority = bool(
        service.settings.quote_wait_seconds
        and service.store.get("source:Polymarket", True)
        and service.store.get(f"poly_league:{prediction.draft.league_id}") != ""
    )
    legacy = (
        priority
        and service.automatic
        and not service.paused
        and service.chat_id
        and service.model.publication_ready
    )
    if not strict and not legacy:
        return False
    now = utcnow()
    key = f"quote_wait:{prediction.draft.match_id}"
    state = service.store.get(key)
    ready = original_pair_ready(prediction, service.settings.quote_max_age_seconds, now)
    source = initial_source(prediction, now, service.settings.quote_max_age_seconds)
    priced = replace(prediction, analysis={**prediction.analysis, "initial_odds_source": source})
    executable = bool(initial_quote(priced, now, service.settings.quote_max_age_seconds))
    ready = ready and executable
    if not state and (ready or not priority and executable):
        return False
    if not state:
        state = {
            "started_at": now.isoformat(),
            "deadline": (
                deadline(prediction.draft, now, service.settings) if priority else now
            ).isoformat(),
            "checks": [],
        }
    if state.get("finished_at") and not strict:
        prediction.analysis = {**prediction.analysis, "quote_wait": state}
        return False
    state.pop("finished_at", None)
    state.pop("outcome", None)
    state["checks"].append(
        {
            "at": now.isoformat(),
            "pair_ready": ready,
            "status": service.store.get(f"market_status:{prediction.draft.match_id}", {}).get(
                "code"
            ),
        }
    )
    state["checks"] = state["checks"][-12:]
    expired = not priority or now >= timestamp(state["deadline"])
    if ready or expired and (executable or not strict):
        state.update(
            finished_at=now.isoformat(),
            outcome="paired_price" if ready else "priced_fallback" if executable else "timeout",
            source=source if executable else None,
        )
        prediction.analysis = {**prediction.analysis, "quote_wait": state}
        service.store.set(key, state)
        return False
    service.store.set(key, state)
    service.store.mark(
        prediction.draft.match_id,
        "waiting_quotes",
        "Повторно проверяем кэфы Polymarket перед публикацией",
    )
    return True
