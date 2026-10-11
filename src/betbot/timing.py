"""Separate source delay, known game time, and an unverified publication stage."""

from .domain import Draft, Prediction, utcnow


def source_delay(draft: Draft) -> int:
    # OpenDota /live copies Steam TopLiveGame; it is not a second real-time clock.
    # This also corrects the old saved assumption clock_is_delayed=False.
    dota_tv = draft.source_name in (
        "OpenDota",
        "Steam",
        "Steam Top",
    ) or draft.source_name.startswith("Steam + ")
    return max(0, draft.delay_seconds) if draft.clock_is_delayed or dota_tv else 0


def draft_timing(draft: Draft, max_game_time=90, now=None) -> str:
    if not draft.clock_known or not draft.source_updated_at:
        return "unverified"
    age = max(0, ((now or utcnow()) - draft.source_updated_at).total_seconds())
    return "late" if draft.game_time + source_delay(draft) + age > max_game_time else "early"


def prediction_timing(prediction: Prediction) -> str:
    if prediction.timing_status in ("early", "late", "unverified"):
        return prediction.timing_status
    stage = draft_timing(prediction.draft, now=prediction.draft.observed_at)
    # Preserve old explicit late flags, while correcting clocks from delayed sources.
    return "late" if prediction.late_analysis and stage == "early" else stage


def separate_timing(prediction: Prediction) -> bool:
    return prediction_timing(prediction) != "early"


def timing_note(prediction: Prediction) -> str:
    if not separate_timing(prediction):
        return ""
    delay = source_delay(prediction.draft)
    if delay:
        amount = f"{delay // 60} мин" if delay % 60 == 0 else f"{delay} сек"
        return f"Данные DotaTV · задержка источника {amount}."
    if prediction_timing(prediction) == "unverified":
        return "Время начала карты не подтверждено."
    return "Разбор после начала карты."
