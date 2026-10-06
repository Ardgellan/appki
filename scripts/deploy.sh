#!/usr/bin/env bash
# Build locally, deploy to the owner's existing VPS. Never prints .env values.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"
host=${APPKI_SSH_HOST:-root@85.239.144.214}
python_bin=${APPKI_PYTHON:-$repo/.venv/bin/python}
if [ ! -f .env ]; then echo 'Создайте .env из .env.example.' >&2; exit 1; fi
if [ ! -x "$python_bin" ]; then echo 'Сначала создайте .venv и установите backend[test].' >&2; exit 1; fi
if [ -n "${APPKI_NODE_DIR:-}" ]; then export PATH="$APPKI_NODE_DIR:$PATH"; fi
"$python_bin" - <<'PY'
from appki.config import Settings
try:
    settings = Settings()
except Exception:
    raise SystemExit('Некорректная .env. Проверьте формат и обязательные настройки.')
if settings.yookassa_mode != 'test':
    raise SystemExit('Этот скрипт развёртывает тестовый магазин. Боевой запуск оформляется отдельно.')
if settings.public_base_url != 'https://85.239.144.214':
    raise SystemExit('Для другого домена сначала обновите конфигурацию HTTPS/nginx.')
PY
(cd web && npm ci --no-audit --no-fund && npm test && npm run build)
(cd backend && "$python_bin" -m pytest -q)
release=$(date -u +%Y%m%dT%H%M%SZ)-$("$python_bin" -c 'import secrets; print(secrets.token_hex(3))')
package=$(mktemp -d)
trap 'rm -rf "$package"' EXIT
# Explicit allowlist excludes .git, .env, IPA, local databases and developer caches.
COPYFILE_DISABLE=1 tar --exclude='__pycache__' --exclude='*.egg-info' --exclude='.pytest_cache' \
    -czf "$package/release.tgz" backend/appki backend/pyproject.toml backend/requirements.lock web/dist deploy scripts README.md docs
ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15)
ssh "${ssh_opts[@]}" "$host" "umask 077; mkdir -p /opt/appki/uploads/$release"
scp "${ssh_opts[@]}" "$package/release.tgz" "$host:/opt/appki/uploads/$release/release.tgz"
scp "${ssh_opts[@]}" .env "$host:/opt/appki/uploads/$release/appki.env"
ssh "${ssh_opts[@]}" "$host" "bash -s -- '$release'" < scripts/deploy-remote.sh
