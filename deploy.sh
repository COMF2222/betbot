#!/usr/bin/env bash
# Direct access or an existing Mihomo. Never changes the VPN container/configuration.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
umask 077

if ! docker info >/dev/null 2>&1; then
  echo 'Docker недоступен. Проверьте docker.service и доступ к Docker.' >&2
  exit 1
fi
docker compose version >/dev/null
if [[ ! -f .env || ! -f data/model.json ]]; then
  echo 'Нужны .env и data/model.json из серверного архива.' >&2
  exit 1
fi

action=${1:-setup}
if [[ "$action" == setup-direct ]]; then
  printf 'DEPLOY_MODE=direct\n' > deploy.env
  echo 'Прямое подключение без VPN.'
fi
if [[ "$action" == setup ]]; then
  mihomo_name=${2:-}
  if [[ -z "$mihomo_name" ]]; then
    mapfile -t candidates < <(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($0) ~ /mihomo|clash-meta/ {print $1}')
    if [[ ${#candidates[@]} -ne 1 ]]; then
      echo 'Укажите контейнер явно: bash deploy.sh setup ИМЯ_MIHOMO ПОРТ'
      echo 'Запущенные контейнеры:'
      docker ps --format 'table {{.Names}}\t{{.Image}}'
      exit 1
    fi
    mihomo_name=${candidates[0]}
  fi
  if [[ ! "$mihomo_name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
    echo 'Некорректное имя контейнера Mihomo.' >&2
    exit 1
  fi
  if [[ $(docker inspect --format '{{.State.Running}}' "$mihomo_name") != true ]]; then
    echo 'Выбранный Mihomo сейчас не запущен.' >&2
    exit 1
  fi
  proxy_port=${3:-}
  if [[ -z "$proxy_port" ]]; then
    # Read only the numeric mixed/HTTP port. No credentials or complete config in output.
    proxy_port=$(docker exec "$mihomo_name" sh -c 'for f in /root/.config/mihomo/config.yaml /root/.config/clash/config.yaml /etc/mihomo/config.yaml /config/config.yaml; do if [ -f "$f" ]; then sed -n "s/^mixed-port: *\([0-9][0-9]*\).*/\1/p; s/^port: *\([0-9][0-9]*\).*/\1/p" "$f" | head -n 1; fi; done' 2>/dev/null | head -n 1) || true
    proxy_port=${proxy_port:-7890}
  fi
  if [[ ! "$proxy_port" =~ ^[0-9]+$ ]] || (( proxy_port < 1 || proxy_port > 65535 )); then
    echo 'Порт должен быть числом от 1 до 65535.' >&2
    exit 1
  fi
  docker_mode=$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$mihomo_name")
  network_mode="container:$mihomo_name"
  if [[ "$docker_mode" == host ]]; then network_mode=host; fi
  if [[ ! -f deploy.env ]]; then
    printf 'MIHOMO_CONTAINER=%s\nMIHOMO_NETWORK_MODE=%s\nVPN_PROXY_URL=http://127.0.0.1:%s\n' "$mihomo_name" "$network_mode" "$proxy_port" > deploy.env
  else
    echo 'Существующий deploy.env сохранён. Для смены Mihomo отредактируйте его.'
  fi
  echo "Используем существующий Mihomo: $mihomo_name, порт $proxy_port."
fi

if [[ ! -f deploy.env ]]; then
  echo 'Сначала выполните: bash deploy.sh setup' >&2
  exit 1
fi
deploy_mode=$(sed -n 's/^DEPLOY_MODE=//p' deploy.env)
deploy_mode=${deploy_mode:-mihomo}
case "$deploy_mode" in
  direct) override=compose.direct.yaml ;;
  mihomo) override=compose.mihomo.yaml ;;
  *) echo 'DEPLOY_MODE должен быть direct или mihomo.' >&2; exit 1 ;;
esac
dc=(docker compose --project-name betbot --env-file deploy.env -f compose.yaml -f "$override")
case "$action" in
  setup|setup-direct)
    "${dc[@]}" config --quiet
    "${dc[@]}" build betbot
    # One-off permission repair, scoped strictly to the mounted bot data directory.
    "${dc[@]}" run --rm --no-deps --user 0:0 --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --entrypoint sh betbot -c 'chown -R 10001:10001 /app/data && chmod -R u+rwX /app/data'
    "${dc[@]}" run --rm --no-deps betbot preflight
    echo 'Проверка пройдена. Остановите локальный betbot run, затем: bash deploy.sh start'
    ;;
  check) "${dc[@]}" run --rm --no-deps betbot preflight ;;
  inspect)
    if [[ "$deploy_mode" == direct ]]; then echo 'Прямое подключение без VPN.'; exit 0; fi
    mihomo_name=${2:-$(sed -n 's/^MIHOMO_CONTAINER=//p' deploy.env)}
    if [[ ! "$mihomo_name" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
      echo 'Укажите имя: bash deploy.sh inspect ИМЯ_MIHOMO [ПУТЬ_К_CONFIG]' >&2
      exit 1
    fi
    docker exec "$mihomo_name" sh -c 'if [ -n "$1" ]; then cat -- "$1"; exit; fi; for f in /root/.config/mihomo/config.yaml /root/.config/clash/config.yaml /etc/mihomo/config.yaml /config/config.yaml; do if [ -f "$f" ]; then cat "$f"; exit; fi; done; echo "Не найден config.yaml. Укажите путь третьим аргументом." >&2; exit 1' sh "${3:-}" | "${dc[@]}" run --rm -T --no-deps --entrypoint python betbot -m betbot.deployment
    ;;
  start) "${dc[@]}" up -d --no-deps betbot; "${dc[@]}" ps ;;
  stop) "${dc[@]}" stop betbot ;;
  restart) "${dc[@]}" up -d --no-deps --force-recreate betbot ;;
  logs) "${dc[@]}" logs --tail=80 -f betbot ;;
  status) "${dc[@]}" ps ;;
  *) echo 'Команды: setup-direct, setup [МИХОМО] [ПОРТ], inspect [МИХОМО] [КОНФИГ], check, start, stop, restart, logs, status' >&2; exit 1 ;;
esac
