"""
coordinator/lib/resolve-claude-klabauter/tests/test_resolves_from_published_mirror.py

Chunk C2 (docs/plans/2026-08-16-one-engine-for-the-whole-box.md): proves that
a process whose LIVE-TREE rungs are unreachable — no ``CLAUDE_KLABAUTER_ROOT`` env var,
no resolvable ``repos.claude_klabauter`` registry key, no ``.claude-klabauter-live-root``
sentinel — can still resolve and EXECUTE a real op sourced entirely from the
published engine mirror (``repos.claude_klabauter``).

"The files are published, therefore it resolves" is exactly the claim this
row exists to refuse. Pointing ``CLAUDE_KLABAUTER_ROOT``/``.claude-klabauter-live-root`` AT the mirror
would prove resolution but not unreachability — Rung 1 short-circuits before
any gate runs. This test instead builds a synthetic settings-home whose
registry carries ONLY ``repos.claude_klabauter`` (no ``claude_klabauter`` key,
no ``.claude-klabauter-live-root`` file), points ``MACHINE_LOCAL_REGISTRY_DIR`` at it, and
clears ``CLAUDE_KLABAUTER_ROOT``/``CLAUDE_PROJECT_DIR`` from the child env — so rungs
1/1.5/2 all genuinely miss and only the published rung can answer. It then
loads the MIRROR's own ``coordinator_core/claude_klabauter_root.py`` (never
this repo's ``engine_root.py``) by path in a subprocess, asserts it resolves
class ``resolved-engine`` at the mirror root, and execs a real,
side-effect-free op (``coordinator/bin/archive-stamp-cli.py --help``) FROM
the mirror's own ``coordinator/bin/`` — confirming the partial-checkout
guard does not fire and the op produces its normal (exit-0, non-empty
stdout) output.

IMPORTANT — the publish pipeline REDACTS the "claude-klabauter" codename as it syncs.
Source-side names never appear in the mirror:

- ``coordinator_core/engine_root.py`` publishes as
  ``coordinator_core/claude_klabauter_root.py`` (function
  ``coordinator_engine_root_with_class`` publishes as
  ``coordinator_claude_klabauter_root_with_class``).
- ``coordinator/lib/resolve-claude-klabauter/`` publishes as
  ``coordinator/lib/resolve-claude-klabauter/``, and inside it
  ``_resolve_claude_klabauter.py`` publishes as ``_resolve_claude_klabauter.py``.

A prior version of this test checked for the SOURCE-side names inside the
mirror and for ``coordinator/bin/publish-resolve-target.py`` (a publish-side
tool, deliberately never allowlisted into the mirror) — both conditions can
never be true post-publish, which pinned both tests below to SKIP forever.
Do not reintroduce either check.

Negative-spec: does NOT hardcode the mirror's absolute path anywhere in this
file (AC12) — resolved at test-collection time from the live
``repos.claude_klabauter`` registry entry via the shim's own
``_resolve_published_engine`` reader, same as any other caller.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C2
Regression-pin, not a Tier-T gate: the mirror this test exercises is a
machine-local artifact (``repos.claude_klabauter``), absent on a clean box
and in any peer's checkout — every test below SKIPS cleanly (never fails)
when the mirror is unregistered or incomplete, rather than becoming
skipping-test theatre that always reports green without ever running.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import pytest

# Both cases spawn a real interpreter to resolve from the published mirror
# under genuinely unreachable live-tree rungs -- an in-process resolution
# would prove nothing, since the rungs under test are the ones a same-process
# import has already satisfied. The spawn is the point, so the file is tiered
# onto the cadence suite rather than the fast one.
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_SHIM_PATH = _REPO_ROOT / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"


def _load_local_shim():
    """Load THIS repo's shim by path — used only to discover the currently
    registered mirror path; never to resolve FROM it (that happens inside
    the subprocess, against the mirror's own copy)."""
    spec = importlib.util.spec_from_file_location("_c2_local_resolve_claude_klabauter_shim", _SHIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registered_mirror_root() -> Optional[str]:
    """The live ``repos.claude_klabauter`` mirror path on THIS box, or
    ``None`` if unregistered/unusable. Never hardcoded — read fresh off the
    real machine-local registry every call."""
    shim = _load_local_shim()
    return shim._resolve_published_engine(shim._ml_dir())


def _mirror_carries_required_files(mirror_root: str) -> bool:
    """Checks the mirror's REAL published (redacted) shape — never the
    source-side "claude-klabauter" names, which the publish pipeline renames away."""
    root = Path(mirror_root)
    return (
        (root / "coordinator_core" / "claude_klabauter_root.py").is_file()
        and (
            root / "coordinator" / "lib" / "resolve-claude-klabauter"
            / "_resolve_claude_klabauter.py"
        ).is_file()
        and (root / "coordinator" / "bin" / "archive-stamp-cli.py").is_file()
    )


def _skip_reason(mirror_root: Optional[str]) -> Optional[str]:
    if mirror_root is None:
        return "no published engine mirror registered (repos.claude_klabauter) on this box"
    if not _mirror_carries_required_files(mirror_root):
        return (
            f"published mirror at '{mirror_root}' is missing "
            "coordinator_core/claude_klabauter_root.py, the redacted "
            "resolve-claude-klabauter shim, or coordinator/bin/archive-stamp-cli.py — a "
            "publish round is needed: run `python coordinator/bin/publish.py "
            "claude-klabauter` (or the matching lib-target row) from claude-klabauter "
            "before this pin can execute"
        )
    return None


_MIRROR_ROOT = _registered_mirror_root()
_SKIP_REASON = _skip_reason(_MIRROR_ROOT)


def _mirror_root() -> str:
    """`_MIRROR_ROOT` narrowed to `str` for the bodies below.

    Every caller sits behind `skipif(_SKIP_REASON is not None)`, and a `None`
    root is one of the conditions that produces a skip reason — so this can
    only fire if that guard is removed, which is exactly when a caller should
    hear about it rather than fail later on a `None` path argument."""
    assert _MIRROR_ROOT is not None, "guarded by _SKIP_REASON; see _skip_reason()"
    return _MIRROR_ROOT

_CHILD_SCRIPT = r"""
import importlib.util
import sys
from pathlib import Path

mirror_root = Path(sys.argv[1])
root_module_path = mirror_root / "coordinator_core" / "claude_klabauter_root.py"

spec = importlib.util.spec_from_file_location("_c2_mirror_claude_klabauter_root", root_module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

root, resolution_class = module.coordinator_claude_klabauter_root_with_class()
print("RESOLVED_ROOT=" + root)
print("RESOLVED_CLASS=" + resolution_class)
"""


@pytest.mark.skipif(_SKIP_REASON is not None, reason=str(_SKIP_REASON))
def test_repointed_resolution_resolves_class_from_mirror_only():
    """The mirror's OWN ``claude_klabauter_root.py`` classifies its answer
    ``resolved-engine`` when the DR-132 gate confirms the running session is
    NOT itself a registered engine-working-repo, proving the published rung
    (not an accidental live-tree hit) answered.

    IMPORTANT — ``claude_klabauter_root.py`` is a genuinely different module
    from source-side ``engine_root.py``, not a straight redacted rename of
    it: ``engine_root.py`` disambiguates a distinct "am I the live tree"
    rung (``repos.claude_klabauter`` / ``CLAUDE_KLABAUTER_ROOT``) from a distinct
    "published mirror" rung (``repos.claude_klabauter``). This module is
    self-referential — it resolves the identity of the repo it ships
    inside — and has only ONE registry key (``repos.claude_klabauter``) for
    both purposes; whether that key's answer counts as "resolved-engine" vs
    "live-working-tree" is instead decided by the DR-132
    ``engine.working_repos.*`` gate (``_is_engine_working_repo``), not by a
    second, CLAUDE_KLABAUTER_ROOT-style key. Do NOT "fix" this by trying to make
    ``repos.claude_klabauter`` absent-yet-present — that key must stay set
    for either class to resolve at all. The unreachability this row proves
    is: the running session is confirmed NOT the registered working repo
    (``CLAUDE_PROJECT_DIR`` set to a directory that is registered as a
    working repo's sibling, never the working repo itself), so step 1 of
    ``resolve_claude_klabauter_root_with_class`` fires the published branch
    rather than falling through to the live-tree ladder."""
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        settings_home = scratch / "settings-home"
        ml_dir = settings_home / "machine-local"
        ml_dir.mkdir(parents=True)
        not_the_working_repo = scratch / "not-the-working-repo"
        not_the_working_repo.mkdir()
        registered_other_working_repo = scratch / "some-other-registered-working-repo"
        # TOML single-quoted strings are LITERAL -- wrap the raw path in
        # single quotes directly, never Python's `!r` repr. `!r` on a
        # backslash-separated Windows path escapes each backslash a SECOND
        # time before it lands in the TOML literal string, which the TOML
        # parser then reads back with doubled backslashes -- this bug
        # pre-dates C4 and is unrelated to the gate mechanism below;
        # RESOLVED_CLASS parsed correctly either way, only RESOLVED_ROOT's
        # string comparison broke.
        (ml_dir / "registry.local.toml").write_text(
            f'"repos.claude_klabauter" = \'{_mirror_root()}\'\n'
            f'"engine.working_repos.other" = \'{registered_other_working_repo}\'\n'
            # docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md § C5
            # (AC20): the published-engine divert requires `engine.target` to be
            # READABLE — presence-only, its value never inspected. Omitting it
            # here (as every fixture built before C5 did) means step 1 of the
            # ladder never fires regardless of the stamp/gate above, falling
            # through to the live-tree rung and resolving `live-working-tree`
            # instead of `resolved-engine`. Same one-line fixture fix as
            # `_make_published_engine_fixture` in
            # test_working_repos_is_locator_only.py.
            '"engine.target" = \'main\'\n',
            encoding="utf-8",
        )
        cwd = scratch / "no-git-here"
        cwd.mkdir()

        script_path = scratch / "_c2_child.py"
        script_path.write_text(_CHILD_SCRIPT, encoding="utf-8")

        env = dict(os.environ)
        env.pop("CLAUDE_KLABAUTER_ROOT", None)
        env.pop("CLAUDE_KLABAUTER_ROOT", None)
        # Set (not cleared) to a directory that is NOT the registered
        # working repo above — this is what lets `_is_engine_working_repo`
        # return the CONFIRMED `False` this module's gate requires, rather
        # than the `None` ("undeterminable") a missing/absent session root
        # would produce, which would fall through to the live-tree ladder
        # instead of proving the published rung answered.
        env["CLAUDE_PROJECT_DIR"] = str(not_the_working_repo)
        env["MACHINE_LOCAL_REGISTRY_DIR"] = str(ml_dir)

        result = subprocess.run(
            [sys.executable, str(script_path), _mirror_root()],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        assert result.returncode == 0, (
            f"mirror-only resolution failed unexpectedly:\nstdout={result.stdout}\n"
            f"stderr={result.stderr}"
        )
        assert "RESOLVED_CLASS=resolved-engine" in result.stdout, result.stdout
        assert f"RESOLVED_ROOT={_mirror_root()}" in result.stdout, result.stdout


@pytest.mark.skipif(_SKIP_REASON is not None, reason=str(_SKIP_REASON))
def test_real_op_executes_from_mirror_under_unreachable_live_tree():
    """Runs a real, side-effect-free op (archive-stamp-cli.py --help)
    straight out of the mirror's own coordinator/bin/, under the same
    unreachable-live-tree env as above. Asserts no partial-checkout error
    fires (exit 0, no traceback on stderr) and the op produces its normal
    argparse usage output.

    archive-stamp-cli.py is chosen because (unlike
    publish-resolve-target.py, which is a publish-side tool deliberately
    never allowlisted into the mirror) it IS genuinely published, is
    side-effect-free under ``--help`` (prints its subcommand usage banner
    and exits 0 without touching any handoff/archive state), and is
    confirmed present via ``_mirror_carries_required_files`` above."""
    target = Path(_mirror_root()) / "coordinator" / "bin" / "archive-stamp-cli.py"

    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        cwd = scratch / "no-git-here"
        cwd.mkdir()

        env = dict(os.environ)
        env.pop("CLAUDE_KLABAUTER_ROOT", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        # Deliberately no MACHINE_LOCAL_REGISTRY_DIR override here — the op
        # is exec'd directly (not via exec_cli's own resolution ladder), so
        # this leg proves the mirror's CODE runs standalone once resolved,
        # not a second resolution pass.

        result = subprocess.run(
            [sys.executable, str(target), "--help"],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        assert result.returncode == 0, (
            f"real op from mirror failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "Traceback" not in result.stderr
        assert "partial" not in result.stderr.lower()
        assert result.stdout.strip() != ""
