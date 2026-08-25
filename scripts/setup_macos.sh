#!/usr/bin/env bash
# =====================================================================
# Wastraq demo - one-shot macOS setup.
#   ./scripts/setup_macos.sh
# Installs PostgreSQL + PostGIS via Homebrew, creates wastraq_demo,
# loads schema + seed + dummy GIS data, and builds the Python venv.
#
# Already have PostgreSQL + PostGIS installed and the database created?
# Use ./scripts/finish_setup.sh instead - it skips the install entirely and
# never drops your database.
#
# Safe to re-run. An existing database is reused, not dropped, unless you
# pass RECREATE_DB=1.
# =====================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_NAME="${DB_NAME:-wastraq_demo}"
# PostgreSQL 17 by default; override with PG_FORMULA=postgresql@16 if needed.
PG_FORMULA="${PG_FORMULA:-postgresql@17}"

say() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }

# --- 1. Homebrew ------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew not found. Install it first:"
  echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  exit 1
fi

BREW_PREFIX="$(brew --prefix)"   # Apple silicon and Intel differ

if [ -x "$BREW_PREFIX/opt/$PG_FORMULA/bin/psql" ]; then
  say "$PG_FORMULA already installed - skipping brew install"
else
  say "Installing $PG_FORMULA + PostGIS (this is the slow part)"
  brew install "$PG_FORMULA" postgis || true
  brew link --overwrite --force "$PG_FORMULA" 2>/dev/null || true
fi

export PATH="$BREW_PREFIX/opt/$PG_FORMULA/bin:$PATH"

say "Starting the PostgreSQL service"
brew services start "$PG_FORMULA" || true

# Wait for the socket to come up.
for _ in $(seq 1 30); do
  if pg_isready -q; then break; fi
  sleep 1
done
pg_isready || { echo "PostgreSQL did not start. Try: brew services restart $PG_FORMULA"; exit 1; }

# --- 2. Database ------------------------------------------------------
if psql -tA -lqt 2>/dev/null | cut -d'|' -f1 | grep -qx "$DB_NAME"; then
  if [ "${RECREATE_DB:-0}" = "1" ]; then
    say "Recreating database $DB_NAME (RECREATE_DB=1)"
    dropdb --if-exists "$DB_NAME"
    createdb "$DB_NAME"
  else
    say "Database $DB_NAME already exists - reusing it"
  fi
else
  say "Creating database $DB_NAME"
  createdb "$DB_NAME"
fi

say "Loading schema, seed data and dummy GIS geometry"
psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$ROOT/database/schema.sql"
psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$ROOT/database/seed.sql"
psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$ROOT/database/gis_dummy_data.sql"
psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$ROOT/database/lookup_function.sql"

# --- 3. Python --------------------------------------------------------
say "Creating the Python virtual environment"
PY="${PYTHON:-python3}"
"$PY" -m venv "$ROOT/.venv"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$ROOT/backend/requirements.txt"

if [ ! -f "$ROOT/backend/.env" ]; then
  cp "$ROOT/backend/.env.example" "$ROOT/backend/.env"
  echo "DB_USER=$USER" >> "$ROOT/backend/.env"
fi

say "Setup complete"
cat <<EOF

PostGIS version:
$(psql -tA -d "$DB_NAME" -c 'SELECT PostGIS_Version();')

Next (either route):
  ./scripts/finish_setup.sh         # loads SQL, starts the API, runs + verifies everything

or by hand:
  source .venv/bin/activate
  ./scripts/run_backend.sh                # terminal 1
  python3 simulation/simulate_picker.py   # terminal 2
  open http://127.0.0.1:8000/dashboard
EOF
