from .domain import Draft, Player, Prediction, Quote, utcnow


def demo_prediction() -> Prediction:
    now = utcnow()
    draft = Draft(
        "DEMO",
        0,
        1,
        2,
        "Команда Север",
        "Команда Юг",
        tuple(
            Player(i + 1, h, name)
            for i, (h, name) in enumerate(
                zip(
                    (1, 13, 29, 5, 30), ("North", "Midnight", "Stone", "Frost", "Echo"), strict=True
                )
            )
        ),
        tuple(
            Player(i + 11, h, name)
            for i, (h, name) in enumerate(
                zip((54, 11, 2, 20, 26), ("South", "Shadow", "Axe", "Reef", "Hex"), strict=True)
            )
        ),
        now,
        now,
        -100,
        "demo",
        2,
    )
    quotes = [
        Quote(
            "Polymarket",
            "DEMO",
            "map_winner",
            "radiant",
            1.82,
            now,
            None,
            2,
            rules="demo",
            gross=True,
        )
    ]
    quotes.append(
        Quote(
            "Polymarket", "DEMO", "map_winner", "dire", 2.24, now, None, 2, rules="demo", gross=True
        )
    )
    return Prediction(
        draft,
        0.58,
        0.46,
        "DEMO — условные данные",
        ["Демонстрационный разбор. Числа и команды не относятся к реальному матчу."],
        "demo",
        quotes,
        2400,
        (1980, 2880),
        reliability={
            "combined": {"count": 250, "success_rate": 0.59},
            "draft": {"count": 240, "success_rate": 0.53},
        },
        league_name="Демонстрационный турнир",
    )
