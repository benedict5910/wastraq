#!/usr/bin/env bash
# =====================================================================
# Shared backend start/stop helpers.
#
# Why this exists: `kill $PID; sleep 1; uvicorn ...` is a race. uvicorn's
# graceful shutdown regularly takes longer than a second, so the new process
# hits "[Errno 48] address already in use", dies, and the OLD process keeps
# serving - on stale code, while every readiness probe still says 200 OK.
# That is exactly what happened on 2026-08-19.
#
# So: stop by pidfile AND by port, wait for the port to actually be free,
# then start, then prove the process answering is the new one.
#
# Sourced by finish_setup.sh and load_real_lane.sh.
# =====================================================================

# Version the backend must report for us to consider it current.
#
# READ FROM THE SOURCE, never typed here. A literal drifts the moment
# backend/app/__init__.py is bumped, and the symptom - "a stale process is
# still holding the port" against a backend that is perfectly current - points
# at completely the wrong thing.
_WQ_CTL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_WQ_SRC_VERSION="$(sed -n 's/^__version__ *= *"\(.*\)"/\1/p' \
                   "$_WQ_CTL_ROOT/backend/app/__init__.py" 2>/dev/null | head -1)"
WQ_EXPECTED_VERSION="${WQ_EXPECTED_VERSION:-${_WQ_SRC_VERSION:-0.4.0}}"

wq_port_pids() {   # pids listening on $1
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:$1" -s TCP:LISTEN 2>/dev/null
  fi
}

wq_port_free() {   # 0 if $1 can actually be bound (the question that matters)
  python3 - "$1" <<'WQEOF'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
    sys.exit(0)          # free
except OSError:
    sys.exit(1)          # in use
finally:
    s.close()
WQEOF
}

wq_stop_backend() { # <port> <pidfile>
  local port="$1" pidfile="$2" pid waited

  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null
    fi
  fi
  for pid in $(wq_port_pids "$port"); do
    kill "$pid" 2>/dev/null
  done

  # Wait for the port to be released (up to ~10 s), then escalate.
  waited=0
  while ! wq_port_free "$port"; do
    sleep 0.5
    waited=$((waited + 1))
    if [ "$waited" -ge 20 ]; then
      for pid in $(wq_port_pids "$port"); do
        echo "  forcing SIGKILL on pid $pid still holding port $port"
        kill -9 "$pid" 2>/dev/null
      done
      sleep 1
      break
    fi
  done

  if wq_port_free "$port"; then
    rm -f "$pidfile"
    return 0
  fi
  echo "  could not free port $port (held by: $(wq_port_pids "$port" | tr '\n' ' '))"
  return 1
}

wq_backend_version() { # <api-base> -> version string, or empty
  curl -sf "$1/" 2>/dev/null \
    | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("version",""))
except Exception: pass' 2>/dev/null
}

wq_start_backend() { # <root> <host> <port> <logdir> [photo_dir]
  local root="$1" host="$2" port="$3" logdir="$4" photo_dir="${5:-$HOME/properties}"
  local api="http://${host}:${port}"
  local pidfile="$logdir/backend.pid" log="$logdir/backend.log"
  local uvicorn="$root/.venv/bin/uvicorn"

  [ -x "$uvicorn" ] || { echo "  no $uvicorn - run ./scripts/finish_setup.sh first"; return 1; }

  wq_stop_backend "$port" "$pidfile" || return 1

  : > "$log"
  ( cd "$root/backend" && PHOTO_DIR="$photo_dir" nohup "$uvicorn" app.main:app \
      --host "$host" --port "$port" >> "$log" 2>&1 & echo $! > "$pidfile" )

  local i
  for i in $(seq 1 60); do
    curl -sf "$api/" >/dev/null 2>&1 && break
    # Fail fast if the child already died (bind error, import error, ...)
    if ! kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null && [ "$i" -gt 4 ]; then
      break
    fi
    sleep 0.5
  done

  if ! curl -sf "$api/" >/dev/null 2>&1; then
    echo "  backend did not come up on $api"
    tail -25 "$log"
    return 1
  fi

  # Prove the process answering is the one we just started, not a survivor.
  local got
  got="$(wq_backend_version "$api")"
  if [ "$got" != "$WQ_EXPECTED_VERSION" ]; then
    echo "  a backend is answering on $api but reports version '${got:-<none>}',"
    echo "  expected '$WQ_EXPECTED_VERSION' - a stale process is still holding the port."
    echo "  listening pids: $(wq_port_pids "$port" | tr '\n' ' ')"
    tail -15 "$log"
    return 1
  fi
  return 0
}
