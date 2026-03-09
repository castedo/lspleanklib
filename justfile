#!/usr/bin/env -S just --justfile

default:
    just --list

test:
    ruff check lspleanklib || true
    mypy --strict lspleanklib
    mypy tests --cache-dir tests/.mypy_cache
    pytest -vv tests --timeout=3

clean:
    rm -rf dist
    rm -rf build
    rm -rf *.egg-info
    rm -f _version.py
