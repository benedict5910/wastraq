#!/usr/bin/env bash
# =====================================================================
# Build (or repair) .venv on a Python this project's wheels exist for.
#
#   ./scripts/setup_python_env.sh              repair only if needed
#   ./scripts/setup_python_env.sh --force      rebuild unconditionally
#
# Options (environment):
#   WQ_PYTHON=/path/to/python3.11   use exactly this interpreter
#   WQ_KEEP_OLD_VENV=0              delete the old .venv instead of moving it
#
# What it does:
#   1. resolve a supported interpreter (3.11 preferred - scripts/py_env.sh),
#      installing python@3.11 via Homebrew if that is the only way
#   2. if .venv was built on a different or unsupported Python, move it aside
#      and create a fresh one (the old one is kept, never silently deleted)
#   3. install backend/requirements.txt
#   4. import every dependency the backend needs at start-up and fail loudly,
#      by name, if one is missing
#
# It touches nothing but .venv. No database, no data, no project files.
# =====================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

LOGS="$ROOT/logs"; mkdir -p "$LOGS"
STAMP="$(date +%Y%m%d-%H%M%S)"
PIP_LOG="$LOGS/pip_${STAMP}.log"

step() { printf "\n\033[1m--- %s\033[0m\n" "$*"; }
ok()   { printf "  \033[32mok\033[0m    %s\n" "$*"; }
warn() { printf "  \033[33mwarn\033[0m  %s\n" "$*"; }
die()  { printf "  \033[31mFAIL\033[0m  %s\n" "$*"; exit 1; }

# ---------------------------------------------------------------------
step "Interpreter"
# ---------------------------------------------------------------------
# shellcheck disable=SC1091
source "$ROOT/scripts/py_env.sh"
wq_resolve_python --install || die "no supported Python interpreter (see the message above)"
ok "using Python $WQ_PY_VER at $WQ_PY"

if command -v python3 >/dev/null 2>&1; then
  DEFAULT_VER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
  [ "$DEFAULT_VER" = "$WQ_PY_VER" ] \
    || warn "your default python3 is $DEFAULT_VER - the project deliberately uses $WQ_PY_VER"
fi

# ---------------------------------------------------------------------
step "Virtualenv"
# ---------------------------------------------------------------------
REBUILD=0
if [ ! -d "$ROOT/.venv" ]; then
  REBUILD=1
  ok "no .venv yet - creating one"
else
  CUR="$(wq_venv_series "$ROOT/.venv")"
  if [ -z "$CUR" ]; then
    REBUILD=1; warn ".venv exists but its python is broken - rebuilding"
  elif [ "$CUR" != "$WQ_PY_VER" ]; then
    REBUILD=1; warn ".venv was built on Python $CUR, project needs $WQ_PY_VER - rebuilding"
  elif [ "$FORCE" = "1" ]; then
    REBUILD=1; ok "--force given - rebuilding .venv on Python $CUR"
  else
    ok ".venv already on Python $CUR"
  fi
fi

if [ "$REBUILD" = "1" ] && [ -d "$ROOT/.venv" ]; then
  if [ "${WQ_KEEP_OLD_VENV:-1}" = "1" ]; then
    OLD="$ROOT/.venv.old-$STAMP"
    mv "$ROOT/.venv" "$OLD" || die "could not move the old .venv aside"
    ok "old .venv moved to $(basename "$OLD")  (delete it with: rm -rf '$OLD')"
  else
    rm -rf "$ROOT/.venv" || die "could not remove the old .venv"
    ok "old .venv removed"
  fi
fi

if [ "$REBUILD" = "1" ]; then
  "$WQ_PY" -m venv "$ROOT/.venv" || die "could not create .venv with $WQ_PY"
  ok "created .venv on Python $WQ_PY_VER"
fi

VPY="$ROOT/.venv/bin/python"
[ -x "$VPY" ] || die ".venv/bin/python missing after creation"

# ---------------------------------------------------------------------
step "Dependencies"
# ---------------------------------------------------------------------
"$VPY" -m pip install --upgrade pip setuptools wheel > "$PIP_LOG" 2>&1 \
  && ok "pip $("$VPY" -m pip --version | awk '{print $2}') ready" \
  || warn "could not upgrade pip - continuing"

echo "  installing backend/requirements.txt (log: $(basename "$PIP_LOG"))"
if "$VPY" -m pip install -r "$ROOT/backend/requirements.txt" >> "$PIP_LOG" 2>&1; then
  ok "requirements installed"
else
  echo "----- pip output -----"
  grep -iE "^ERROR|No matching distribution|Could not find" "$PIP_LOG" | tail -10
  echo "----------------------"
  die "pip install failed - full log: $PIP_LOG"
fi

# ---------------------------------------------------------------------
step "Import check"
# ---------------------------------------------------------------------
# Everything the backend imports at START-UP. A missing one here is a crash
# at boot, not a runtime 500, so it is checked before we ever try to serve.
"$VPY" - <<'PYEOF'
import importlib, sys

# (import name, pip name, what breaks without it)
NEEDED = [
    ("fastapi",      "fastapi",           "the API itself"),
    ("uvicorn",      "uvicorn[standard]", "the server"),
    ("psycopg",      "psycopg[binary]",   "every database query"),
    ("psycopg_pool", "psycopg-pool",      "the connection pool"),
    ("pydantic",     "pydantic",          "request/response models"),
    ("dotenv",       "python-dotenv",     "reading backend/.env"),
    ("requests",     "requests",          "the picker simulation"),
    ("numpy",        "numpy",             "geometry generation scripts"),
]

missing = []
for mod, pkg, why in NEEDED:
    try:
        importlib.import_module(mod)
        print(f"  \033[32mok\033[0m    {pkg}")
    except Exception as e:
        missing.append((pkg, why, e))
        print(f"  \033[31mFAIL\033[0m  {pkg} - {why} ({e})")

# python-multipart is imported by FastAPI at ROUTE BUILD time and has gone by
# two module names across releases. Accept either; this is exactly the check
# that would have caught the boot crash.
for name in ("python_multipart", "multipart"):
    try:
        importlib.import_module(name)
        print("  \033[32mok\033[0m    python-multipart")
        break
    except Exception:
        continue
else:
    missing.append(("python-multipart", "the survey photo upload route "
                    "(FastAPI refuses to START without it)", "not importable"))
    print("  \033[31mFAIL\033[0m  python-multipart")

if missing:
    print("\nMissing: " + ", ".join(p for p, _, _ in missing))
    sys.exit(1)

print(f"\n  all dependencies importable on Python "
      f"{sys.version_info.major}.{sys.version_info.minor}")
PYEOF
RC=$?
[ $RC -eq 0 ] || die "a dependency is still missing - full pip log: $PIP_LOG"

# Confirm the app module itself imports. This is the real test: it exercises
# every route definition, which is where the multipart failure actually bit.
if ( cd "$ROOT/backend" && "$VPY" -c "import app.main" ) \
     >/dev/null 2>"$LOGS/import_app_${STAMP}.log"; then
  ok "backend/app/main.py imports cleanly (all routes build)"
else
  echo "----- import error -----"; tail -20 "$LOGS/import_app_${STAMP}.log"
  die "the app itself does not import - see $LOGS/import_app_${STAMP}.log"
fi

echo
ok "Python environment ready: $ROOT/.venv (Python $WQ_PY_VER)"
