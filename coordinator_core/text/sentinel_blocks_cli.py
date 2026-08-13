"""sentinel_blocks_cli — byte-parity port of coordinator/bin/lib/sentinel-blocks-cli.js.

Thin CLI wrapper around sentinel_blocks (extract_block) for use in shell
scripts.

Port disposition: this module has NO coordinator-claude-side trampoline. The coordinator-claude `.js` CLI
wrapper (coordinator/bin/lib/sentinel-blocks-cli.js) is invoked exclusively
as `node <path> extract <file> <begin> <end>` (hardcoded `node`, never `bash`
or direct-exec) — there is no dual-exec path for a sh/python polyglot
trampoline to hook into (node
does not execute a `''''exec ...''' ` bash/python polyglot line as valid
JavaScript). Per the precedent set by the underlying library port
(coordinator_core/text/sentinel_blocks.py docstring), the `.js` file and its
CLI wrapper are left untouched; this module exists so a future python-native
caller of the extract-block CLI contract can import it directly, mirroring
the JS CLI's argv/stdout/exit-code contract exactly.

Spec backlink: archive/specs/2026-05-01-portable-ideas-from-obsidian-research.md
§W2 (Sentinel-Block Primitives)

CLI contract (byte-parity with sentinel-blocks-cli.js):
    extract <file> <begin-marker> <end-marker>
        Prints the block content between markers (exclusive — marker lines
        not included) to stdout. Exits 1 if either marker is absent, or if
        the file cannot be read. Exits 1 with a usage message if invoked
        with no command, an unknown command, or missing `extract` args.
"""
from __future__ import annotations

import sys
from typing import List

from coordinator_core.text.sentinel_blocks import extract_block


def _usage() -> None:
    print(
        "Usage: sentinel-blocks-cli.js extract <file> <begin-marker> <end-marker>",
        file=sys.stderr,
    )


def main(argv: List[str]) -> int:
    if not argv:
        _usage()
        return 1

    command = argv[0]
    rest = argv[1:]

    if command == "extract":
        if len(rest) < 3:
            _usage()
            return 1
        file, begin_marker, end_marker = rest[0], rest[1], rest[2]

        try:
            with open(file, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as exc:
            print(
                f"sentinel-blocks-cli: cannot read file: {file}: {exc}",
                file=sys.stderr,
            )
            return 1

        result = extract_block(content, begin_marker, end_marker)
        if result is None:
            print(
                f"sentinel-blocks-cli: markers not found in {file}\n"
                f"  begin: {begin_marker}\n"
                f"  end:   {end_marker}",
                file=sys.stderr,
            )
            return 1

        sys.stdout.write(result["block"])
        return 0

    print(f"sentinel-blocks-cli: unknown command: {command}", file=sys.stderr)
    _usage()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
