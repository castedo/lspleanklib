"""
CLI common code
"""


def version() -> str:
    try:
        from ._version import version  # type: ignore[import-not-found, import-untyped, unused-ignore]

        return str(version)
    except ImportError:
        return '0.0.0'


def split_cmd_line(
    cmd_line_args: list[str], default_extra_args: list[str] | None = None
) -> tuple[list[str], list[str]]:
    try:
        cut = cmd_line_args.index('--')
    except ValueError:
        cut = len(cmd_line_args)
    extra_args = cmd_line_args[cut + 1 :]
    if default_extra_args is None:
        default_extra_args = []
    return (cmd_line_args[:cut], extra_args or default_extra_args)
