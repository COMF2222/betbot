"""Reasons why a forecast has no map price, frozen with the forecast."""

MARKET_ISSUES = {
    "map_unknown": "номер карты ещё не подтверждён",
    "market_unmatched": "не удалось найти однозначный рынок этого матча на Polymarket",
    "map_market_missing": "на Polymarket нет отдельного рынка победителя этой карты",
    "map_market_closed": "рынок этой карты на Polymarket закрыт для торговли",
    "market_ambiguous": "на Polymarket найдено несколько рынков одной карты",
    "market_identity": "команды или токены рынка не совпадают с этой картой",
    "prices_unavailable": "Polymarket не дал согласованные цены на эту карту",
    "market_source_error": "Polymarket временно не ответил при получении котировок",
    "market_disabled": "получение котировок Polymarket выключено",
}
