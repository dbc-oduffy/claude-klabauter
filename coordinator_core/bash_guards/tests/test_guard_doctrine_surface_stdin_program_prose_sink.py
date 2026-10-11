"""A stdin-program heredoc is judged by its write sinks, not by a governed
mention in prose beside a redirect-looking character."""
from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard

IDS = guard._SIZING_RECORD_IDENTIFIERS
SZ = "state/sizings/y.yaml"
PROSE = f"a <sid>/x and {SZ} then a > b"
OUT = ".coordinator-local/memo-outbox/x.md"

ALLOW_CASES = [
    (
        "python stdin heredoc, prose names governed path, sink ungoverned",
        f"python3 - <<'PY'\nopen('{OUT}','w').write('{PROSE}')\nPY",
    ),
    (
        "write_text sink ungoverned, prose governed",
        f"python3 - <<'PY'\nfrom pathlib import Path\nPath('{OUT}').write_text('{PROSE}')\nPY",
    ),
    (
        "governed name only in a literal never bound to a write",
        f"python3 - <<'PY'\nprint('see {SZ} and a > b')\nPY",
    ),
    (
        "cat redirect to ungoverned file, same body",
        f"cat > {OUT} <<'EOF'\n{PROSE}\nEOF",
    ),
]

DENY_CASES = [
    ("cat redirect to governed", f"cat > state/sizings/x.yaml <<'EOF'\nbody\nEOF"),
    ("cat heredoc then redirect to governed", f"cat <<'EOF' > state/sizings/x.yaml\nbody\nEOF"),
    (
        "stdin open w on governed",
        "python3 - <<'PY'\nopen('state/sizings/x.yaml','w').write('x')\nPY",
    ),
    (
        "stdin write_text on governed",
        "python3 - <<'PY'\nfrom pathlib import Path\nPath('state/sizings/x.yaml').write_text('x')\nPY",
    ),
    (
        "stdin variable bound to governed path reaches a write",
        "python3 - <<'PY'\np='state/sizings/x.yaml'\nopen(p,'w').write('x')\nPY",
    ),
    (
        "Path.open w with json.dump on a bound governed path",
        "python3 - <<'PY'\nimport json\nfrom pathlib import Path\np='state/sizings/x.yaml'\njson.dump({}, Path(p).open('w'))\nPY",
    ),
    (
        "governed sink beside a prose-only mention",
        f"python3 - <<'PY'\nopen('state/sizings/x.yaml','w').write('{PROSE}')\nPY",
    ),
]


@pytest.mark.parametrize("label,cmd", ALLOW_CASES, ids=[c[0] for c in ALLOW_CASES])
def test_prose_mention_with_ungoverned_sink_allows(label: str, cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, IDS) is False, label


@pytest.mark.parametrize("label,cmd", DENY_CASES, ids=[c[0] for c in DENY_CASES])
def test_governed_sink_still_denies(label: str, cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, IDS) is True, label
