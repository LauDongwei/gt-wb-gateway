#!/usr/bin/env bash
# ============================================================
#  gt-wb-gateway resilient launcher (macOS)
#
#  Watches BOTH processes:
#    gateway    127.0.0.1:8787  -- run in foreground, restart on exit
#    dashboard  127.0.0.1:8735  -- sidecar, re-checked periodically
#
#  Why a :watch loop: the dashboard is a sidecar with no watchdog of
#  its own. An earlier version exited as soon as the gateway was
#  healthy, so nothing re-checked 8735 until the gateway itself died
#  (a dead dashboard could then stay invisible until next logon).
#  Now, with the gateway healthy, we stay resident and re-probe the
#  dashboard every 60s; when the gateway drops, :watch hands control
#  back to the main :loop that restarts it.
#
#  Probes clear the proxy vars first: shells on a machine running Clash
#  Verge export HTTP_PROXY, urllib honors it, and a probe to 127.0.0.1
#  would detour through the proxy, fail, and make the watchdog resurrect
#  a healthy process (port-conflict zombies). This is the single most
#  common cause of "the gateway died but nothing came back".
#
#  Keep this file LF-only. CRLF makes bash report
#  `bad interpreter: /bin/bash^M` and the watchdog never runs.
# ============================================================
set -u
cd "$(dirname "$0")/.." || exit 1

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[error] virtualenv not found at $PY"
  echo "[error] create it first:  uv venv --python 3.13 .venv"
  echo "[error] then:             uv pip install --python $PY -r requirements.txt"
  exit 1
fi

probe() {  # $1 = port; returns 0 when /health answers 200
  "$PY" - "$1" <<'PYEOF' >/dev/null 2>&1
import sys, urllib.request
port = sys.argv[1]
try:
    sys.exit(0 if urllib.request.urlopen(
        f"http://127.0.0.1:{port}/health", timeout=2).status == 200 else 1)
except Exception:
    sys.exit(1)
PYEOF
}

ensure_dashboard() {
  if probe 8735; then return 0; fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] dashboard down - starting sidecar on 127.0.0.1:8735"
  nohup "$PY" deploy/status_server.py --port 8735 >/tmp/gtwb-dashboard.out 2>&1 &
  disown 2>/dev/null || true
  sleep 2
}

while true; do
  ensure_dashboard

  if probe 8787; then
    # gateway healthy: stay resident, keep watching the sidecar
    sleep 60
    ensure_dashboard
    if probe 8787; then continue; fi
    echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] gateway down - back to main loop"
    continue
  fi

  "$PY" -m gtwb
  echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] gateway exited, restarting in 10s..."
  sleep 10
done
