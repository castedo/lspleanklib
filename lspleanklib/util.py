import logging
from pathlib import Path
from typing import TypeVar

T = TypeVar('T')

log = logging.getLogger(__spec__.parent)


async def awaitable(value: T) -> T:
    return value


def Path_from_uri(uri: str) -> Path:
    if hasattr(Path, 'from_uri'):
        return Path.from_uri(uri)
    else:
        # for Python < 3.13 (copied from 3.13 stdlib)
        from urllib.error import URLError
        from urllib.request import url2pathname

        try:
            path = Path(url2pathname(uri, require_scheme=True))
        except URLError as exc:
            raise ValueError(exc.reason) from None
        if not path.is_absolute():
            raise ValueError(f"URI is not absolute: {uri!r}")
        return path
