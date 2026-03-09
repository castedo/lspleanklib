import sys


def log(msg: str | BaseException) -> None:
    if isinstance(msg, BaseException):
        msg = repr(msg)
    print(msg, file=sys.stderr)
