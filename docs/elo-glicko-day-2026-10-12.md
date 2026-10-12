# Elo и Glicko: 2026-10-12

Период: **12.10.2026, 00:00–23:59 МСК**. Включены все 20 уникальных опубликованных прогнозов этого дня, включая слабые сигналы и разборы с задержкой. Дата определяется временем создания прогноза, как в ежедневном отчёте бота.

Фактически опубликовано: **10 попаданий из 20**. Это 10 попаданий и 10 промахов. Модель публикаций менялась в течение дня; пересчёт ниже показывает результат каждой модели на всех одинаковых картах.

| Модель весь день | Попадания | Точность | Brier, меньше лучше | Сигналы ≥55% |
| --- | ---: | ---: | ---: | ---: |
| Модель с Elo | 12/20 | 60.0% | 0.253273 | 5/12 |
| Модель с Glicko | 10/20 | 50.0% | 0.249556 | 7/15 |

## По турнирам

| Турнир | Elo | Glicko |
| --- | ---: | ---: |
| DreamLeague Season 30 Qualifiers | 7/13 | 6/13 |
| EPL Masters 2026 | 5/7 | 4/7 |

## Все карты

Время публикации московское. Галочка означает, что выбранная модель назвала победившую команду. Проценты — пересчитанные вероятности, без округления при выборе фаворита.

| Время | Турнир | Карта | Победитель | Elo | Glicko |
| --- | --- | --- | --- | --- | --- |
| 09:16 | DreamLeague Season 30 Qualifiers | Imperial Power — Crescent · #1 | Crescent | ❌ Imperial Power 58.1% | ❌ Imperial Power 64.6% |
| 10:34 | DreamLeague Season 30 Qualifiers | Imperial Power — Crescent · #2 | Imperial Power | ✅ Imperial Power 53.3% | ❌ Crescent 55.1% |
| 11:13 | DreamLeague Season 30 Qualifiers | Imperial Power — Crescent · #3 | Imperial Power | ✅ Imperial Power 52.6% | ❌ Crescent 55.5% |
| 11:16 | DreamLeague Season 30 Qualifiers | Team Thailand — Team Kinetix · #1 | Team Kinetix | ❌ Team Thailand 55.3% | ❌ Team Thailand 60.4% |
| 11:22 | DreamLeague Season 30 Qualifiers | Team Nemesis Academy — Ivory · #1 | Ivory | ❌ Team Nemesis Academy 56.9% | ❌ Team Nemesis Academy 56.3% |
| 12:14 | DreamLeague Season 30 Qualifiers | Team Kinetix — Team Thailand · #2 | Team Kinetix | ✅ Team Kinetix 52.8% | ✅ Team Kinetix 61.4% |
| 12:35 | DreamLeague Season 30 Qualifiers | Ivory — Team Nemesis Academy · #2 | Team Nemesis Academy | ❌ Ivory  52.8% | ❌ Ivory  62.3% |
| 14:18 | DreamLeague Season 30 Qualifiers | Ivory — Team Nemesis Academy · #3 | Ivory | ✅ Ivory  55.9% | ✅ Ivory  52.3% |
| 16:21 | EPL Masters 2026 | CyberHero — Team Lynx · #1 | CyberHero | ✅ CyberHero 50.9% | ❌ Team Lynx 52.4% |
| 16:21 | DreamLeague Season 30 Qualifiers | Nemiga Gaming — 111 · #1 | Nemiga Gaming | ❌ 111 55.1% | ❌ 111 50.5% |
| 17:16 | DreamLeague Season 30 Qualifiers | 111 — Nemiga Gaming · #2 | Nemiga Gaming | ❌ 111 62.2% | ✅ Nemiga Gaming 51.5% |
| 17:22 | DreamLeague Season 30 Qualifiers | Old blood — WhiteSails · #1 | WhiteSails | ✅ WhiteSails 53.2% | ✅ WhiteSails 58.6% |
| 17:22 | DreamLeague Season 30 Qualifiers | Dawn Bulls — Mena Warriors · #1 | Dawn Bulls | ✅ Dawn Bulls 59.9% | ✅ Dawn Bulls 80.1% |
| 17:30 | EPL Masters 2026 | Team Lynx — CyberHero · #2 | Team Lynx | ❌ CyberHero 56.6% | ❌ CyberHero 59.5% |
| 18:20 | DreamLeague Season 30 Qualifiers | Mena Warriors — Dawn Bulls · #2 | Dawn Bulls | ✅ Dawn Bulls 58.1% | ✅ Dawn Bulls 78.6% |
| 18:22 | EPL Masters 2026 | Team Lynx — CyberHero · #3 | Team Lynx | ✅ Team Lynx 53.0% | ✅ Team Lynx 53.7% |
| 19:50 | EPL Masters 2026 | Team Lynx — Mena Warriors · #1 | Team Lynx | ✅ Team Lynx 55.7% | ✅ Team Lynx 64.8% |
| 21:06 | EPL Masters 2026 | Mena Warriors — Team Lynx · #2 | Team Lynx | ✅ Team Lynx 53.4% | ✅ Team Lynx 65.0% |
| 22:27 | EPL Masters 2026 | Dawn Bulls — The Warriors · #1 | The Warriors | ❌ Dawn Bulls 60.9% | ❌ Dawn Bulls 73.6% |
| 23:26 | EPL Masters 2026 | Dawn Bulls — The Warriors · #2 | Dawn Bulls | ✅ Dawn Bulls 61.7% | ✅ Dawn Bulls 62.8% |

## Проверка расчёта

Использованы замороженные модели `logistic-v2-89bc7fac69d6` и `glicko-team-research-v1-aea4609b642d`. Обе обучены до 2026-10-10T14:49:49+00:00; SHA-256 совпадают с файлами на сервере.

Для каждой карты восстановлена отдельная история каждой модели. Результат прошлой карты включается только после окончания с буфером 60 секунд и подтверждения источником; доступны только результаты до времени исходного драфта. Результат оцениваемой карты не участвует в её прогнозе. Модели не обучались заново и не подбирались по результатам этого дня.

Пересчитанные вероятности совпали с 20 опубликованными прогнозами: максимальная разница 0. Для 6 вечерних карт Glicko также совпал с параллельными прогнозами, записанными до подтверждения результата; максимальная разница 0.

Все 20 карт относятся к отдельному учёту по времени источника; ранних прогнозов 0. Это короткий ретроспективный срез наблюдаемых турниров, не оценка будущей точности или доходности. Карты в одной серии зависимы.

В колонке сигналов показан результат только при перевесе ≥55%; число карт у моделей отличается, поэтому основное сравнение — на всех одинаковых картах.
