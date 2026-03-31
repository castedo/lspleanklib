import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypeAlias, TypeVar

T = TypeVar('T')

log = logging.getLogger(__spec__.parent)


async def awaitable(value: T) -> T:
    return value


def Path_from_uri(uri: str) -> Path:
    if hasattr(Path, 'from_uri'):
        return Path.from_uri(uri)
    else:
        # for Python < 3.13 (modified from 3.13 stdlib to work in 3.12)
        from urllib.error import URLError
        from urllib.request import url2pathname

        try:
            path = Path(url2pathname(uri.removeprefix('file:')))
        except URLError as exc:
            raise ValueError(exc.reason) from None
        if not path.is_absolute():
            raise ValueError(f"URI is not absolute: {uri!r}")
        return path


# Useful general types from Language Server Protocol Specification
LspAny: TypeAlias = None | str | int | Sequence['LspAny'] | Mapping[str, 'LspAny']
LspObject: TypeAlias = Mapping[str, LspAny]


def get_str(lobj: LspAny, key: str) -> str:
    got = lobj.get(key) if isinstance(lobj, Mapping) else None
    return got if isinstance(got, str) else ''


def get_seq(lobj: LspAny, key: str) -> Sequence[LspAny]:
    got = lobj.get(key) if isinstance(lobj, Mapping) else None
    return got if isinstance(got, Sequence) else []


def get_obj(lobj: LspAny, key: str) -> Mapping[str, LspAny]:
    got = lobj.get(key) if isinstance(lobj, Mapping) else None
    return got if isinstance(got, Mapping) else {}


def get_uri_path(lobj: LspAny, key: str) -> Path:
    uri = get_str(lobj, key)
    return Path.cwd() if not uri else Path_from_uri(uri)
