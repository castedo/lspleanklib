#!/usr/bin/bash
set -o errexit -o nounset

THIS=$(realpath $(dirname "$0"))

if [[ ! -e $THIS/cases/min_import/.lake || ! -e $THIS/cases/alt_import/.lake ]]; then
  echo Building lake workspace build artifacts ...
  pytest $THIS --timeout=10 -k test_workspace_symbol_search > /dev/null || true
  echo ... done.
fi
