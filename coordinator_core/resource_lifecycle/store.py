"""BoundedStore: append-only JSONL store that refuses writes past max_bytes.

The allowance check reads the file's real size at each write, so a second
process cannot push the file past max_bytes unnoticed (the check-then-append is
not atomic across processes). There is no truncate, rotate or delete path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

from coordinator_core.resource_lifecycle.errors import AllowanceExceeded


class BoundedStore:
    """Append-only JSONL file capped at `max_bytes`; never evicts history."""

    def __init__(self, path: str | os.PathLike[str], max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        self.path = Path(path)
        self.max_bytes = max_bytes
        try:
            self._size = self.path.stat().st_size
        except FileNotFoundError:
            self._size = 0

    @property
    def size(self) -> int:
        return self._size

    def write(self, record: Any) -> None:
        """Append one record as a JSON line; raise AllowanceExceeded and write nothing if it would not fit."""
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with open(self.path, "ab") as fh:
            self._size = os.fstat(fh.fileno()).st_size
            if self._size + len(line) > self.max_bytes:
                raise AllowanceExceeded(
                    f"{self.path} holds {self._size} of {self.max_bytes} bytes; "
                    f"a {len(line)}-byte record does not fit.",
                    f"raise max_bytes for {self.path} or archive the file and open a new path; "
                    "nothing was written or evicted.",
                )
            fh.write(line)
        self._size += len(line)

    def read(self) -> Iterator[Any]:
        """Yield every stored record in write order."""
        try:
            fh = open(self.path, "rb")
        except FileNotFoundError:
            return
        with fh:
            for raw in fh:
                if raw.strip():
                    yield json.loads(raw)
