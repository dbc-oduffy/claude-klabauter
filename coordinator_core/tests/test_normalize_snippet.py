from __future__ import annotations

import os
import random
import shutil
import string
import subprocess

import pytest

from coordinator_core.text.normalize_snippet import normalize_snippet
from coordinator_core.testing.doe_root import resolve_doe_root
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def test_a_leading_blank_lines_stripped():
    assert normalize_snippet("\n\nhello\nworld") == "hello\nworld"


def test_b_trailing_blank_lines_stripped():
    assert normalize_snippet("hello\nworld") == "hello\nworld"


def test_c_interior_blank_lines_preserved():
    assert (
        normalize_snippet("first\n\nsecond\n\nthird")
        == "first\n\nsecond\n\nthird"
    )


def test_d_all_blank_input_to_empty_output():
    assert normalize_snippet("") == ""


def test_e_single_content_line_unchanged():
    assert normalize_snippet("hello") == "hello"


def test_f_whitespace_only_lines_treated_as_blank():
    assert (
        normalize_snippet("   \nhello\n   \nworld\n   ")
        == "hello\n\nworld"
    )


def test_g_clean_content_unchanged():
    assert normalize_snippet("alpha\nbeta\ngamma") == "alpha\nbeta\ngamma"


def _find_doe_normalize_lib() -> str | None:
    # layers the CLAUDE_KLABAUTER_TEST_DOE_ROOT override on top) rather than a
    root = resolve_doe_root()
    if not root:
        return None
    candidate = os.path.join(root, "coordinator", "lib", "normalize-snippet.sh")
    return candidate if os.path.isfile(candidate) else None


def _bash_normalize(lib_path: str, text: str) -> str:
    proc = subprocess.run(
        ["bash", "-c", 'source "$1" && printf %s "$(normalize "$2")"', "--", lib_path, text],
        capture_output=True,
        text=True,
        timeout=5,
    **no_console_creationflags())
    return proc.stdout


_FUZZ_ALPHABET = string.ascii_letters + string.digits + " \t"


def _random_line(rng: random.Random) -> str:
    kind = rng.choice(["content", "blank", "whitespace-only"])
    if kind == "blank":
        return ""
    if kind == "whitespace-only":
        return "".join(rng.choice(" \t") for _ in range(rng.randint(1, 4)))
    return "".join(rng.choice(_FUZZ_ALPHABET) for _ in range(rng.randint(1, 12)))


def _random_snippet(rng: random.Random) -> str:
    n_lines = rng.randint(0, 6)
    return "\n".join(_random_line(rng) for _ in range(n_lines))


@pytest.mark.skipif(
    shutil.which("bash") is None or _find_doe_normalize_lib() is None,
    reason="bash or DoE sibling repo (coordinator/lib/normalize-snippet.sh) not available",
)
def test_differential_fuzz_matches_bash_oracle():
    lib_path = _find_doe_normalize_lib()
    assert lib_path is not None
    rng = random.Random(20260716)
    for _ in range(200):
        sample = _random_snippet(rng)
        expected = _bash_normalize(lib_path, sample)
        actual = normalize_snippet(sample)
        assert actual == expected, f"mismatch on {sample!r}: bash={expected!r} python={actual!r}"
