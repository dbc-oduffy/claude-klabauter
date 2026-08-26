"""coordinator_core/tests/test_lfs_pre_push_hook_is_installable.py --

Guard for AC7's SECOND clause: "the disposition survives re-clone".

Spec backlink: chunk C8 of
`docs/plans/2026-08-25-push-re-homes-onto-the-cadence-surfaces.md`.
Decision record: DR-223's `pre-push` row.

WHAT THIS GUARD IS FOR, stated as the failure it prevents rather than the
behaviour it asserts. On 2026-08-25 AC7 was ticked met on the strength of its
first clause — the gate worked, and a no-op push fell from 290.6ms / 25 procs
to 93.8ms / 9. The second clause was never read. The gate lived only in
`.git/hooks/`, which is untracked per-clone state, so a fresh clone inherited
nothing and paid the full ~267ms until somebody hand-installed the file. The
sibling guard `test_no_lfs_hook_on_push_path.py` proves the gate WORKS on
whatever clone runs it; this one proves the gate ARRIVES on a clone that never
had it. Those are different claims and the first was mistaken for the second.

Negative-spec:
    - Zero spawns, and never touches the real repository. Every case runs
      `install()` against a tmp_path hooks directory. A guard for the push
      path that itself spawns git on the push path would be self-defeating,
      and this file is in the fast tier because of it.
    - Does NOT assert the hook's RUNTIME behaviour (that it skips, that it
      delegates, that it emits its decision line when actually exec'd by git).
      That is `test_no_lfs_hook_on_push_path.py`'s job and duplicating it here
      would give two files one owner.
    - Does NOT require the real `.git/hooks/pre-push` to exist or match. A
      developer clone may legitimately not have run setup yet, and a guard
      that failed on that would fire on the very state it exists to let people
      recover from. The tracked-template-is-the-source-of-truth claim is
      asserted against the template, not against local disk.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from coordinator_core.ops.install_lfs_pre_push_hook import (
    DECISION_LINE_DELEGATING,
    DECISION_LINE_SKIPPED,
    HOOK_FILENAME,
    classify_existing,
    hook_body,
    install,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_STOCK_LFS_SHIM = (
    '#!/bin/sh\ncommand -v git-lfs >/dev/null 2>&1 || { printf >&2 "..."; exit 2; }\n'
    'git lfs pre-push "$@"\n'
)


def test_installs_onto_a_clone_that_has_no_pre_push_hook(tmp_path: Path) -> None:
    """Case (a) — the re-clone case AC7's second clause is actually about."""
    hooks = tmp_path / ".git" / "hooks"
    code, message = install(hooks)

    assert code == 0, message
    installed = hooks / HOOK_FILENAME
    assert installed.is_file(), "a fresh clone must end up with the gate on disk"
    assert installed.read_text(encoding="utf-8") == hook_body()


def test_replaces_the_stock_git_lfs_shim(tmp_path: Path) -> None:
    """Case (b). The stock shim is the thing being replaced, so refusing to
    overwrite it — the pre-commit siblings' rule — would make this installer a
    no-op on every clone that has ever run `git lfs install`, which is all of
    them. This asymmetry is the correctness core of the module."""
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / HOOK_FILENAME).write_text(_STOCK_LFS_SHIM, encoding="utf-8")

    code, _ = install(hooks)

    assert code == 0
    assert (hooks / HOOK_FILENAME).read_text(encoding="utf-8") == hook_body()


def test_does_not_clobber_a_foreign_pre_push_hook(tmp_path: Path) -> None:
    """Case (c) — somebody else's work is left exactly as found, and the
    caller is not blocked."""
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    foreign = "#!/bin/sh\n# somebody's own pre-push gate\nexec ./run-my-checks.sh\n"
    (hooks / HOOK_FILENAME).write_text(foreign, encoding="utf-8")

    code, message = install(hooks)

    assert code == 0, "a foreign hook is a clean skip, never a blocked install"
    assert (hooks / HOOK_FILENAME).read_text(encoding="utf-8") == foreign
    assert "left untouched" in message


def test_upgrades_an_older_rendering_of_our_own_gate(tmp_path: Path) -> None:
    """Detection is by content marker precisely so an older version of our own
    gate is recognised as ours and upgraded, rather than read as foreign and
    skipped forever."""
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    stale = "#!/bin/sh\n# coordinator-lfs-gate (an older rendering)\nexit 0\n"
    (hooks / HOOK_FILENAME).write_text(stale, encoding="utf-8")

    assert classify_existing(stale) == "ours"
    code, _ = install(hooks)

    assert code == 0
    assert (hooks / HOOK_FILENAME).read_text(encoding="utf-8") == hook_body()


def test_install_is_idempotent(tmp_path: Path) -> None:
    hooks = tmp_path / ".git" / "hooks"
    install(hooks)
    code, message = install(hooks)

    assert code == 0
    assert "no write" in message


def test_hook_body_keeps_lf_endings() -> None:
    """git execs hooks through sh on every platform, Windows included. A CRLF
    shebang line is not a portability nicety — it breaks the hook."""
    assert "\r" not in hook_body()
    assert hook_body().startswith("#!/bin/sh\n")


def test_lf_survives_a_crlf_checkout_of_this_module(tmp_path: Path) -> None:
    """The fresh-Windows-clone case, which is precisely what AC7's second
    clause is about -- and the one way this deliverable could silently break on
    arrival.

    Committing this module produced `warning: LF will be replaced by CRLF the
    next time Git touches it`. If that CRLF reached the hook body, every
    freshly-cloned Windows box would install a hook whose `#!/bin/sh` line ends
    in CR, which does not run -- the exact clone this chunk exists to serve
    would be the one it broke, and no existing test would have said so, because
    they all read the WORKING TREE, where the file is LF. That is the same
    working-tree-not-HEAD blindness that let `push_outstanding` sit dead at HEAD
    for hours with a fully green suite.

    It survives for two independent reasons and this test pins both: Python's
    tokenizer normalises source line endings, so a CRLF-on-disk module still
    yields LF inside the string literal; and `install()` writes with
    `newline=""`, so Python does not re-translate on the way out. Either one
    silently regressing would ship a broken hook to every new clone.
    """
    source = Path(__file__).resolve().parents[1] / "ops" / "install_lfs_pre_push_hook.py"
    crlf_source = source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    as_crlf = tmp_path / "install_lfs_pre_push_hook_crlf.py"
    as_crlf.write_bytes(crlf_source)
    assert b"\r\n" in as_crlf.read_bytes(), "probe setup failed to produce a CRLF source file"

    spec = importlib.util.spec_from_file_location("_crlf_probe", str(as_crlf))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert "\r" not in module.hook_body()

    hooks = tmp_path / ".git" / "hooks"
    code, message = module.install(hooks)
    assert code == 0, message
    assert b"\r\n" not in (hooks / HOOK_FILENAME).read_bytes()


def test_hook_body_carries_the_cross_repo_decision_tokens() -> None:
    """AC7c contract: a cross-repo consumer tests these exact strings. They are
    not cosmetic and a reword is a cross-repo-visible break."""
    body = hook_body()
    assert DECISION_LINE_SKIPPED in body
    assert DECISION_LINE_DELEGATING in body


def test_predicate_never_invokes_git_lfs_to_decide() -> None:
    """The measured trap: `git lfs track` is 280.5ms, MORE than the 267.2ms
    shim it would gate. A predicate that shells out to git-lfs re-introduces
    the entire defect while looking like a simplification, so the skip arm may
    not contain a git-lfs call at all."""
    skip_arm = hook_body().split('if [ -z "$_declares_lfs" ]', 1)[1].split("fi", 1)[0]
    assert "git lfs" not in skip_arm
    assert "git-lfs" not in skip_arm


def test_setup_py_actually_calls_the_installer() -> None:
    """A registered-but-uncalled mechanism is not delivered. This plan already
    shipped one — `push.outstanding` was registered, dead at HEAD, and every
    test passed because they imported it from the working tree (AC3/AC9c).
    Re-clone durability depends on the install chain reaching this code, so the
    wiring is the thing under test, not the function's existence."""
    setup_py = (_REPO_ROOT / "scripts" / "setup.py").read_text(encoding="utf-8")

    assert "def install_lfs_pre_push_gate(" in setup_py, "installer step missing from setup.py"
    called = [
        line for line in setup_py.splitlines()
        if "install_lfs_pre_push_gate(" in line and not line.lstrip().startswith("def ")
    ]
    assert called, "install_lfs_pre_push_gate is defined but never called — present-but-dead"


def test_carve_out_register_names_this_hook_site() -> None:
    """Carve-out (b) in shell-out-carve-outs.md is ENUMERATION-CONSTITUTIVE:
    its Sites list IS the membership test, and satisfying the rationale is not
    membership. A `#!/bin/sh` generator absent from that list is a violation on
    the day it lands. The carve-out also requires every invocation to name the
    specific hook file it acts on, so `pre-push` must appear too."""
    register = (_REPO_ROOT / "docs" / "reference" / "shell-out-carve-outs.md").read_text(encoding="utf-8")
    site = "coordinator_core/ops/install_lfs_pre_push_hook.py"

    assert site in register, f"{site} generates a #!/bin/sh hook body but is not in the carve-out register"
    line = next(l for l in register.splitlines() if site in l)
    assert "pre-push" in line, "carve-out (b) requires the site to name its specific hook file"


@pytest.mark.parametrize(
    "content, expected",
    [
        (None, "absent"),
        ("#!/bin/sh\n# coordinator-lfs-gate\n", "ours"),
        (_STOCK_LFS_SHIM, "stock-lfs"),
        ("#!/bin/sh\necho hi\n", "foreign"),
    ],
)
def test_classification_of_an_existing_hook(content: str | None, expected: str) -> None:
    assert classify_existing(content) == expected


def test_foreign_hook_merely_mentioning_git_lfs_pre_push_is_not_misclassified() -> None:
    """Review: code-reviewer P1 — a hand-written foreign hook that merely
    MENTIONS the stock-lfs marker inside a comment must classify as
    `foreign`, never `stock-lfs`. A plain substring probe would misclassify
    this and `install()` would silently overwrite it."""
    foreign = "#!/bin/sh\n# do not run git lfs pre-push here\nexec ./run-my-checks.sh\n"
    assert classify_existing(foreign) == "foreign"


def test_foreign_hook_mentioning_coordinator_lfs_gate_in_prose_is_not_misclassified() -> None:
    """Same edge for the `ours` marker: a comment that mentions
    `coordinator-lfs-gate` without the line itself STARTING WITH the marker
    (e.g. documenting a decision to avoid it) must not be read as `ours`."""
    foreign = "#!/bin/sh\n# we deliberately avoid coordinator-lfs-gate here\nexec ./run-my-checks.sh\n"
    assert classify_existing(foreign) == "foreign"


def test_foreign_hook_mentioning_git_lfs_pre_push_is_not_overwritten_by_install(tmp_path: Path) -> None:
    """End-to-end guard for the same edge: `install()` must leave this
    foreign hook exactly as found, not silently overwrite it."""
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    foreign = "#!/bin/sh\n# do not run git lfs pre-push here\nexec ./run-my-checks.sh\n"
    (hooks / HOOK_FILENAME).write_text(foreign, encoding="utf-8")

    code, message = install(hooks)

    assert code == 0
    assert (hooks / HOOK_FILENAME).read_text(encoding="utf-8") == foreign
    assert "left untouched" in message


def test_our_own_current_hook_body_classifies_as_ours_not_stock_lfs() -> None:
    """Order-of-checks regression guard: our own template's delegate arm
    contains `exec git lfs pre-push "$@"`, so checking `stock-lfs` before
    `ours` would misclassify our own current hook as the stock shim."""
    assert classify_existing(hook_body()) == "ours"
