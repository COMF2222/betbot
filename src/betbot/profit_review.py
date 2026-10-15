"""One fixed sample review and private notification, independent of selection rules."""

from html import escape

from .domain import utcnow
from .profit_lab import PROTOCOL, summarize


def checkpoint(entries):
    """Choose the cohort by forecast order and decisions, never by wins or ROI."""
    cohort = []
    controls = []
    groups = set()
    cutoff = None
    for entry in sorted(entries, key=lambda item: item["forecast_id"]):
        cohort.append(entry)
        if entry["decisions"]["model_all"]["take"]:
            controls.append(entry)
            groups.add(entry["cluster"])
        if (
            len(controls) >= PROTOCOL["minimum_forward_trades"]
            and len(groups) >= PROTOCOL["minimum_forward_clusters"]
        ):
            cutoff = entry["forecast_id"]
            break
    pending = sum(
        not isinstance(entry.get("radiant_win"), bool)
        and any(pick["take"] for pick in entry["decisions"].values())
        for entry in cohort
    )
    return {
        "cohort": cohort,
        "cutoff_forecast_id": cutoff,
        "selected": len(controls),
        "settled": sum(isinstance(entry.get("radiant_win"), bool) for entry in controls),
        "clusters": len(groups),
        "pending_maps": pending,
        "ready": cutoff is not None and pending == 0,
    }


def progress_text(entries, review=None):
    if review:
        return "Первый этап завершён. Итог сохранён; уведомление отправляется только в личку."
    state = checkpoint(entries)
    text = (
        f"До первого отчёта: <b>{state['settled']}/{PROTOCOL['minimum_forward_trades']}</b> "
        f"рассчитанных контрольных ставок · серии: <b>{state['clusters']}/{PROTOCOL['minimum_forward_clusters']}</b>."
    )
    if state["cutoff_forecast_id"] is not None and state["pending_maps"]:
        text += f" Выборка набрана, ждём исход ещё {state['pending_maps']} карт."
    text += (
        " Контроль — фавориты основной модели с исходным кэфом ≥1.30. "
        "Одна карта считается один раз, восемь вариантов не складываются. "
        "Когда выборка будет рассчитана, пришлю итог в личку."
    )
    return text


def review_text(review):
    state = review["sample"]
    early = review["early_report"]
    text = (
        "🧪 <b>Первый этап проверки доходности завершён</b>\n\n"
        f"Контроль: <b>{state['settled']} рассчитанных ставок</b> · {state['clusters']} серий.\n"
        f"Подтверждённо ранних в контроле: {state['early_controls']}.\n\n"
        "<b>Восемь вариантов · ROI с условными расходами 2% / 5%</b>\n"
    )
    for row in review["report"]["policies"]:
        values = (
            f"{row['roi']:+.1%} / {row['stress_5pct_roi']:+.1%}" if row["settled"] else "нет ставок"
        )
        text += f"{escape(row['name'])}: <b>{values}</b> · {row['wins']}/{row['settled']}\n"
    candidates = [row for row in early["policies"] if row["review_candidate"]]
    if candidates:
        names = ", ".join(escape(row["name"]) for row in candidates)
        verdict = (
            f"Есть кандидаты для следующей проверки: {names}. "
            "Это результат выборки, будущая прибыль не гарантирована."
        )
    elif not any(
        row["settled"] >= PROTOCOL["minimum_forward_trades"]
        and row["clusters"] >= PROTOCOL["minimum_forward_clusters"]
        for row in early["policies"]
    ):
        verdict = (
            "Для вывода о доходности сразу после драфта данных пока недостаточно. "
            "Поздние наблюдения не подтверждают результат раннего входа."
        )
    else:
        verdict = "На этой выборке прибыльная стратегия не подтверждена."
    text += (
        f"\n<b>Вердикт:</b> {verdict}\n\n"
        "У каждого варианта своя выборка; редким фильтрам 100 контрольных карт может не хватить. "
        "Расходы 2%/5% — допущение, фактическое исполнение не проверено. "
        "Итог этого этапа зафиксирован. Новые наблюдения собираются дальше; банк и ставки не переключены."
    )
    return text


class ProfitReview:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def key(run):
        return f"profit_lab_review:{run['id']}:first"

    def saved(self, run):
        return self.store.get(self.key(run))

    def prepare(self, run, entries):
        saved = self.saved(run)
        if saved:
            return saved
        state = checkpoint(entries)
        if not state["ready"]:
            return None
        cohort = state.pop("cohort")
        early = [entry for entry in cohort if entry["timing"] == "early"]
        state["early_controls"] = sum(entry["decisions"]["model_all"]["take"] for entry in early)
        review = {
            "run_id": run["id"],
            "completed_at": utcnow().isoformat(),
            "sample": state,
            "forecast_ids": [entry["forecast_id"] for entry in cohort],
            "report": summarize(cohort, phase="forward"),
            "early_report": summarize(early, phase="forward"),
        }
        review["text"] = review_text(review)
        self.store.set(self.key(run), review)
        return review
