"""`_early_engine_import_refusal` probes the changed engine modules in the per-sha source export
before the round stages anything; withheld paths are skipped, and the probe
costs no more process time than the post-run gate on the real tree."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BIN_DIR.parents[1]
_NOISE_MS = 100.0

_spec = importlib.util.spec_from_file_location("percolate_round_early_gate", _BIN_DIR / "percolate-round.py")
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

_CONSUMER = "coordinator_core/consumer.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def scene(tmp_path, monkeypatch):
    """A source repo whose HEAD ships a consumer importing a name the provider lacks; the mirror
    is stamped at the earlier, good commit."""
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q")
    _write(src, "coordinator_core/__init__.py", "")
    _write(src, "coordinator_core/provider.py", "def helper():\n    return 1\n")
    _write(src, _CONSUMER, "from coordinator_core.provider import helper\n")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "good")
    good = _git(src, "rev-parse", "HEAD")
    _write(src, _CONSUMER, "from coordinator_core.provider import missing_name\n")
    _git(src, "commit", "-qam", "bad")
    shipped = _git(src, "rev-parse", "HEAD")

    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _git(mirror, "init", "-q")
    _git(mirror, "commit", "-q", "--allow-empty", "-m", f"publish [source-head {good}]")

    exports: list = []
    clone_stub = SimpleNamespace(
        default_cache_root=lambda: tmp_path / "chain",
        materialise_commit_clone=lambda repo, sha, cache: exports.append(sha) or src,
    )
    monkeypatch.setattr(_mod, "_commit_clone_module", lambda: clone_stub)
    return SimpleNamespace(src=src, mirror=mirror, shipped=shipped, exports=exports)


def _refusal(scene, *, withholding=None, shipped="default", mirror="default"):
    return _mod._early_engine_import_refusal(
        str(scene.src),
        scene.shipped if shipped == "default" else shipped,
        withholding,
        mirror=scene.mirror if mirror == "default" else mirror,
    )


def test_a_bad_changed_module_is_refused_by_name(scene):
    refusal = _refusal(scene)

    assert refusal and "consumer" in refusal and "missing_name" in refusal
    assert scene.exports == [scene.shipped]


def test_a_withheld_path_is_not_probed(scene):
    assert _refusal(scene, withholding=SimpleNamespace(paths={_CONSUMER: None})) is None


def test_no_shipped_sha_is_left_to_the_post_run_gate(scene):
    assert _refusal(scene, shipped=None) is None
    assert scene.exports == []


def test_no_mirror_stamp_probes_only_the_always_module(scene):
    assert _refusal(scene, mirror=None) is None


def test_the_early_probe_writes_bytecode_only_into_the_export(scene, monkeypatch):
    flags = []
    monkeypatch.setattr(_mod, "_engine_import_failures", lambda root, rels, **kw: flags.append(kw) or None)
    _refusal(scene)
    assert flags == [{"write_bytecode": True}]

    argv = []
    monkeypatch.undo()
    monkeypatch.setattr(_mod, "_run", lambda cmd, **kw: argv.append(cmd) or SimpleNamespace(returncode=0, stdout="{}", stderr=""))
    _mod._engine_import_failures(str(scene.src), ["coordinator_core/consumer.py"])
    assert "-B" in argv[0]


def test_the_early_probe_costs_no_more_than_the_post_run_gate_on_the_real_tree(tmp_path):
    """Both gates derive the same changed-module list (mirror stamp + one diff) and probe it; the
    early one on the per-sha export, the post-run one on the real tree. Process time is measured
    net of interpreter + module load."""
    sha = _git(_REPO_ROOT, "rev-parse", "HEAD")
    stamp = _git(_REPO_ROOT, "rev-parse", "HEAD~10")
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _git(mirror, "init", "-q")
    _git(mirror, "commit", "-q", "--allow-empty", "-m", f"publish [source-head {stamp}]")

    script = tmp_path / "probe.py"
    script.write_text(
        "import importlib.util, sys\n"
        "from pathlib import Path\n"
        f"repo = Path({str(_REPO_ROOT)!r})\n"
        "sys.path.insert(0, str(repo))\n"
        "spec = importlib.util.spec_from_file_location('pr', repo / 'coordinator/bin/percolate-round.py')\n"
        "m = importlib.util.module_from_spec(spec); sys.modules['pr'] = m; spec.loader.exec_module(m)\n"
        "import coordinator_core.percolate.source_preflight\n"
        "if sys.argv[1] == 'run':\n"
        f"    m._early_engine_import_refusal(str(repo), {sha!r}, None,"
        f" mirror=Path({str(mirror)!r}))\n"
        "elif sys.argv[1] == 'late':\n"
        "    import subprocess\n"
        "    import coordinator_core.percolate.source_preflight as sp\n"
        f"    base = sp.mirror_walk_base(Path({str(mirror)!r}))\n"
        "    out = subprocess.run(['git', '-C', str(repo), 'diff', '--name-only',"
        f" f'{{base}}..{sha}', '--', 'coordinator_core'], capture_output=True, text=True).stdout.split()\n"
        "    m._engine_import_failures(str(repo), out)\n"
        "print('PROBE-RAN')\n",
        encoding="utf-8",
    )
    # Warm the shared per-sha engine export and chain cache outside the timer.
    warm = subprocess.run([sys.executable, str(script), "run"], capture_output=True, text=True, cwd=str(tmp_path))
    assert "PROBE-RAN" in warm.stdout, warm.stderr[-800:]
    warm = subprocess.run([sys.executable, str(script), "late"], capture_output=True, text=True, cwd=str(tmp_path))
    assert "PROBE-RAN" in warm.stdout, warm.stderr[-800:]

    def _measure(mode: str) -> float:
        out = tmp_path / f"{mode}.out"
        result = single_invocation_tree_process_time(
            [sys.executable, str(script), mode], cwd=str(tmp_path), stdout_path=str(out),
        )
        assert result["rc"] == 0 and "PROBE-RAN" in out.read_text(encoding="utf-8")
        return result["process_time_ms"]

    def _best(mode: str) -> float:
        return min(_measure(mode) for _ in range(3))

    baseline = _best("load")
    early = _best("run") - baseline
    late = _best("late") - baseline

    assert early <= late + _NOISE_MS, (
        f"early engine-import probe cost {early:.0f}ms process time, post-run gate {late:.0f}ms"
    )


@pytest.fixture()
def cli(monkeypatch, tmp_path):
    """`_cmd_round` with every preflight stubbed: records the order of gates and whether the round
    body (staging + real run) was reached."""
    calls: list = []
    withholding = SimpleNamespace(refusal=None, paths={}, walk_base=None, toplevel=None)

    monkeypatch.setattr(_mod, "_resolve_percolate_root", lambda override: str(tmp_path))
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(tmp_path / "src"))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(tmp_path / "dest"))
    monkeypatch.setattr(_mod, "_bootstrap_engine", lambda: None)
    monkeypatch.setattr(_mod, "_resolve_shipped_sha", lambda repo, ref: "a" * 40)
    monkeypatch.setattr(_mod, "_fold_bar_refusal", lambda *a, **k: None)
    monkeypatch.setattr(_mod, "_registry_integrity_refusal", lambda *a, **k: None)
    monkeypatch.setattr(_mod, "_field7_preflight", lambda *a, **k: calls.append("field7") or None)
    monkeypatch.setattr(_mod, "_reap_dead_round_tempdirs", lambda: None)
    monkeypatch.setattr(_mod, "_cmd_round_default", lambda *a, **k: calls.append(("round", k)) or 0)
    import coordinator_core.percolate.source_preflight as sp

    monkeypatch.setattr(sp, "check_source_published", lambda *a, **k: None)
    monkeypatch.setattr(sp, "source_withholding", lambda *a, **k: withholding)
    args = _mod._build_parser().parse_args(["alpha", "--percolate-root", str(tmp_path)])
    return SimpleNamespace(calls=calls, args=args, withholding=withholding, sp=sp, monkeypatch=monkeypatch)


def test_a_bad_engine_module_refuses_before_field7_and_the_round_body(cli, capsys):
    cli.monkeypatch.setattr(_mod, "_early_engine_import_refusal", lambda *a, **k: "consumer -> boom")

    assert _mod._cmd_round(cli.args) == _mod._EXIT_FAIL
    assert cli.calls == []
    assert "refused, nothing synced: consumer -> boom" in capsys.readouterr().err


def test_a_withholding_refusal_returns_before_the_early_probe(cli):
    cli.withholding.refusal = "half-work"
    cli.monkeypatch.setattr(
        _mod, "_early_engine_import_refusal", lambda *a, **k: pytest.fail("probe ran after a preflight refusal")
    )

    assert _mod._cmd_round(cli.args) == _mod._EXIT_FAIL
    assert cli.calls == []


def test_the_early_probe_is_skipped_under_no_publish(cli):
    cli.args.no_publish = True
    cli.monkeypatch.setattr(
        _mod, "_early_engine_import_refusal", lambda *a, **k: pytest.fail("probe ran under --no-publish")
    )

    assert _mod._cmd_round(cli.args) == 0
    assert cli.calls[0] == "field7"


def test_the_probe_runs_against_the_destination_mirror_before_field7(cli):
    seen: list = []

    def probe(*a, **k):
        seen.append((a, k))
        cli.calls.append("probe")
        k["passed"].append(a[1])

    cli.monkeypatch.setattr(_mod, "_early_engine_import_refusal", probe)

    assert _mod._cmd_round(cli.args) == 0
    assert cli.calls[:2] == ["probe", "field7"]
    assert seen[0][1]["mirror"].name == "dest"
    assert cli.calls[2][1]["engine_import_probed_sha"] == "a" * 40


def test_a_passed_probe_on_the_same_tree_skips_the_late_gate():
    covers = _mod._early_import_probe_covers_late_gate
    assert covers("a" * 40, SimpleNamespace(paths={})) is True
    assert covers("a" * 40, None) is True


def test_the_late_gate_still_runs_when_the_probe_did_not_pass_or_an_engine_path_was_withheld():
    covers = _mod._early_import_probe_covers_late_gate
    assert covers(None, SimpleNamespace(paths={})) is False
    assert covers("a" * 40, SimpleNamespace(paths={_CONSUMER: None})) is False
    assert covers("a" * 40, SimpleNamespace(paths={"docs/x.md": None})) is True


def test_a_passing_helper_reports_the_probed_sha(scene):
    passed: list = []
    good = _mod._early_engine_import_refusal(
        str(scene.src), scene.shipped,
        SimpleNamespace(paths={_CONSUMER: None}), mirror=scene.mirror, passed=passed,
    )
    assert good is None and passed == [scene.shipped]

    failed: list = []
    assert _refusal(scene) is not None
    _mod._early_engine_import_refusal(
        str(scene.src), scene.shipped, None, mirror=scene.mirror, passed=failed,
    )
    assert failed == []
