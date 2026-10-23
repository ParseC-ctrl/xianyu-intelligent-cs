#!/usr/bin/env bash
# Mac (including Colima) and Windows WSL2 share this Linux-container deployment.
set -euo pipefail
cd "$(dirname "$0")/.."
if docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose=(docker-compose)
else
  printf '%s\n' '缺少 Docker Compose，请在当前 Linux/WSL2 环境安装 docker-compose-plugin，或使用已有兼容的 docker-compose；Windows 部署见 WINDOWS-DEPLOYMENT.md。' >&2
  exit 1
fi
if [[ ! -f config/customer.env ]]; then
  python3 scripts/setup-customer.py
fi
compose_files=(-f compose.customer.yml)
# A private machine-specific override (for example Colima DNS). Do not copy it
# to a different Windows/macOS machine without reviewing that machine's DNS.
if [[ -f .local/compose.local.yml ]]; then
  compose_files+=(-f .local/compose.local.yml)
fi
case "${1:-start}" in
  start)
    python3 scripts/sync-local-faqs.py --validate
    "${compose[@]}" --env-file config/customer.env "${compose_files[@]}" up -d --build
    python3 scripts/sync-local-faqs.py
    ;;
  stop) "${compose[@]}" --env-file config/customer.env "${compose_files[@]}" stop ;;
  status) "${compose[@]}" --env-file config/customer.env "${compose_files[@]}" ps ;;
  logs) "${compose[@]}" --env-file config/customer.env "${compose_files[@]}" logs --tail=100 customer ;;
  *) printf '%s\n' '用法：bash scripts/customer.sh [start|stop|status|logs]' >&2; exit 1 ;;
esac
