#!/usr/bin/env bash
# =====================================================================
# Add RealSense picker tracking (perception phase 1).
#
#   ./scripts/add_realsense_picker_tracking.sh
#
# Options (environment):
#   SKIP_BACKEND=1   don't stop/start the backend
#   SKIP_TESTS=1     don't run the offline test suite
#   SKIP_MODEL=1     don't pre-download the YOLO weights
#   DRY_RUN=1        report only - install nothing, change nothing
#   API=http://127.0.0.1:8000
#
# What it does:
#   1. check the pinned interpreter and the existing .venv
#   2. install backend/requirements-vision.txt into that .venv
#   3. verify pyrealsense2 honestly (Apple Silicon has no PyPI wheel)
#   4. pre-fetch the YOLO weights into models/ so nothing downloads mid-demo
#   5. run scripts/test_vision_logic.py (no camera needed)
#   6. restart the backend and check /health/vision and /vision/status
#
# What it does NOT do:
#   * touch PostgreSQL. No schema, no seed, no migration, no backup needed -
#     this phase persists nothing. The 16-property pilot lane, its geometry
#     and its verification states are not read or written by any of it.
#   * start the camera. That is the picker-tracking page's job.
#   * modify backend/.env.
#
# Safe to re-run. Every step is idempotent.
# =====================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
API="${API:-http://${API_HOST}:${API_PORT}}"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
MODEL_DIR="${VISION_MODEL_DIR:-$ROOT/models}"
MODEL_NAME="${VISION_MODEL:-yolov8n.pt}"

LOGS="$ROOT/logs"; mkdir -p "$LOGS"
STAMP="$(date +%Y%m%d-%H%M%S)"
MAIN_LOG="$LOGS/add_realsense_${STAMP}.log"
exec > >(tee "$MAIN_LOG") 2>&1

step() { printf "\n\033[1m=== %s\033[0m\n" "$*"; }
ok()   { printf "  \033[32mok\033[0m    %s\n" "$*"; }
warn() { printf "  \033[33mwarn\033[0m  %s\n" "$*"; }
info() { printf "  \033[2m      %s\033[0m\n" "$*"; }
die()  { printf "  \033[31mFAIL\033[0m  %s\n" "$*"; printf "\nStopped. Log: %s\n" "$MAIN_LOG"; exit 1; }

RS_OK=0
RS_NOTE=""
WARNINGS=0

echo "Wastraq - add RealSense picker tracking (perception phase 1)"
echo "project  : $ROOT"
echo "platform : $(uname -s) $(uname -m)"
echo "started  : $(date)"
[ "${DRY_RUN:-0}" = "1" ] && echo "mode     : DRY RUN - nothing will be installed or changed"

# ---------------------------------------------------------------------
step "1/6  Python environment"
# ---------------------------------------------------------------------
[ -x "$PY" ] || die ".venv not found - run ./scripts/setup_python_env.sh first"
PYVER="$("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"
ok "interpreter: $PY  ($PYVER)"

case "$PYVER" in
  3.11.*|3.12.*|3.13.*) ok "supported series for the vision stack" ;;
  *) warn "torch/ultralytics wheels are patchy on $PYVER."
     warn "The project pins 3.11 - see scripts/py_env.sh."
     WARNINGS=$((WARNINGS+1)) ;;
esac

for f in backend/requirements-vision.txt backend/app/vision/pipeline.py \
         backend/app/static/picker-tracking.html scripts/test_vision_logic.py; do
  [ -f "$ROOT/$f" ] || die "missing $f - the code changes were not applied"
done
ok "vision source files present"

# ---------------------------------------------------------------------
step "2/6  Vision dependencies"
# ---------------------------------------------------------------------
# Installed into the SAME .venv, from a SEPARATE requirements file. It is
# separate precisely so a wheel that does not exist for this platform can
# never break the core backend install.
if [ "${DRY_RUN:-0}" = "1" ]; then
  warn "DRY_RUN - skipping pip install"
  "$PIP" install --dry-run -r backend/requirements-vision.txt >/dev/null 2>&1 \
    && ok "requirements-vision.txt resolves" \
    || warn "requirements-vision.txt does NOT resolve on this platform"
else
  echo "  installing backend/requirements-vision.txt (this pulls torch - it is large)"
  if "$PIP" install -r backend/requirements-vision.txt 2>&1 | tail -12; then
    ok "pip install finished"
  else
    warn "pip reported a problem - see above and $MAIN_LOG"
    WARNINGS=$((WARNINGS+1))
  fi
fi

for mod in ultralytics cv2 numpy; do
  if "$PY" -c "import $mod" >/dev/null 2>&1; then
    V="$("$PY" -c "import $mod;print(getattr($mod,'__version__','?'))" 2>/dev/null)"
    ok "$mod importable ($V)"
  else
    warn "$mod NOT importable - the detector will not load"
    WARNINGS=$((WARNINGS+1))
  fi
done

# ---------------------------------------------------------------------
step "3/6  Intel RealSense SDK"
# ---------------------------------------------------------------------
if "$PY" -c "import pyrealsense2" >/dev/null 2>&1; then
  RSV="$("$PY" -c "import pyrealsense2 as rs;print(getattr(rs,'__version__','?'))" 2>/dev/null)"
  ok "pyrealsense2 importable ($RSV)"
  RS_OK=1
  if "$PY" - <<'PYEOF' 2>/dev/null
import sys
import pyrealsense2 as rs
devs = list(rs.context().query_devices())
if not devs:
    sys.exit(3)
d = devs[0]
print("  %s  serial %s  fw %s" % (
    d.get_info(rs.camera_info.name),
    d.get_info(rs.camera_info.serial_number),
    d.get_info(rs.camera_info.firmware_version)))
PYEOF
  then
    ok "a RealSense device is connected (above)"
  else
    warn "pyrealsense2 works but no camera is plugged in right now"
    info "that is fine for install; plug it in before the demo"
  fi
else
  RS_OK=0
  warn "pyrealsense2 is NOT importable"
  if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
    RS_NOTE="apple-silicon"
    cat <<'EOS'

  ------------------------------------------------------------------
  This is expected on an Apple Silicon Mac and it is NOT hidden.
  Intel publishes no macOS arm64 wheel for pyrealsense2, so
  `pip install pyrealsense2` cannot work here. librealsense has to be
  built with its Python bindings against THIS .venv:

    brew install cmake libusb pkg-config
    git clone --depth 1 https://github.com/IntelRealSense/librealsense.git \
      ~/librealsense && cd ~/librealsense && mkdir -p build && cd build
    cmake .. \
      -DBUILD_PYTHON_BINDINGS=bool:true \
      -DPYTHON_EXECUTABLE="$PWD/../../.venv/bin/python" \
      -DBUILD_EXAMPLES=false -DBUILD_GRAPHICAL_EXAMPLES=false \
      -DCMAKE_BUILD_TYPE=Release
    make -j"$(sysctl -n hw.ncpu)"
    cp wrappers/python/pyrealsense2*.so \
       "$(ls -d ../../.venv/lib/python3.*/site-packages)/"

  Then re-run this script. Everything else installed above works
  regardless; the backend, the page and the API all come up and simply
  report camera_connected=false until the SDK is present.
  ------------------------------------------------------------------

EOS
  else
    RS_NOTE="not-installed"
    info "try: $PIP install pyrealsense2"
  fi
  WARNINGS=$((WARNINGS+1))
fi

# ---------------------------------------------------------------------
step "4/6  Detector weights"
# ---------------------------------------------------------------------
if [ "${SKIP_MODEL:-0}" = "1" ] || [ "${DRY_RUN:-0}" = "1" ]; then
  warn "skipping the weights download"
elif [ -f "$MODEL_DIR/$MODEL_NAME" ]; then
  ok "weights already present: $MODEL_DIR/$MODEL_NAME"
else
  mkdir -p "$MODEL_DIR"
  echo "  fetching $MODEL_NAME into models/ (once, so nothing downloads mid-demo)"
  if ( cd "$MODEL_DIR" && "$PY" -c "
from ultralytics import YOLO
YOLO('$MODEL_NAME')
" ) >/dev/null 2>&1 && [ -f "$MODEL_DIR/$MODEL_NAME" ]; then
    ok "weights fetched: $MODEL_DIR/$MODEL_NAME"
  else
    warn "could not fetch the weights (offline?). Ultralytics will try again"
    warn "on first use, which will stall the first demo frame."
    WARNINGS=$((WARNINGS+1))
  fi
fi

# ---------------------------------------------------------------------
step "5/6  Offline tests (no camera required)"
# ---------------------------------------------------------------------
TRC=0
if [ "${SKIP_TESTS:-0}" = "1" ]; then
  warn "SKIP_TESTS=1"
else
  "$PY" "$ROOT/scripts/test_vision_logic.py" | tail -30
  TRC=${PIPESTATUS[0]}
  [ $TRC -eq 0 ] && ok "vision logic tests passed" || die "vision logic tests FAILED"

  # the pre-existing offline test must still pass - this phase must not have
  # moved anything the property system depends on
  "$PY" "$ROOT/scripts/test_lookup_logic.py" >/dev/null 2>&1 \
    && ok "existing GIS lookup tests still pass" \
    || die "scripts/test_lookup_logic.py now FAILS - roll back, see backups/"
fi

# ---------------------------------------------------------------------
step "6/6  Backend"
# ---------------------------------------------------------------------
if [ "${SKIP_BACKEND:-0}" = "1" ] || [ "${DRY_RUN:-0}" = "1" ]; then
  warn "not restarting the backend"
else
  # shellcheck disable=SC1091
  source "$ROOT/scripts/backend_ctl.sh"
  wq_stop_backend "$API_PORT" "$LOGS/backend.pid" >/dev/null 2>&1
  if wq_start_backend "$ROOT" "$API_HOST" "$API_PORT" "$LOGS"; then
    ok "backend restarted on $API (version $WQ_EXPECTED_VERSION)"
  else
    die "backend did not come up - see $LOGS/backend.log"
  fi

  V="$(curl -fsS "$API/health/vision" 2>/dev/null)"
  if [ -n "$V" ]; then
    ok "/health/vision answers"
    echo "$V" | "$PY" -c "
import json,sys
d=json.load(sys.stdin)
print('        state=%s camera=%s detector=%s missing=%s'
      % (d.get('state'), d.get('camera_connected'),
         d.get('detector_loaded'), d.get('missing_dependencies')))
" 2>/dev/null
  else
    warn "/health/vision did not answer"
    WARNINGS=$((WARNINGS+1))
  fi

  curl -fsS "$API/vision/status" >/dev/null 2>&1 \
    && ok "/vision/status answers with the camera stopped (it must never 500)" \
    || { warn "/vision/status did not answer"; WARNINGS=$((WARNINGS+1)); }

  curl -fsS "$API/summary" >/dev/null 2>&1 \
    && ok "the existing operations dashboard API still answers" \
    || { warn "/summary did not answer - check the database is running"
         WARNINGS=$((WARNINGS+1)); }
fi

echo
echo "======================================================================"
if [ $WARNINGS -eq 0 ]; then
  printf "\033[32mREALSENSE PICKER TRACKING INSTALLED\033[0m\n"
else
  printf "\033[33mINSTALLED WITH %d WARNING(S) - read them above\033[0m\n" "$WARNINGS"
fi
echo "======================================================================"
if [ $RS_OK -eq 1 ]; then
  echo "RealSense SDK        : present"
else
  echo "RealSense SDK        : MISSING ($RS_NOTE) - software is installed and"
  echo "                       tested, the camera is NOT verified. docs/VISION.md"
fi
echo
echo "Live demo page       : $API/picker-tracking"
echo "Status JSON          : $API/vision/status"
echo "Live tracks JSON     : $API/vision/tracks"
echo "Annotated feed       : $API/vision/stream.mjpeg"
echo "API docs             : $API/docs"
echo
echo "Run the backend      : ./scripts/run_backend.sh"
echo "Offline tests        : .venv/bin/python scripts/test_vision_logic.py"
echo "Hardware test        : .venv/bin/python scripts/test_realsense_picker_tracking.py"
echo "Hardware test + view : .venv/bin/python scripts/test_realsense_picker_tracking.py --view"
echo
echo "Nothing in PostgreSQL was read or written by this script."
echo "Rollback             : see backups/ (the pre-change copies of every"
echo "                       modified file) and docs/VISION.md"
echo "Logs                 : $MAIN_LOG"
echo "finished             : $(date)"
exit 0
