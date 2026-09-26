
from __future__ import annotations

import sys
from typing import List, Optional


def _extract(text: str, field: str) -> Optional[str]:
    prefix = f"{field}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _normalize(raw: str) -> str:
    value = raw.lstrip()

    hash_idx = value.find("#")
    if hash_idx != -1:
        value = value[:hash_idx].rstrip(" \t")

    value = value.strip()

    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    elif len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        value = value[1:-1]

    if value == "null" or value == "":
        return ""

    return value


def read_frontmatter_field(file_path: str, field: str) -> str:
    if not file_path or not field:
        return ""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        print(f"skip: read_frontmatter_field: with open(file_path, \"r\", encoding=\"utf-8\", errors=\"replace\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""

    raw = _extract(text, field)
    if raw is None:
        return ""
    return _normalize(raw)


def main(argv: List[str]) -> int:
    file_path = argv[0] if len(argv) > 0 else ""
    field = argv[1] if len(argv) > 1 else ""
    sys.stdout.write(read_frontmatter_field(file_path, field))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
