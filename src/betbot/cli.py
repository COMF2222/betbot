import argparse
import asyncio
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from .config import Settings
from .demo import demo_prediction
from .domain import utcnow
from .model import FEATURE_SETS, fit
from .network import route_label, telegram_bot, telegram_proxy
from .presentation import render_card
from .providers import BOOKMAKERS, HTTP, OddsPapi, OpenDota, Polymarket, TheOddsAPI, eligible_draft
from .service import Service
from .storage import Store


async def backtest(settings: Settings, args):
    from .backtest import markdown_report, run_backtest

    settings.prepare()
    http = HTTP(settings.proxy_url or None)
    league_names = {}
    try:
        if args.input:
            raw = json.loads(args.input.read_text(encoding="utf-8-sig"))
            rows = raw.get("rows", []) if isinstance(raw, dict) else raw
        else:
            rows = await OpenDota(http, settings.opendota_api_key).training_data(args.limit)
        try:
            leagues = await OpenDota(http, settings.opendota_api_key).leagues()
            league_names = {
                str(r["leagueid"]): str(r.get("name") or r["leagueid"]) for r in leagues
            }
        except Exception:
            pass
    finally:
        await http.close()
    scope = json.loads(args.league_list.read_text(encoding="utf-8")) if args.league_list else None
    if scope and not scope.get("events"):
        raise ValueError("В списке турниров нет событий")
    report = await asyncio.to_thread(
        run_backtest,
        rows,
        args.output,
        args.months,
        not args.static_history,
        league_names,
        settings.min_favorite_probability,
        scope=scope,
        feature_set=args.feature_set,
        end=args.end,
    )
    if scope and args.feature_set == "classic":
        destination = settings.data_dir / "reports" / "tier1"
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("report.md", "report.json", "results.csv"):
            shutil.copyfile(args.output / name, destination / name)
    print(markdown_report(report))
    print(f"Отчёт: {(args.output / 'report.md').resolve()}")


async def train(
    settings: Settings, limit: int, input_path: Path | None, feature_set: str = "classic"
):
    settings.prepare()
    if input_path:
        raw = json.loads(input_path.read_text(encoding="utf-8-sig"))
        rows = raw.get("rows", []) if isinstance(raw, dict) else raw
    else:
        http = HTTP(settings.proxy_url or None)
        try:
            rows = await OpenDota(http, settings.opendota_api_key).training_data(limit)
        finally:
            await http.close()
        (settings.data_dir / "training-data.json").write_text(
            json.dumps(
                {"obtained_at": utcnow().isoformat(), "source": "OpenDota Explorer", "rows": rows},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    report = await asyncio.to_thread(fit, rows, settings.model_path, feature_set)
    print(json.dumps(report, ensure_ascii=False, indent=2))


async def doctor(settings: Settings, check_bookmakers: bool):
    settings.prepare()
    http = HTTP(settings.proxy_url or None)
    odota, poly = OpenDota(http, settings.opendota_api_key), Polymarket(http)
    results = {
        "checked_at": utcnow().isoformat(),
        "telegram_configured": bool(settings.telegram_bot_token and settings.admins),
        "model_exists": settings.model_path.exists(),
        "sources": {},
    }
    try:

        async def check_dota():
            try:
                drafts = await odota.live()
                timely = [
                    d
                    for d in drafts
                    if not eligible_draft(
                        d, settings.max_draft_age_seconds, settings.max_game_time_seconds
                    )
                ]
                results["sources"]["OpenDota"] = {
                    "ok": True,
                    "league_games": len(drafts),
                    "eligible_drafts_now": len(timely),
                    "note": "Покрытие и задержка каждого выбранного турнира проверяются отдельно",
                }
            except Exception as exc:
                results["sources"]["OpenDota"] = {"ok": False, "error": str(exc)}

        async def check_poly():
            try:
                events = await poly.discover(force=True)
                maps = [
                    (e, m)
                    for e in events
                    for m in e.get("markets", [])
                    if any(poly.map_market(m, i) for i in range(1, 6))
                ]
                quote_ok = False
                for _, market in maps[:3]:
                    from .providers import json_list

                    ids = (
                        json_list(market.get("positionIds"))
                        if market.get("version") == "v2"
                        else json_list(market.get("clobTokenIds"))
                    )
                    if ids:
                        try:
                            book = await http.get(
                                "https://clob.polymarket.com/book", {"token_id": str(ids[0])}
                            )
                            quote_ok = bool(book.get("asks"))
                            if quote_ok:
                                break
                        except Exception:
                            continue
                results["sources"]["Polymarket"] = {
                    "ok": True,
                    "events": len(events),
                    "map_markets": len(maps),
                    "orderbook_with_asks": quote_ok,
                    "sample_event_ids": [str(e["id"]) for e in events[:3]],
                }
            except Exception as exc:
                results["sources"]["Polymarket"] = {"ok": False, "error": str(exc)}

        async def check_telegram():
            if not settings.telegram_bot_token:
                results["sources"]["Telegram"] = {
                    "ok": False,
                    "note": "Не заполнен TELEGRAM_BOT_TOKEN",
                }
                return
            bot = telegram_bot(settings)
            try:
                me = await bot.get_me()
                from aiogram.types import BotCommandScopeChat

                menus = {}
                for admin in sorted(settings.admins):
                    try:
                        commands = await bot.get_my_commands(
                            scope=BotCommandScopeChat(chat_id=admin)
                        )
                        menus[str(admin)] = [c.command for c in commands]
                    except Exception as exc:
                        menus[str(admin)] = {"error_type": type(exc).__name__}
                results["sources"]["Telegram"] = {
                    "ok": True,
                    "username": me.username,
                    "route": route_label(telegram_proxy(settings)),
                    "admin_menus": menus,
                    "note": "Проверено подключение без получения обновлений и отправки сообщений",
                }
            except Exception as exc:
                results["sources"]["Telegram"] = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "route": route_label(telegram_proxy(settings)),
                    "note": "Проверьте токен, VPN и TELEGRAM_PROXY_URL в .env",
                }
            finally:
                await bot.session.close()

        await asyncio.gather(check_dota(), check_poly(), check_telegram())
        if settings.oddspapi_api_key:
            try:
                books = await OddsPapi(
                    http, settings.oddspapi_api_key, settings.oddspapi_base_url
                ).bookmakers()
                results["sources"]["OddsPapi"] = {
                    "ok": True,
                    "bookmakers": len(books),
                    "note": "Каталог доступен; конкретные рынки, бренды и свежесть ещё требуют привязки и проверки",
                }
                (settings.data_dir / "bookmakers.json").write_text(
                    json.dumps(books, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as exc:
                results["sources"]["OddsPapi"] = {"ok": False, "error": str(exc)}
        if settings.the_odds_api_key:
            try:
                results["sources"]["The Odds API"] = await TheOddsAPI(
                    http, settings.the_odds_api_key
                ).coverage()
            except Exception as exc:
                results["sources"]["The Odds API"] = {"ok": False, "error": str(exc)}
        sites = {
            "1win": "https://1win.com",
            "BetBoom": "https://betboom.ru",
            "Winline": "https://winline.ru",
            "PARI": "https://pari.ru",
        }
        for book in BOOKMAKERS:
            row = {
                "connected": False,
                "status": "requires_verified_feed",
                "note": "Рабочий источник именно этих котировок не подтверждён; нет фиктивных подключений",
            }
            if check_bookmakers:
                try:
                    response = await http.client.get(sites[book])
                    row["homepage_http_status"] = response.status_code
                    row["homepage_content_type"] = response.headers.get("content-type", "")
                    row["note"] += ". Доступность страницы не подтверждает наличие API линии."
                except Exception as exc:
                    row["homepage_error"] = type(exc).__name__
            results["sources"][book] = row
    finally:
        await http.close()
    path = settings.data_dir / "source-audit.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


async def demo(settings: Settings, output: Path, assets: bool, report: bool = False):
    settings.prepare()
    if report:
        from .reports import demo_report, render_report

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(render_report(demo_report()))
        print(f"Демонстрация итогов записана: {output.resolve()}")
        return
    store = Store(Path(":memory:"))
    service = Service(settings, store)
    try:
        cached = settings.data_dir / "heroes.json"
        if cached.exists():
            service.heroes = {
                int(k): v for k, v in json.loads(cached.read_text(encoding="utf-8")).items()
            }
        if assets:
            service.heroes = {int(h["id"]): h for h in await service.odota.heroes()}
            await service.hero_assets(demo_prediction().draft)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(
            render_card(demo_prediction(), service.heroes, service.assets, demo=True)
        )
        print(f"Демонстрация записана: {output.resolve()}")
    finally:
        await service.close()
        store.close()


async def telegram_ids(settings: Settings):
    if not settings.telegram_bot_token:
        raise ValueError(
            "Сначала заполните TELEGRAM_BOT_TOKEN в .env, отправьте боту /start и повторите команду"
        )
    bot = telegram_bot(settings)
    try:
        updates = await bot.get_updates(timeout=0, limit=100)
        seen = set()
        for update in updates:
            message = update.message
            if (
                message
                and message.chat.type == "private"
                and message.from_user
                and message.from_user.id not in seen
            ):
                user = message.from_user
                print(f"{user.id} · {user.full_name} · @{user.username or 'без username'}")
                seen.add(user.id)
        if not seen:
            print(
                "Личных сообщений пока нет. Откройте своего бота, отправьте /start и повторите команду. Основной процесс бота должен быть остановлен."
            )
    finally:
        await bot.session.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Betbot · Telegram-анализ Dota 2")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Запустить Telegram-бота и worker")
    sub.add_parser(
        "telegram-id",
        help="Узнать свой Telegram ID из личного сообщения боту перед первым запуском",
    )
    training = sub.add_parser("train", help="Собрать историю и обучить модель с временным тестом")
    training.add_argument("--limit", type=int, default=5000)
    training.add_argument("--input", type=Path)
    training.add_argument("--feature-set", choices=FEATURE_SETS, default="classic")
    historical = sub.add_parser(
        "backtest", help="Заморозить модель до периода и проверить исторические карты"
    )
    historical.add_argument("--months", type=int, default=4)
    historical.add_argument(
        "--end",
        type=datetime.fromisoformat,
        help="Конец проверки ISO 8601 для повторения того же периода",
    )
    historical.add_argument("--limit", type=int, default=20000)
    historical.add_argument("--input", type=Path)
    historical.add_argument("--output", type=Path, default=Path("artifacts/backtest"))
    historical.add_argument(
        "--league-list", type=Path, help="JSON со списком основных событий, league ID и датами"
    )
    historical.add_argument(
        "--feature-set",
        choices=FEATURE_SETS,
        default="classic",
        help="Сравнение базовой модели, состава и подробной статистики драфта",
    )
    historical.add_argument(
        "--static-history",
        action="store_true",
        help="Не обновлять даже рейтинги по предыдущим результатам тестового периода",
    )
    check = sub.add_parser("doctor", help="Проверить реальные источники без публикаций")
    check.add_argument("--check-bookmakers", action="store_true")
    example = sub.add_parser("demo", help="Создать PNG с явно условными данными")
    example.add_argument("--output", type=Path, default=Path("artifacts/demo.png"))
    example.add_argument(
        "--report", action="store_true", help="Демонстрационная карточка итогов дня"
    )
    example.add_argument("--with-assets", action="store_true")
    return root


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    try:
        settings = Settings()
        if args.command == "run":
            from .telegram import configure_logging, run_bot

            configure_logging()
            asyncio.run(run_bot(settings))
        elif args.command == "train":
            asyncio.run(train(settings, args.limit, args.input, args.feature_set))
        elif args.command == "backtest":
            asyncio.run(backtest(settings, args))
        elif args.command == "doctor":
            asyncio.run(doctor(settings, args.check_bookmakers))
        elif args.command == "demo":
            asyncio.run(demo(settings, args.output, args.with_assets, args.report))
        elif args.command == "telegram-id":
            asyncio.run(telegram_ids(settings))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Never print a request URL or configuration dump containing API keys.
        print(f"Не удалось выполнить команду: {type(exc).__name__}", file=sys.stderr)
        if isinstance(exc, ValueError) and not isinstance(exc, ValidationError):
            print(str(exc)[:300], file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
