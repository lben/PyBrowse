#!/usr/bin/env bash
# Start PyBrowse (macOS and Linux). The first run creates a private Python
# environment in .venv next to this script and installs the dependencies.
set -euo pipefail

src="${BASH_SOURCE[0]}"
while [ -L "$src" ]; do  # follow symlinks, e.g. ~/bin/pybrowse -> this file
  dir="$(cd -P "$(dirname "$src")" && pwd)"
  src="$(readlink "$src")"
  [[ "$src" != /* ]] && src="$dir/$src"
done
here="$(cd -P "$(dirname "$src")" && pwd)"
venv="$here/.venv"
py="$venv/bin/python"

# (Re)install when the environment is missing, a previous install failed,
# or requirements.txt changed since the last install.
if ! cmp -s "$here/requirements.txt" "$venv/requirements.installed"; then
  if ! command -v python3 >/dev/null 2>&1 ||
     ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo "PyBrowse needs Python 3.10 or newer: https://www.python.org/downloads/" >&2
    exit 1
  fi
  echo "Setting up PyBrowse in $venv (first run only)..."
  [ -x "$py" ] || python3 -m venv "$venv"
  "$py" -m pip install --quiet --disable-pip-version-check -r "$here/requirements.txt"
  cp "$here/requirements.txt" "$venv/requirements.installed"
fi

exec "$py" "$here/main.py" "$@"
