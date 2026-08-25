#!/usr/bin/env bash
# =====================================================================
# Find a Python interpreter this project's dependency stack actually
# supports, and put it in $WQ_PY.  Sourced, not executed:
#
#   source scripts/py_env.sh
#   wq_resolve_python || exit 1
#   "$WQ_PY" -m venv .venv
#
# Why this exists
# ---------------
# macOS ships and Homebrew upgrades `python3` out from under you. A
# machine that had 3.11 last month can be on 3.14 today, and `python3 -m
# venv` will happily build a virtualenv the scientific/database wheels do
# not exist for yet - psycopg-binary and numpy in particular publish
# wheels months behind a new CPython release. The failure then shows up
# far downstream as "No matching distribution found", or worse, as a
# half-installed venv that imports FastAPI but not python-multipart.
#
# So the interpreter is chosen deliberately, not inherited.
#
# Preference order: 3.11 (the version this project is developed and
# verified against), then 3.12, then 3.13. 3.14+ is refused by default -
# not because it is bad, but because the wheels are not there yet.
#
# Overrides:
#   WQ_PYTHON=/path/to/python3.11   use exactly this interpreter
#   WQ_ALLOW_ANY_PYTHON=1           accept whatever python3 is, and take
#                                   the consequences
# =====================================================================

WQ_PREFERRED_PY_SERIES="${WQ_PREFERRED_PY_SERIES:-3.11 3.12 3.13}"

# ---------------------------------------------------------------------
# echo the "3.11"-style series of an interpreter, or nothing if unusable
_wq_py_series() {
  [ -x "$1" ] || command -v "$1" >/dev/null 2>&1 || return 1
  "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null
}

_wq_series_ok() {  # <series>
  case " $WQ_PREFERRED_PY_SERIES " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

# All the places a given series can plausibly live on a Mac.
_wq_candidates_for() {  # <series>
  local s="$1"
  cat <<EOF
python${s}
/opt/homebrew/bin/python${s}
/opt/homebrew/opt/python@${s}/bin/python${s}
/usr/local/bin/python${s}
/usr/local/opt/python@${s}/bin/python${s}
/Library/Frameworks/Python.framework/Versions/${s}/bin/python${s}
$HOME/.pyenv/versions/${s}/bin/python3
EOF
  # pyenv installs land in versioned directories: 3.11.9, 3.11.10, ...
  if [ -d "$HOME/.pyenv/versions" ]; then
    # shellcheck disable=SC2012
    ls -d "$HOME/.pyenv/versions/${s}."*/bin/python3 2>/dev/null | sort -rV
  fi
}

_wq_first_working() {  # <series>
  local c series
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then
      series="$(_wq_py_series "$c")"
      if [ "$series" = "$1" ]; then
        command -v "$c" 2>/dev/null || printf '%s\n' "$c"
        return 0
      fi
    fi
  done < <(_wq_candidates_for "$1")
  return 1
}

# ---------------------------------------------------------------------
# wq_resolve_python [--install]
#   Sets WQ_PY, WQ_PY_VER. Returns non-zero if nothing usable was found.
#   With --install, offers to `brew install python@3.11` when Homebrew is
#   available and no supported interpreter exists.
# ---------------------------------------------------------------------
wq_resolve_python() {
  local want_install=0 s found ver
  [ "${1:-}" = "--install" ] && want_install=1

  # 1. explicit override wins, no questions asked
  if [ -n "${WQ_PYTHON:-}" ]; then
    ver="$(_wq_py_series "$WQ_PYTHON")"
    if [ -z "$ver" ]; then
      echo "  WQ_PYTHON=$WQ_PYTHON is not a working interpreter" >&2
      return 1
    fi
    WQ_PY="$WQ_PYTHON"; WQ_PY_VER="$ver"
    export WQ_PY WQ_PY_VER
    return 0
  fi

  # 2. preferred series, best first
  for s in $WQ_PREFERRED_PY_SERIES; do
    if found="$(_wq_first_working "$s")"; then
      WQ_PY="$found"; WQ_PY_VER="$s"
      export WQ_PY WQ_PY_VER
      return 0
    fi
  done

  # 3. try to install the primary one
  if [ "$want_install" = "1" ] && command -v brew >/dev/null 2>&1; then
    local primary="${WQ_PREFERRED_PY_SERIES%% *}"
    echo "  no supported Python found - installing python@${primary} with Homebrew..."
    if brew install "python@${primary}" >/dev/null 2>&1 || brew list "python@${primary}" >/dev/null 2>&1; then
      if found="$(_wq_first_working "$primary")"; then
        WQ_PY="$found"; WQ_PY_VER="$primary"
        export WQ_PY WQ_PY_VER
        return 0
      fi
    fi
    echo "  brew install python@${primary} did not produce a usable interpreter" >&2
  fi

  # 4. last resort: whatever python3 is, only if explicitly allowed
  if [ "${WQ_ALLOW_ANY_PYTHON:-0}" = "1" ] && command -v python3 >/dev/null 2>&1; then
    WQ_PY="$(command -v python3)"
    WQ_PY_VER="$(_wq_py_series "$WQ_PY")"
    export WQ_PY WQ_PY_VER
    echo "  WQ_ALLOW_ANY_PYTHON=1 - using Python $WQ_PY_VER, wheels may be missing" >&2
    return 0
  fi

  {
    echo "No supported Python interpreter found."
    echo "This project needs one of: $WQ_PREFERRED_PY_SERIES"
    if command -v python3 >/dev/null 2>&1; then
      local have newest
      have="$(_wq_py_series python3)"
      # highest preferred series, for the "yours is newer" comparison
      newest="$(printf '%s\n' $WQ_PREFERRED_PY_SERIES | sort -V | tail -1)"
      if [ -n "$have" ] && [ "$have" = "$(printf '%s\n%s\n' "$have" "$newest" | sort -V | tail -1)" ] \
         && [ "$have" != "$newest" ]; then
        echo "Your default python3 is $have, which is newer than this project supports:"
        echo "psycopg-binary and numpy do not publish wheels for it yet."
      else
        echo "Your default python3 is ${have:-unknown}, which is not one of them."
      fi
    fi
    echo
    echo "Install one with:"
    echo "    brew install python@${WQ_PREFERRED_PY_SERIES%% *}"
    echo "or point the scripts at an existing interpreter:"
    echo "    WQ_PYTHON=/path/to/python3.11 ./scripts/upgrade_dashboards.sh"
  } >&2
  return 1
}

# Report the interpreter a virtualenv was built with ("3.14"), or nothing.
wq_venv_series() {  # <venv-dir>
  [ -x "$1/bin/python" ] || return 1
  "$1/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null
}
