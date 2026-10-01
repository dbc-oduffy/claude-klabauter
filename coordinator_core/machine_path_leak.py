"""Machine-path-leak scan for tracked settings.json content, shared by the Bash and engine commit paths.

Pure string work: no git, no filesystem, no subprocess.
"""

import json
import re
from typing import Iterator, List, Optional, Tuple

_SETTINGS_PATTERNS = [
    re.compile(r"^/Users/[^/]+/"),
    re.compile(r"^/home/[^/]+/"),
    re.compile(r"^C:[/\\]Users[/\\]"),
    re.compile(r"^X:[/\\]"),
    re.compile(r"^E:[/\\]"),
]

_TESTS_FIXTURE_SEGMENT_RE = re.compile(r"(^|/)tests/fixtures/")
_SETTINGS_JSON_RE = re.compile(r"(^|/)settings\.json$")


def _is_machine_abs(val: str) -> bool:
    return any(p.search(val) for p in _SETTINGS_PATTERNS)


def _walk_json(obj, path: str = "") -> Iterator[Tuple[str, str]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = (path + "." + k) if path else k
            yield from _walk_json(v, child)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_json(v, path + "[" + str(i) + "]")
    elif isinstance(obj, str):
        if _is_machine_abs(obj):
            yield (path, obj)


def leak_detail(rel_path: str, content: str) -> Optional[str]:
    """Violation text for machine-absolute leaf values in ``content``; ``None`` if clean.

    Unparseable JSON is reported (an ``ERROR`` line), not passed.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return "ERROR — failed to parse {} as JSON: {}".format(rel_path, e)

    lines: List[str] = []
    for leaf_path, leaf_val in _walk_json(data):
        lines.append("VIOLATION: {}: machine-specific path in JSON leaf".format(rel_path))
        lines.append("  Leaf path : {}".format(leaf_path))
        lines.append("  Value     : {}".format(leaf_val))
        lines.append(
            "  Remedy    : machine-specific paths must live in gitignored\n"
            "              settings.local.json or machine-local registry,\n"
            "              not in tracked settings.json"
        )

    if not lines:
        return None
    return "\n".join(lines)


def is_settings_json(rel_path: str) -> bool:
    """Does ``rel_path`` name a settings.json the scan covers? Fixtures included."""
    return bool(_SETTINGS_JSON_RE.search(rel_path))


def fixture_suppressible(rel_path: str, detail: str) -> bool:
    """True only for the unparseable-JSON finding under a ``tests/fixtures/`` tree.

    A real leak blocks wherever it lives; the path decides only whether a parse failure is tolerable.
    """
    if not _TESTS_FIXTURE_SEGMENT_RE.search(rel_path):
        return False
    return detail.startswith("ERROR")
