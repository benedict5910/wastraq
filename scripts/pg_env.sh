#!/usr/bin/env bash
# Shared helper: put the right Homebrew PostgreSQL client on PATH.
# Prefers 17, then 16, then 15. Override with PG_BIN=/path/to/bin.
# Sourced by setup_macos.sh, finish_setup.sh and verify_demo.sh.

wq_resolve_pg() {
  if [ -n "${PG_BIN:-}" ] && [ -x "$PG_BIN/psql" ]; then
    export PATH="$PG_BIN:$PATH"
    return 0
  fi
  local prefix
  prefix="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
  local v
  for v in ${PG_VERSIONS:-17 16 15}; do
    if [ -x "$prefix/opt/postgresql@$v/bin/psql" ]; then
      export PATH="$prefix/opt/postgresql@$v/bin:$PATH"
      export WQ_PG_VERSION="$v"
      return 0
    fi
  done
  # Fall back to whatever is already on PATH (e.g. Postgres.app, EDB installer).
  command -v psql >/dev/null 2>&1
}

wq_pg_banner() {
  printf "psql:   %s\n" "$(command -v psql || echo 'NOT FOUND')"
  printf "server: %s\n" "$(psql -tA -d postgres -c 'SHOW server_version;' 2>/dev/null || echo 'unreachable')"
}
