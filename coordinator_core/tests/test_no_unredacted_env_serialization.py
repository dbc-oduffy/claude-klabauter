"""No tracked engine line may serialize the whole environment without `redact_env`.

Line scan, not AST: the live shapes sit inside code-in-a-string subprocess shims. Known limit: a
two-step dump (`env = dict(os.environ)` on one line, `json.dump(env, ...)` on another) is not a
single-line hit; only the one-line shape is guarded. Key-picking dumps
(`os.environ.get("NAME")`) are not whole-env and do not match.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("coordinator_core", "coordinator", "bin", "scripts")

# A whole-env expression after a serializing call; an `env=` between them is a subprocess env copy.
_DUMP = re.compile(
    r"(?:json\.dumps?\(|write_text\()(?:(?!\benv=).)*?"
    r"(?:dict\(os\.environ\)|\*\*os\.environ|os\.environ\.copy\(\)|os\.environ\))"
)


def is_unredacted_env_dump(line: str) -> bool:
    return bool(_DUMP.search(line) and "redact_env" not in line)


def test_matcher_reports_a_planted_raw_dump():
    planted = 'json.dump({"env": dict(os.' + 'environ)}, fh)'
    assert is_unredacted_env_dump(planted)
    assert is_unredacted_env_dump("p.write_text(json.dumps({**os." + "environ}))")


def test_matcher_passes_redacted_and_key_picks():
    assert not is_unredacted_env_dump('json.dump({"env": redact_env(os.environ)}, fh)')
    assert not is_unredacted_env_dump('json.dump({"k": os.environ.get("HOME")}, fh)')
    assert not is_unredacted_env_dump("env = {**os.environ, 'A': '1'}")
    assert not is_unredacted_env_dump('run(a, env={**os.environ, "CFG": json.dumps(cfg)})')
