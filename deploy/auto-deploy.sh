#!/usr/bin/env bash
#
# Pull-деплой Show Ring: systemd-таймер (deploy/systemd/) раз в несколько
# минут запускает этот скрипт на сервере. Если в main любого из двух
# репозиториев появился новый коммит и его CI в GitHub зелёный — перематывает
# рабочие копии ровно на проверенные коммиты и запускает deploy.sh.
#
# Почему не self-hosted runner (аудит безопасности 03.10.2026, № 3): runner
# выполняет workflow из коммита, который его запустил, — в публичном
# репозитории PR из форка мог бы выполнить свой код на прод-сервере рядом с
# .env. Здесь сервер сам ходит в GitHub только на чтение: ни входящих
# подключений, ни токенов, ни чужого кода.
#
# Ручной запуск (то же, что делает таймер):  <бэк>/deploy/auto-deploy.sh
# Приватные репозитории: GITHUB_TOKEN (fine-grained, только чтение
# Checks/Contents) в окружении сервиса — иначе API статуса CI вернёт 404.
set -euo pipefail

# Весь код — внутри main: скрипт обновляет СВОЙ же файл (git merge в
# бэкенд-репо), а bash читает файл по ходу выполнения. Функцию он разбирает
# целиком до вызова, поэтому замена файла на диске выполнение не ломает.
main() {
    local script_dir backend_dir frontend_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    backend_dir="$(dirname "$script_dir")"
    frontend_dir="${SHOW_RING_FRONTEND_DIR:-$(dirname "$backend_dir")/show-ring-frontend}"

    # Таймер не должен запускать вторую копию, пока идёт сборка.
    exec 8>/tmp/show-ring-autodeploy.lock
    if ! flock -n 8; then
        log "предыдущий запуск ещё идёт — пропускаю"
        return 0
    fi

    local dir local_sha remote_sha slug status
    local -a targets=()
    for dir in "$backend_dir" "$frontend_dir"; do
        git -C "$dir" fetch -q origin main
        local_sha="$(git -C "$dir" rev-parse HEAD)"
        remote_sha="$(git -C "$dir" rev-parse origin/main)"
        [ "$local_sha" = "$remote_sha" ] && continue

        slug="$(repo_slug "$dir")"
        status="$(ci_status "$slug" "$remote_sha")"
        case "$status" in
            success) targets+=("$dir=$remote_sha") ;;
            pending)
                log "$slug ${remote_sha:0:7}: CI ещё идёт — проверю в следующий раз"
                return 0 ;;
            *)
                log "$slug ${remote_sha:0:7}: CI $status — не деплою"
                return 0 ;;
        esac
    done

    if [ "${#targets[@]}" -eq 0 ]; then
        return 0
    fi

    local target
    for target in "${targets[@]}"; do
        dir="${target%%=*}"
        log "$(repo_slug "$dir") -> ${target#*=}"
        git -C "$dir" merge -q --ff-only "${target#*=}"
    done

    SHOW_RING_SKIP_PULL=1 "$backend_dir/deploy/deploy.sh"
}

log() { echo "[auto-deploy $(date -Iseconds)] $*"; }

# owner/repo из адреса origin (https://github.com/o/r.git или git@github.com:o/r.git).
repo_slug() {
    git -C "$1" remote get-url origin | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##'
}

# success | pending | failure — по check-runs коммита. audit — сигнал, не
# блокировка (как и в защите ветки), deploy — старый job, если остался.
ci_status() {
    local auth=()
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        auth=(-H "Authorization: Bearer $GITHUB_TOKEN")
    fi
    curl -fsS --max-time 20 "${auth[@]}" \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/$1/commits/$2/check-runs?per_page=100" \
    | python3 -c '
import json, sys
runs = [r for r in json.load(sys.stdin)["check_runs"]
        if r["name"] not in ("audit", "deploy")]
if not runs or any(r["status"] != "completed" for r in runs):
    print("pending")
elif all(r["conclusion"] in ("success", "skipped", "neutral") for r in runs):
    print("success")
else:
    print("failure")
'
}

main "$@"; exit
