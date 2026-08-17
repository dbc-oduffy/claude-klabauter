"""Every source the installer delivers as an extension-less POSIX exec target
must carry a `#!` line.

WHY THIS GUARD EXISTS. On 2026-08-17 a from-scratch install verification found
`claude-doe` unable to launch at all: `execve` returned ENOEXEC, the shell fell
back to parsing Python as `sh`, and no new session could start. The cause was a
missing shebang in `coordinator/bin/claude-doe.py` — a line that had never
existed in that file's history (`git log -S` returns empty), so this was
latent-by-construction rather than a regression, surfacing only when the
installed copy was refreshed.

The asymmetry that hid it: most peers in `coordinator/bin/` also lack a shebang
at source, and are fine, because their installed copies are GENERATED
trampolines whose content is authored with one. `claude-doe.py` is the outlier —
it is installed by BYTE COPY (`shutil.copy2` in `maximalist`'s Step 3.5b, and
`shutil.copyfile` in `wrapper_onto_path._install_one`). Nothing injects a
shebang into a byte copy, so for that delivery shape the source file is the only
place the line can come from.

NEGATIVE SPEC — this guard deliberately does NOT assert that every file in
`coordinator/bin/` has a shebang. That would be false: the generated-trampoline
peers correctly have none at source, and a guard that flagged them would be
muted within a week. The predicate is delivery shape, not directory membership.

NEGATIVE SPEC — it does not read the live installed bin directory either. A test
that passes only on a box that happens to have been installed proves nothing on
a fresh clone, and the live copy is exactly what a refresh overwrites.

Windows is unaffected by the shebang itself (`CreateProcess` cannot exec an
extension-less shebang script regardless — see the `.cmd`/`.ps1` sibling
delivery documented in `coordinator_core.install.substrate`), so the companion
assertion here is that adding the POSIX line does not disturb that pairing.
"""
from __future__ import annotations

from pathlib import Path

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[3]

#: Sources the installer delivers by byte copy to an extension-less, exec-bit
#: POSIX path. Derived from the `wrapper_src` call sites — grep `wrapper_src`
#: under `coordinator_core/install/` before adding a row, and add one whenever a
#: new byte-copied exec target appears.
_BYTE_COPIED_POSIX_EXEC_SOURCES = (
    Path("coordinator") / "bin" / "claude-doe.py",
)


def test_byte_copied_posix_exec_sources_start_with_a_shebang():
    for relpath in _BYTE_COPIED_POSIX_EXEC_SOURCES:
        src = _CLAUDE_KLABAUTER_ROOT / relpath
        assert src.is_file(), f"declared byte-copied exec source is missing: {relpath}"
        first_line = src.read_text(encoding="utf-8").splitlines()[0]
        assert first_line.startswith("#!"), (
            f"{relpath} is installed as an extension-less POSIX exec target but its first "
            f"line is {first_line!r}. Without a shebang execve returns ENOEXEC and the "
            f"shell parses it as sh."
        )
        assert "python3" in first_line, (
            f"{relpath} shebang does not name python3: {first_line!r}"
        )


def test_the_wrapper_source_maximalist_installs_is_the_one_this_guard_checks():
    """Pin the guard to the installer's own path derivation.

    A guard listing a path the installer no longer uses is the same vacuous pass
    this whole family exists to prevent, so assert the two agree rather than
    trusting the constant above to stay current."""
    source = (_CLAUDE_KLABAUTER_ROOT / "coordinator_core" / "install" / "maximalist.py").read_text(
        encoding="utf-8"
    )
    assert 'os.path.join(claude_klabauter_root, "coordinator", "bin", "claude-doe.py")' in source, (
        "maximalist's Step 3.5b no longer derives the claude-doe wrapper source the way "
        "this guard assumes — reconcile _BYTE_COPIED_POSIX_EXEC_SOURCES with the new "
        "derivation."
    )


def test_windows_sibling_delivery_is_untouched_by_the_posix_shebang():
    """The POSIX fix must not be mistaken for a whole-platform fix.

    Windows cannot exec an extension-less shebang script at all; it is served by
    generated `.cmd`/`.ps1` siblings. Assert that delivery leg still exists, so a
    future change that "simplifies" it away has to fail here rather than silently
    leaving Windows with only a file its exec loader cannot launch."""
    source = (_CLAUDE_KLABAUTER_ROOT / "coordinator_core" / "install" / "maximalist.py").read_text(
        encoding="utf-8"
    )
    assert "gen-claude-doe-launcher" in source, (
        "the Windows claude-doe launcher generation leg is gone — Windows would be left "
        "with an extension-less POSIX target CreateProcess cannot exec (WinError 193)."
    )
