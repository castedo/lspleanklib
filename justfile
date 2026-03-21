#!/usr/bin/env -S just --justfile

default:
    just --list

test:
    ruff check lspleanklib || true
    mypy --strict lspleanklib
    mypy tests --cache-dir tests/.mypy_cache
    pytest -vv tests --timeout=2 \
#      -m 'not slow' \
#      --durations=3 \
#      --log-cli-level=DEBUG \

clean:
    rm -rf dist
    rm -rf build
    rm -rf *.egg-info
    rm -f _version.py
