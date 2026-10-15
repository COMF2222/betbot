"""One fixed sample review and private notification, independent of selection rules."""

from html import escape

from .domain import utcnow
from .profit_lab import summarize

REVIEW_VERSION = "all-50-v1"
REVIEW_TARGET = 50
REVIEW_PLAN = {
    "version": REVIEW_VERSION,
    "target_observations": REVIEW_TARGET,
    "timing": "all_including_delayed_or_unverified",
    "count_basis": "all_published_forward_forecasts_with_confirmed_result",
    "minimum_clusters": 0,
    "classification": "exploratory",
    "change_reason": "User requested a 50-map review including delayed observations",
}


def checkpoint(entries):
    """First 50 observed maps, chosen before results; skipped and late maps count."""
    cohort = sorted(entries, key=lambda item: item["forecast_id"])[:REVIEW_TARGET]
    cutoff = cohort[-1]["forecast_id"] if len(cohort) == REVIEW_TARGET else None
    pending = sum(not isinstance(entry.get("radiant_win"), bool) for entry in cohort)
    return {
        "cohort": cohort,
        "cutoff_forecast_id": cutoff,
        "selected": len(cohort),
        "settled": len(cohort) - pending,
        "clusters": len({entry["cluster"] for entry in cohort}),
        "pending_maps": pending,
        "control_settled": sum(
            isinstance(entry.get("radiant_win"), bool) and entry["decisions"]["model_all"]["take"]
            for entry in cohort
        ),
        "ready": cutoff is not None and pending == 0,
    }


def progress_text(entries, review=None):
    if review:
        return "Первый этап завершён. Итог сохранён; уведомление отправляется только в личку."
    state = checkpoint(entries)
    text = (
        f"До первого отчёта: <b>{state['settled']}/{REVIEW_TARGET}</b> "
        f"рассчитанных карт · серий: {state['clusters']}."
    )
    if state["cutoff_forecast_id"] is not None and state["pending_maps"]:
        text += f" Выборка набрана, ждём исход ещё {state['pending_maps']} карт."
    text += (
        " Считаем все наблюдения, включая поздние и пропущенные фильтрами карты. "
        "Минимума по числу серий нет. Одна карта считается один раз. "
        "Когда выборка будет рассчитана, пришлю итог в личку."
    )
    return text


def review_text(review):
    state = review["sample"]
    text = (
        "🧪 <b>Первый этап проверки доходности завершён</b>\n\n"
        f"Выборка: <b>{state['settled']} рассчитанных карт</b> · {state['clusters']} серий.\n"
        f"Ранних: {state['early_observations']} · поздних или без точного времени: {state['settled'] - state['early_observations']}.\n\n"
        "<b>Восемь вариантов · ROI с условными расходами 2% / 5%</b>\n"
    )
    for row in review["report"]["policies"]:
        values = (
            f"{row['roi']:+.1%} / {row['stress_5pct_roi']:+.1%}" if row["settled"] else "нет ставок"
        )
        text += f"{escape(row['name'])}: <b>{values}</b> · {row['wins']}/{row['settled']}\n"
    evaluated = [row for row in review["report"]["policies"] if row["settled"]]
    positive = [row for row in evaluated if row["roi"] > 0 and row["stress_5pct_roi"] > 0]
    if positive:
        names = ", ".join(escape(row["name"]) for row in positive)
        verdict = (
            f"Плюс на общей выборке при расходах 2% и 5%: {names}. "
            "Это предварительный результат этих наблюдений."
        )
    elif any(row["roi"] > 0 for row in evaluated):
        verdict = "Положительный ROI при расходах 2% не сохранился при расходах 5%."
    elif evaluated:
        verdict = "На этой общей выборке ни один вариант не вышел в плюс после расходов 2%."
    else:
        verdict = "Исходы собраны, но подходящих исходных цен для расчёта доходности не оказалось."
    text += (
        f"\n<b>Вердикт:</b> {verdict}\n\n"
        f"Это обзор первых {REVIEW_TARGET} карт, включая поздние. У фильтров своё число сделок; "
        "карты без подходящей исходной цены учитываются в прогрессе, но не в ROI. "
        "Расходы 2%/5% — допущение. Короткая выборка не подтверждает будущую доходность. "
        "Итог этого этапа зафиксирован. Новые наблюдения собираются дальше; банк и ставки не переключены."
    )
    return text


class ProfitReview:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def key(run):
        return f"profit_lab_review:{run['id']}:{REVIEW_VERSION}"

    def saved(self, run):
        return self.store.get(self.key(run))

    def prepare(self, run, entries):
        saved = self.saved(run)
        if saved:
            return saved
        plan_key = f"{self.key(run)}:plan"
        plan = self.store.get(plan_key)
        if not plan:
            plan = {
                **REVIEW_PLAN,
                "recorded_at": utcnow().isoformat(),
                "first_forecast_id": run["first_forecast_id"],
            }
            self.store.set(plan_key, plan)
        state = checkpoint(entries)
        if not state["ready"]:
            return None
        cohort = state.pop("cohort")
        early = [entry for entry in cohort if entry["timing"] == "early"]
        state["early_observations"] = len(early)
        review = {
            "run_id": run["id"],
            "review_plan": plan,
            "completed_at": utcnow().isoformat(),
            "sample": state,
            "forecast_ids": [entry["forecast_id"] for entry in cohort],
            "report": summarize(cohort, phase="forward"),
            "early_report": summarize(early, phase="forward"),
        }
        review["text"] = review_text(review)
        self.store.set(self.key(run), review)
        return review
