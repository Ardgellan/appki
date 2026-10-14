#!/usr/bin/env bash
# Build/install only. Does not log in, acquire licenses, download IPAs or publish apps.
set -euo pipefail
umask 077

if [[ $(id -u) != 0 ]]; then
  echo 'Run this installer as root.' >&2
  exit 1
fi
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
  echo 'This pinned installer supports Linux x86_64.' >&2
  exit 1
fi
for ipa_command in curl patch tar sha256sum runuser useradd usermod groupadd getent python3 cc; do
  command -v "$ipa_command" >/dev/null || { echo "Missing dependency: $ipa_command" >&2; exit 1; }
done

ipa_script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ipa_upstream_commit=747d66fc0acd896759f43a206a7ddfa2ab49e584
ipa_upstream_sha=64ef64b92d80ab11b7e143dc01a990b0eb6dd6c5c413243942cf4b066735ff1f
ipa_go_version=go1.27.2
ipa_go_sha=ecbadb99091a3f46e31f5f934b068b1864eafa7995211b39eaddf76996045fe5
ipa_toolchain=/opt/appki-acquirer/toolchain
ipa_worker_root=/var/lib/appki-acquirer
ipa_tool=/usr/local/libexec/appki-ipatool

getent passwd appki >/dev/null || { echo 'Deploy the appki service user first.' >&2; exit 1; }
getent group appki-artifacts >/dev/null || groupadd --system appki-artifacts
if ! getent passwd appki-acquirer >/dev/null; then
  useradd --system --user-group --home-dir "$ipa_worker_root" --shell /usr/sbin/nologin appki-acquirer
fi
if [[ $(id -gn appki-acquirer) != appki-acquirer ]]; then
  echo 'appki-acquirer must have its own primary group.' >&2
  exit 1
fi
case " $(id -nG appki-acquirer) " in
  *' appki '*)
    echo 'Remove appki-acquirer from the private appki service group before installing.' >&2
    exit 1
    ;;
esac
usermod --append --groups appki-artifacts appki
usermod --append --groups appki-artifacts appki-acquirer
install -d -m 700 -o appki-acquirer -g appki-acquirer "$ipa_worker_root"
install -d -m 700 -o appki-acquirer -g appki-acquirer "$ipa_worker_root/build-cache" "$ipa_worker_root/module-cache"
install -d -m 755 -o root -g root /opt/appki-acquirer /usr/local/libexec
if [[ -e /var/lib/appki && $(stat -c '%U' /var/lib/appki) != appki ]]; then
  echo '/var/lib/appki must belong to the appki service user.' >&2
  exit 1
fi
install -d -m 710 -o appki -g appki-artifacts /var/lib/appki
install -d -m 2750 -o appki-acquirer -g appki-artifacts /var/lib/appki/artifacts

ipa_build_dir=$(mktemp -d "$ipa_worker_root/build.XXXXXXXX")
trap 'rm -rf -- "$ipa_build_dir"' EXIT
curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
  "https://codeload.github.com/majd/ipatool/tar.gz/$ipa_upstream_commit" -o "$ipa_build_dir/ipatool.tar.gz"
echo "$ipa_upstream_sha  $ipa_build_dir/ipatool.tar.gz" | sha256sum --check --status
curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
  "https://go.dev/dl/$ipa_go_version.linux-amd64.tar.gz" -o "$ipa_build_dir/go.tar.gz"
echo "$ipa_go_sha  $ipa_build_dir/go.tar.gz" | sha256sum --check --status
install -d -m 755 -o root -g root "$ipa_toolchain"
if [[ -e "$ipa_toolchain/go" ]]; then
  rm -rf -- "$ipa_toolchain/go"
fi
tar -xzf "$ipa_build_dir/go.tar.gz" -C "$ipa_toolchain"
chmod -R a+rX "$ipa_toolchain/go"
tar -xzf "$ipa_build_dir/ipatool.tar.gz" -C "$ipa_build_dir"
ipa_source_dir="$ipa_build_dir/ipatool-$ipa_upstream_commit"
patch --batch --fuzz=0 -p1 -d "$ipa_source_dir" < "$ipa_script_dir/ipatool-appki.patch"
chown -R appki-acquirer:appki-acquirer "$ipa_build_dir"

# runuser retains the actual worker account's HOME. XDG state is supplied only
# at runtime by the Python worker, not by the build or the shared web service.
runuser -u appki-acquirer -- env \
  GOCACHE="$ipa_worker_root/build-cache" GOMODCACHE="$ipa_worker_root/module-cache" \
  CGO_ENABLED=1 GOTOOLCHAIN=local GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org \
  "$ipa_toolchain/go/bin/go" -C "$ipa_source_dir" test ./cmd -run '^TestPrivate'
runuser -u appki-acquirer -- env \
  GOCACHE="$ipa_worker_root/build-cache" GOMODCACHE="$ipa_worker_root/module-cache" \
  CGO_ENABLED=1 GOTOOLCHAIN=local GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org \
  "$ipa_toolchain/go/bin/go" -C "$ipa_source_dir" test \
  ./pkg/appstore/appki_private.go ./pkg/appstore/appki_private_test.go
runuser -u appki-acquirer -- env \
  GOCACHE="$ipa_worker_root/build-cache" GOMODCACHE="$ipa_worker_root/module-cache" \
  CGO_ENABLED=1 GOTOOLCHAIN=local GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org \
  "$ipa_toolchain/go/bin/go" -C "$ipa_source_dir" build -trimpath \
  -ldflags='-s -w -X github.com/majd/ipatool/v2/cmd.version=2.6.0-appki.1' \
  -o "$ipa_build_dir/appki-ipatool" .
install -m 755 -o root -g root "$ipa_build_dir/appki-ipatool" "$ipa_tool"
cat > "$ipa_build_dir/appki-acquire" <<'WRAPPER'
#!/bin/sh
set -eu
if [ "$(id -u)" = 0 ]; then
  exec runuser -u appki-acquirer -- /opt/appki/venv/bin/python -m appki.acquisition "$@"
fi
exec /opt/appki/venv/bin/python -m appki.acquisition "$@"
WRAPPER
install -m 755 -o root -g root "$ipa_build_dir/appki-acquire" /usr/local/bin/appki-acquire
"$ipa_tool" --version
echo 'Installed private loader. No Apple account or IPA was accessed.'
