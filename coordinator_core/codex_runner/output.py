"""One bounded UTF-8 output reader shared by worker/persona result assembly."""
from pathlib import Path

OUTPUT_LIMIT_BYTES = 1_048_576


def read_output(path: Path) -> tuple[str, dict]:
    with path.open("rb") as stream:
        data = stream.read(OUTPUT_LIMIT_BYTES + 1)
    # Provider stdout is written as UTF-8 by the shared text supervisor. Ignore
    # only a partial multibyte character at the byte boundary, never exceed it.
    text = data[:OUTPUT_LIMIT_BYTES].decode("utf-8", errors="ignore")
    return text, {"output_truncated": len(data) > OUTPUT_LIMIT_BYTES,
                  "output_limit_bytes": OUTPUT_LIMIT_BYTES,
                  "output_bytes": len(text.encode("utf-8"))}
