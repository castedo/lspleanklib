#!/usr/bin/env -S just --justfile

default:
    just --list

test-runtime:
    tests/prep_lake_package_artifacts.sh
    pytest tests \
      -vv \
      --timeout=3 \
#      -m 'not slow' \
#      --durations=3 \
#      --log-cli-level=DEBUG \

test: && test-runtime
    ruff check lspleanklib || true
    mypy --strict lspleanklib
    mypy tests --cache-dir tests/.mypy_cache

clean:
    rm -rf dist
    rm -rf build
    rm -rf *.egg-info
    rm -f _version.py
