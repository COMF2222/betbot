import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


def timestamp(value: Any) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC)
        if value:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except (ValueError, OverflowError, TypeError, OSError):
        pass
    return None


@dataclass(frozen=True)
class Player:
    account_id: int
    hero_id: int
    name: str = ""
    position: int | None = None


@dataclass(frozen=True)
class Draft:
    match_id: str
    league_id: int
    radiant_id: int
    dire_id: int
    radiant_name: str
    dire_name: str
    radiant: tuple[Player, ...]
    dire: tuple[Player, ...]
    observed_at: datetime
    source_updated_at: datetime | None
    game_time: int
    series_id: str = ""
    map_number: int | None = None
    patch: int | None = None
    delay_seconds: int = 0

    @property
    def complete(self) -> bool:
        heroes = [p.hero_id for p in self.radiant + self.dire]
        return (
            len(self.radiant) == len(self.dire) == 5
            and all(h > 0 for h in heroes)
            and len(set(heroes)) == 10
            and self.radiant_id > 0
            and self.dire_id > 0
            and self.radiant_id != self.dire_id
            and bool(self.radiant_name and self.dire_name)
        )

    def to_dict(self) -> dict:
        result = asdict(self)
        result["observed_at"] = self.observed_at.isoformat()
        result["source_updated_at"] = (
            self.source_updated_at.isoformat() if self.source_updated_at else None
        )
        return result

    @classmethod
    def from_dict(cls, raw: dict) -> "Draft":
        values = dict(raw)
        for side in ("radiant", "dire"):
            values[side] = tuple(Player(**p) for p in values[side])
        values["observed_at"] = timestamp(values["observed_at"])
        values["source_updated_at"] = timestamp(values.get("source_updated_at"))
        return cls(**values)


@dataclass(frozen=True)
class Quote:
    source: str
    match_id: str
    market: str
    outcome: str
    odds: float
    observed_at: datetime
    source_updated_at: datetime | None
    map_number: int | None
    line: float | None = None
    rules: str = ""
    active: bool = True
    url: str = ""
    gross: bool = False
    liquidity: float | None = None
    market_id: str = ""

    def fresh(self, now: datetime, max_age: int) -> bool:
        age = (now - (self.source_updated_at or self.observed_at)).total_seconds()
        return self.active and 0 <= age <= max_age and math.isfinite(self.odds) and self.odds > 1

    def to_dict(self) -> dict:
        result = asdict(self)
        result["observed_at"] = self.observed_at.isoformat()
        result["source_updated_at"] = (
            self.source_updated_at.isoformat() if self.source_updated_at else None
        )
        return result

    @classmethod
    def from_dict(cls, raw: dict) -> "Quote":
        values = dict(raw)
        values["observed_at"] = timestamp(values["observed_at"])
        values["source_updated_at"] = timestamp(values.get("source_updated_at"))
        return cls(**values)


def best_quotes(quotes: list[Quote], now: datetime, max_age: int) -> list[Quote]:
    """One maximum per exact market/rules/cost class, never merge gross and net prices."""
    best: dict[tuple, Quote] = {}
    for q in quotes:
        if not q.fresh(now, max_age):
            continue
        key = (q.match_id, q.market, q.map_number, q.outcome, q.line, q.rules, q.gross)
        if key not in best or q.odds > best[key].odds:
            best[key] = q
    return list(best.values())


@dataclass
class Prediction:
    draft: Draft
    radiant_probability: float
    draft_probability: float
    model_version: str
    reasons: list[str]
    quality: str
    quotes: list[Quote] = field(default_factory=list)
    duration_seconds: float | None = None
    duration_interval: tuple[float, float] | None = None
    totals: list[dict] = field(default_factory=list)
    publication_ready: bool = False
    analysis: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)
    history_as_of: str = ""
    reliability: dict = field(default_factory=dict)
    league_name: str = ""

    @property
    def favorite(self) -> str:
        return "radiant" if self.radiant_probability >= 0.5 else "dire"

    @property
    def favorite_probability(self) -> float:
        return max(self.radiant_probability, 1 - self.radiant_probability)

    @property
    def favorite_name(self) -> str:
        return self.draft.radiant_name if self.favorite == "radiant" else self.draft.dire_name

    def to_dict(self) -> dict:
        return {
            "draft": self.draft.to_dict(),
            "radiant_probability": self.radiant_probability,
            "draft_probability": self.draft_probability,
            "model_version": self.model_version,
            "reasons": self.reasons,
            "quality": self.quality,
            "quotes": [q.to_dict() for q in self.quotes],
            "duration_seconds": self.duration_seconds,
            "duration_interval": self.duration_interval,
            "totals": self.totals,
            "publication_ready": self.publication_ready,
            "analysis": self.analysis,
            "features": self.features,
            "history_as_of": self.history_as_of,
            "reliability": self.reliability,
            "league_name": self.league_name,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Prediction":
        values = dict(raw)
        values["draft"] = Draft.from_dict(values["draft"])
        values["quotes"] = [Quote.from_dict(q) for q in values.get("quotes", [])]
        return cls(**values)
