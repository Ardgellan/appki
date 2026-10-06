#!/usr/bin/env bash
set -euo pipefail
release=${1:?release required}
case "$release" in *[!0-9a-zA-Z-]*|'') echo 'Invalid release' >&2; exit 1;; esac
upload="/opt/appki/uploads/$release"
target="/opt/appki/releases/$release"
test -s "$upload/release.tgz"
test -f "$upload/appki.env"
test -s /etc/letsencrypt/live/happ-ota-ip/fullchain.pem
id -u appki >/dev/null 2>&1 || useradd --system --home /var/lib/appki --shell /usr/sbin/nologin appki
install -d -o root -g root -m 755 /opt/appki /opt/appki/releases
install -d -o appki -g appki -m 700 /var/lib/appki
install -d -o root -g appki -m 750 /var/lib/appki/apps /etc/appki /var/backups/appki
install -d -o root -g root -m 755 "$target"
tar -xzf "$upload/release.tgz" -C "$target" --no-same-owner
chown -R root:root "$target"
chmod -R go-w "$target"
if [ ! -f /var/lib/appki/apps/Happ.ipa ]; then
    install -o root -g appki -m 440 /var/lib/happ-ota/Happ.ipa /var/lib/appki/apps/Happ.ipa
fi
printf '%s  %s\n' '94e9591fe92be4589c19344ab7089d0b6abe101cf9cfa7a80707d6c390a4a2f2' '/var/lib/appki/apps/Happ.ipa' | sha256sum -c -
if [ ! -x /opt/appki/venv/bin/python ]; then python3 -m venv /opt/appki/venv; fi
/opt/appki/venv/bin/python -m pip install --disable-pip-version-check -q -r "$target/backend/requirements.lock"
# Stable import path follows the current release symlink; no editable build required.
site_dir=$(/opt/appki/venv/bin/python -c 'import site; print(site.getsitepackages()[0])')
printf '%s\n' /opt/appki/current/backend > "$site_dir/appki.pth"
if [ -f /etc/appki/appki.env ]; then
    install -m 600 /etc/appki/appki.env "/var/backups/appki/env-$release"
fi
if [ -f /var/lib/appki/appki.sqlite3 ]; then
    python3 - "$release" <<'PY'
import sqlite3, sys
source = sqlite3.connect('/var/lib/appki/appki.sqlite3')
target = sqlite3.connect('/var/backups/appki/db-' + sys.argv[1] + '.sqlite3')
source.backup(target)
target.close()
source.close()
PY
    chmod 600 "/var/backups/appki/db-$release.sqlite3"
fi
install -o root -g appki -m 640 "$upload/appki.env" /etc/appki/appki.env
rm -f "$upload/appki.env"
previous=$(readlink -f /opt/appki/current || true)
ln -sfn "$target" /opt/appki/current
install -m 644 "$target/deploy/systemd/appki-api.service" /etc/systemd/system/appki-api.service
install -m 644 "$target/deploy/systemd/appki-bot.service" /etc/systemd/system/appki-bot.service
systemctl daemon-reload
systemctl enable appki-api >/dev/null
systemctl restart appki-api
healthy=false
for attempt in $(seq 1 15); do
    if curl -fsS http://127.0.0.1:18880/healthz >/dev/null; then healthy=true; break; fi
    sleep 1
done
if [ "$healthy" != true ]; then
    echo 'API healthcheck failed; restoring previous release if available.' >&2
    if [ -n "$previous" ] && [ "$previous" != /opt/appki/current ]; then
        ln -sfn "$previous" /opt/appki/current
        if [ -f "/var/backups/appki/env-$release" ]; then
            install -o root -g appki -m 640 "/var/backups/appki/env-$release" /etc/appki/appki.env
        fi
        systemctl restart appki-api
    fi
    exit 1
fi
# Only replace the vhost owned by our prior Happ installation test.
if [ -f /etc/nginx/sites-available/happ-ota ]; then
    cp -p /etc/nginx/sites-available/happ-ota "/var/backups/appki/nginx-$release.conf"
fi
install -m 644 "$target/deploy/nginx-appki.conf" /etc/nginx/sites-available/happ-ota
ln -sfn /etc/nginx/sites-available/happ-ota /etc/nginx/sites-enabled/happ-ota
if ! nginx -t; then
    if [ -f "/var/backups/appki/nginx-$release.conf" ]; then
        cp -p "/var/backups/appki/nginx-$release.conf" /etc/nginx/sites-available/happ-ota
    fi
    exit 1
fi
systemctl reload nginx
# Empty token is a supported preview state; do not run a failing background bot.
if /opt/appki/venv/bin/python - <<'PY'
from dotenv import dotenv_values
values = dotenv_values('/etc/appki/appki.env')
raise SystemExit(0 if values.get('BOT_TOKEN') else 1)
PY
then
    systemctl enable appki-bot >/dev/null
    systemctl restart appki-bot
else
    systemctl disable --now appki-bot >/dev/null 2>&1 || true
fi
curl -fsS https://85.239.144.214/healthz
printf '\nReleased %s\n' "$release"
systemctl is-active appki-api nginx
