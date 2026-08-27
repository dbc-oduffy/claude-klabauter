"""CI AST gate: every ``os.open(..., O_CREAT, ...)`` call must pass an explicit
mode argument.

``os.open`` defaults ``mode=0o777``, masked by the process umask. Under the
common umask of 022 that yields **0o755** — an executable data file. Every
other create path in the codebase yields 0o644 (``open()``/``Path.write_*``
default to 0o666 & ~umask), so the moment a writer switches to ``os.open`` for
``O_EXCL`` atomicity it silently starts producing executable files, and nothing
about the diff looks like a permissions change.

This is not cosmetic on this fleet. A tracked file with git index mode 100755
is a first-class violation class in
``coordinator_core.ops.check_posix_exec_assumptions`` (``mode_100755``), and
these writers emit *tracked* artifacts: review-trail records, PM-vouch waivers,
ceremony lock files. A mode-less ``os.open`` therefore manufactures new ratchet
debt on every ceremony run, forever, and the ratchet's own remediation advice
(fix the file) is useless because the next run recreates it. Observed
2026-07-28: 48 of DoE-claude's tracked ``state/review-trail/*.json`` records
were 100755, all of them born that way, and the growth was mistaken for a
baseline-widening incident rather than a producer defect.

The fix is always the same one token: pass the mode. ``0o644`` for data,
``0o600`` for anything holding a secret or a session identity.

Detection is deliberately narrow — only calls whose flags mention ``O_CREAT``
are checked, since a mode argument is ignored (and pointless) without it. Flags
are matched syntactically on the ``O_CREAT`` attribute name anywhere in the
flags expression, which covers the ``os.O_CREAT | os.O_EXCL | os.O_WRONLY``
shape every caller here uses. A call that builds its flags dynamically is not
matched and is not a violation by this gate's definition — no such caller
exists, and inventing an evaluator for one would trade a real check for a
speculative one.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOTS = (
    _REPO_ROOT / "coordinator_core",
    _REPO_ROOT / "coordinator" / "bin",
)


def _mentions_o_creat(node: ast.AST) -> bool:
    """True if `O_CREAT` appears anywhere in this flags expression."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "O_CREAT":
            return True
        if isinstance(sub, ast.Name) and sub.id == "O_CREAT":
            return True
    return False


def _is_os_open(func: ast.AST) -> bool:
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "open"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    )


def _violations_in(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_os_open(node.func):
            continue
        # os.open(path, flags, mode) -- mode is the third positional arg, or
        # the `mode=` keyword.
        has_mode = len(node.args) >= 3 or any(
            kw.arg == "mode" for kw in node.keywords
        )
        if has_mode:
            continue
        flags = node.args[1] if len(node.args) >= 2 else None
        if flags is None or not _mentions_o_creat(flags):
            continue
        found.append((node.lineno, ast.unparse(node)))
    return found


def test_detector_fires_on_a_planted_violation(tmp_path):
    """Self-test: a green gate that cannot detect anything is worthless.

    Mirrors test_async_handler_discipline_planted_violation.py -- proves the
    walker actually reds on the exact shape that shipped the defect, and
    stays silent on the three correct shapes it must not flag.
    """
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import os\n"
        "def bad(p):\n"
        "    return os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)\n",
        encoding="utf-8",
    )
    assert _violations_in(planted), "detector missed the planted violation"

    clean = tmp_path / "clean.py"
    clean.write_text(
        "import os\n"
        "def positional(p):\n"
        "    return os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)\n"
        "def keyword(p):\n"
        "    return os.open(str(p), os.O_CREAT | os.O_WRONLY, mode=0o600)\n"
        "def no_creat(p):\n"
        "    return os.open(str(p), os.O_RDONLY)\n",
        encoding="utf-8",
    )
    assert not _violations_in(clean), "detector flagged a correct call"


def test_os_open_with_o_creat_always_declares_mode():
    offenders: list[str] = []
    for root in _SCAN_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for lineno, snippet in _violations_in(path):
                rel = path.relative_to(_REPO_ROOT)
                offenders.append(f"  {rel}:{lineno}\n      {snippet}")

    assert not offenders, (
        "os.open(..., O_CREAT, ...) without an explicit mode defaults to "
        "0o777 & ~umask -- 0o755 under the usual umask of 022, i.e. an "
        "executable data file. These writers emit TRACKED artifacts, so each "
        "one manufactures a fresh `mode_100755` violation for the "
        "check_posix_exec_assumptions ratchet on every run, which no amount "
        "of fixing the files can clear.\n\n"
        "Fix: pass the mode -- 0o644 for data, 0o600 for secrets/identity.\n\n"
        "Offending call(s):\n" + "\n".join(offenders)
    )
