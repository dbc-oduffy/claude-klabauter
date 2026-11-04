"""Log tracebacks without opening source files.

`logging.Formatter.formatException` runs `traceback.print_exception`, whose `linecache` lookup opens every
frame's `.py` file; under a scan-on-open filter that open blocks an op thread. `install` swaps in a formatter
that prints frames from `walk_tb` alone, so an error path opens no source file.
"""

import logging
import traceback

_INSTALLED = False


def format_exception_without_source(exc_info) -> str:
    """`Traceback` frames as ``File, line, name`` only (no source text), then the exception line."""
    exc_type, exc, tb = exc_info
    lines = ["Traceback (most recent call last):"]
    for frame, lineno in traceback.walk_tb(tb):
        code = frame.f_code
        lines.append(f'  File "{code.co_filename}", line {lineno}, in {code.co_name}')
    lines.extend(part.rstrip("\n") for part in traceback.format_exception_only(exc_type, exc))
    return "\n".join(lines)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    logging.Formatter.formatException = lambda self, ei: format_exception_without_source(ei)  # type: ignore[method-assign]
