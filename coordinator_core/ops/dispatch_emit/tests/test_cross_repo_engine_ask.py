"""An engine-ask driven from one repo (`driver`) at another repo's tree (`target`): the four failures.

One fixture, two real git repos under tmp_path. The process cwd is the driver, which is where every
in-run `coordinator-invoke` and `sizing-assemble` lands when its command pins no root. Each case
names its failure.

- b1 the sizing is written in one repo and read in another.
- b2 the target's own rows are withheld as foreign.
- b3 review prep resolves the run base sha in the wrong object store.
- b4 the delivery re-verify names the driver's HEAD and reads the driver's diff.

A command is pinned to the target by a literal `--repo <root>` or a `cd <root>` ahead of it, or an
absolute path where the command takes one; a root held in a JS variable is not read here.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_stage import _handler as ask_stage
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_gated_rows import _patch_registry
from coordinator_core.ops.review_prep import _handler as review_prep
from coordinator_core.testing.git_write import write_seen_by_git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_FLAGS = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_PLAN_REL = "docs/plans/2026-10-15-cross-repo.md"
_PLAN_ID = "pln-cross-repo-abc123"
_FAIL = {"verdict": "FAIL", "product_files": 1, "unbacked": [{"claim": "adds mod.py change", "anchor": "none"}]}
_SESSION = "sid-cross-repo"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, **_FLAGS
    ).stdout.strip()


def _put(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_seen_by_git(path, text, newline="\n")


def _init(root: Path, marker: str) -> Path:
    """A repo whose history is unique to `marker`, so no sha is shared with another fixture repo."""
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _put(root / "pkg" / "mod.py", f"# {marker}\nVALUE = 1\n")
    _put(root / ".gitignore", ".coordinator-local/\nstate/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"seed {marker}")
    return root


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The driver is the process cwd; the target holds a committed base and one uncommitted change."""
    driver = _init(tmp_path / "driver", "driver")
    target = _init(tmp_path / "target-repo", "target")
    _put(driver / "pkg" / "driver_only.py", "X = 1\n")
    _git(driver, "add", "-A")
    _git(driver, "commit", "-q", "-m", "driver moves on")
    base = _git(target, "rev-parse", "HEAD")
    _put(target / "pkg" / "mod.py", "# target\nVALUE = 2\n")
    _put(
        target / _PLAN_REL,
        f"---\nplan_id: {_PLAN_ID}\nstatus: executing\n---\n# Plan\n",
    )
    monkeypatch.chdir(driver)
    return SimpleNamespace(driver=driver, target=target, base=base)


def _compose(root: Path) -> str:
    return compose_ask_script(
        repo_root=root.as_posix(),
        prompt="add a widget",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def _enclosing_repo(path: Path) -> Path | None:
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent.resolve()
    return None


def _command_span(script: str, op: str) -> str:
    """The backtick-free run of script text holding `coordinator-invoke" <op>`, with the anchor clause (`cd <root>` ... gate command) before it."""
    hit = re.search(rf'coordinator-invoke"[^`]*?\b{re.escape(op)}\b[^`]*', script)
    assert hit, f"{op} is not invoked in the composed script"
    return script[max(0, hit.start() - 480) : hit.end()]


def _pinned_root(span: str) -> Path | None:
    hit = re.search(r"--repo[ =]+\\?['\"]?([^\s'\"`]+?)(?=\\?['\"\s`]|$)", span) or re.search(
        r"\bcd \\?['\"]?([^\s'\"`;&]+?)(?=\\?['\"\s`;&]|$)", span
    )
    return Path(hit.group(1)) if hit else None


def _landing_root(span: str) -> Path | None:
    """The repo an invoke command with this text acts on when run from the driver cwd."""
    pinned = _pinned_root(span)
    return _enclosing_repo(pinned if pinned is not None else Path(show_toplevel() or Path.cwd()))


def _js_string_value(expr: str, repo_root: str, slug: str = "x") -> str:
    out = []
    for token in (t.strip() for t in expr.split(" + ")):
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
            out.append(token[1:-1])
        elif token in ("_slug", "_slugOf(r)"):
            out.append(slug)
        elif token == "REPO_ROOT":
            out.append(repo_root)
        else:
            raise AssertionError(f"cannot evaluate {token!r} in {expr!r}")
    return "".join(out)


def test_b1_the_sizing_is_written_and_read_in_the_same_repo(world):
    """The size phase writes the sizing with a repo-relative `--write`, which lands in the cwd repo;
    the gate and every later phase read it from the run's root."""
    script = _compose(world.target)
    expr = re.search(r"const _sizingPathFor = \(r\) => (.+);\n", script).group(1)
    sizing = _js_string_value(expr, world.target.as_posix())
    scout = next(ln for ln in script.splitlines() if "const _scouted" in ln)
    write_root = _landing_root(scout)
    written = Path(sizing) if Path(sizing).is_absolute() else write_root / sizing
    assert _enclosing_repo(written.parent) == world.target.resolve(), f"sizing written at {written}"
    # The read-side expression above is injected with the target as root, so it cannot show where the
    # scout's own `--write` lands; pin that argument in the composed prompt.
    template_writes = re.findall(r"--write \\'([^']+?)\\'", scout)
    assert template_writes, "the scout prompt names no --write argument"
    for arg in template_writes:
        landed = Path(arg) if Path(arg).is_absolute() else write_root / arg
        assert _enclosing_repo(landed.parent) == world.target.resolve(), f"scout --write {arg!r} lands at {landed}"
    assert "dispatch.ask_gate" in scout
    assert _landing_root(scout) == world.target.resolve()


def test_b2_the_targets_own_rows_are_staged_not_withheld(world, monkeypatch):
    """A row gated on the repo the run is writing into is that repo's own row; a row gated on a third
    repo stays withheld."""
    third = world.target.parent / "third-repo"
    _patch_registry(
        monkeypatch,
        {"repos.target_repo": world.target.as_posix(), "repos.third_repo": third.as_posix()},
    )
    _put(
        world.target / _PLAN_REL,
        "---\nplan_id: pln-cross-repo-abc123\n---\n# Plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: U1\n  title: ungated\n  change_kind: code-edit\n  surface: pkg/a.py\n  writes:\n    - pkg/a.py\n"
        "- id: G1\n  title: owned by the target\n  change_kind: code-edit\n  surface: pkg/g.py\n  writes:\n    - pkg/g.py\n"
        "  external_gate:\n    - owner_repo: target-repo\n      requires: landed-work\n"
        "- id: G2\n  title: owned by a third repo\n  change_kind: code-edit\n  surface: pkg/h.py\n  writes:\n    - pkg/h.py\n"
        "  external_gate:\n    - owner_repo: third-repo\n      requires: landed-work\n```\n",
    )
    reply = read_stage_reply(world.target, ask_stage({"run_id": "r1", "plan_path": _PLAN_REL}, repo_root=world.target))
    assert "error" not in reply, reply
    assert "G1" in [r["id"] for r in reply["rows"]], "the target's own row was withheld"
    assert [g["id"] for g in reply["gated"]] == ["G2"]


def test_b3_review_prep_resolves_the_run_base_sha_in_the_targets_object_store(world):
    """The composed prep command, run the way an agent runs it from the driver cwd, freezes the target's diff."""
    script = _compose(world.target)
    command = re.search(r"coordinator-invoke\" review\.prep \\'(\{.*?\})\\'", script)
    assert command, "no review.prep command in the composed script"
    params_text = command.group(1).replace("\\'", "'")
    assert world.base in params_text
    params_text = params_text.replace("<plan_path>", _PLAN_REL)
    import json

    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    params = {**json.loads(params_text), "session_id": _SESSION}
    root = _landing_root(_command_span(script, "review.prep"))
    assert root == world.target.resolve()
    manifest = record_dir(world.target, "run-1") / "manifest.json"
    _put(manifest, json.dumps({"review_declared_paths": ["pkg/mod.py"]}))
    reply = review_prep(params, repo_root=root)
    assert reply["error"] is None, reply["error"]
    assert reply["run_base_sha"] == world.base and reply["product_files"] == 1


def test_b4_the_delivery_reverify_judges_the_targets_head_and_diff(world, capsys, verifier_roster):
    """`--reverify-delivery` on the target's run record, run from the driver cwd, composes the target's
    diff: its head is the target's HEAD, never the driver's."""
    _git(world.target, "add", "-A")
    _git(world.target, "commit", "-q", "-m", "the run's change")
    head = _git(world.target, "rev-parse", "HEAD")
    assert head != _git(world.driver, "rev-parse", "HEAD")
    record = _record_in(world.target, world.base)
    out = world.target / "docs" / "plans" / "cross-repo.workflow.mjs"
    rc = cli_module.main(
        ["--plan", str(world.target / _PLAN_REL), "--out", str(out), "--reverify-delivery", str(record)]
    )
    assert rc == cli_module.EXIT_OK, capsys.readouterr().err
    text = out.read_text(encoding="utf-8")
    named = re.findall(r"git diff ([0-9a-f]{40}\.\.[0-9a-f]{40})", text)
    assert f"{world.base}..{head}" in named, f"the delivery diff names {named}, not the target's run range"


def _record_in(repo: Path, base: str) -> Path:
    import json

    data = {
        "kind": "superseding-review",
        "plan_id": _PLAN_ID,
        "commit_range": {"base": base, "head": base},
        "supersedes": None,
        "prep_sidecar": None,
        "unresolved": [],
        "confinement_violations": 0,
        "fixes_applied": 0,
        "slices": 1,
        "brief_conformance": {"items": 0, "met": 0, "unmet": 0},
        "prep": {"run_base_sha": base, "product_files": 1, "foreign_claims": [], "slice_files": ["pkg/mod.py"]},
        "delivery": _FAIL,
        "tests": {"status": "not_run"},
        "criterion": {"status": "met", "observation": "ok", "sidecar": None},
    }
    path = repo / "state" / "superseding-reviews" / "2026-10" / "rec.md"
    _put(path, "---\n" + "\n".join(f"{k}: {json.dumps(v)}" for k, v in data.items()) + "\n---\n\nbody\n")
    return path
