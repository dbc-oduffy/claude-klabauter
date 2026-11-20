"""A failed review.reverify list call is reported as status_before indeterminate, and grind withholds it."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import wake_digest
from coordinator_core.ops.dispatch_emit.landing_integrity import REVERIFY_INDETERMINATE
from coordinator_core.ops.dispatch_emit.tests.test_grind_compose import _compose
from coordinator_core.ops.review_mint import execute_review as er

_KW = dict(
    base_ref="abc123",
    prep_slice_id="prep",
    repo_root=None,
    plan_path="docs/plans/p.md",
    run_base_sha="deadbeef",
    prompt_head="HEAD",
    host_degraded=False,
    sc=lambda call, *_a: call,
)
_STEM = "run no tests and stop"


def _phase(**over) -> str:
    return er._reverify_phase(**{**_KW, **over})[1]


def test_single_prefix_prompt_carries_the_indeterminate_instruction():
    block = _phase()
    assert _STEM in block
    assert f'status_before \\"{REVERIFY_INDETERMINATE}\\"' in block or f'status_before "{REVERIFY_INDETERMINATE}"' in block
    assert "reverify_error" in block


def test_group_prompt_carries_the_indeterminate_instruction():
    block = _phase(group_prefixes=["g1", "g2"])
    assert _STEM in block
    assert "reverify_error" in block


def test_windows_reverify_commands_carry_the_snapshot_identity(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(er, "os", SimpleNamespace(name="nt"))
    session = "01a1f576-c271-7602-b740-b7a76c816332"
    for prefixes in ([], ["g1", "g2"]):
        block = _phase(
            group_prefixes=prefixes,
            repo_root="C:/dev/repo",
            snapshot_session_id=session,
            run_key="ask-fixture",
        )
        commands = [
            part.split("`", 1)[0]
            for part in block.split("`coordinator-invoke review.reverify ")[1:]
        ]
        assert len(commands) == (6 if prefixes else 4)
        assert all(f'"session_id":"{session}"' in command for command in commands)
        assert all("--repo \\'C:/dev/repo\\'" in command for command in commands)
        assert "${COORDINATOR_SETTINGS_HOME:-" not in block


def _both() -> list[str]:
    return [_phase(), _phase(group_prefixes=["g1", "g2"])]


def test_empty_lists_never_say_pass_and_map_to_not_run():
    for block in _both():
        assert "status_before pass" not in block
        assert "status_before \\\"not_run\\\"" in block or 'status_before "not_run"' in block
        assert "to pass when none failed" not in block


def test_pass_requires_a_test_that_ran():
    for block in _both():
        assert "tests ran and none failed" in block
        assert "Never report pass when no test ran" in block
        assert "pass requires at least one test that ran and passed" in block


def test_owned_tests_run_first_and_the_capped_remainder_is_not_run_with_tests_run():
    from coordinator_core.ops.reverify_scope import TRIPWIRE_BOUND

    for block in _both():
        owned_at = block.index("first ONE pytest call of the test files among them")
        tripwire_at = block.index("ONE pytest call of the `tripwires` files")
        assert owned_at < tripwire_at
        assert "`owned`" in block and "`tripwires_not_selected`" in block
        assert f"never more than {TRIPWIRE_BOUND}" in block
        assert "every file past that cut is `not_run`" in block
        assert "Return `tests_run`" in block and "`not_run` (a list of paths)" in block
        assert "the test files that import an edited module" not in block
        assert "with a not-run remainder are pass with `not_run` listed, never indeterminate" in block
        assert "pass requires at least one test that ran and passed" in block


def test_group_prompt_runs_the_union_of_owned_lists_first():
    block = _phase(group_prefixes=["g1", "g2"])
    assert "`owned` list (the union of the `owned` lists)" in block
    assert "files (the union in list order)" in block


def test_verify_scope_targets_join_the_owned_call():
    block = _phase(group_prefixes=["g1"], verify_scope_targets={"a.py": ["t/test_a.py"]})
    assert "among them, the verify-scope targets named below and the `owned` list" in block
    assert "t/test_a.py" in block


def test_schema_declares_tests_run_and_not_run_and_keeps_them_optional():
    props = er._REVERIFY_SCHEMA["properties"]
    assert props["tests_run"] == {"type": "integer"}
    assert props["not_run"] == {"type": "array", "items": {"type": "string"}}
    assert "tests_run" not in er._REVERIFY_SCHEMA["required"]
    assert "not_run" not in er._REVERIFY_SCHEMA["required"]


def test_node_test_files_are_selected_under_node_test_one_file_at_a_time():
    for block in _both():
        assert "*.test.js" in block
        assert "tests/*.test.js" in block
        assert "node --test <that file>" in block
        assert "never a directory" in block


def test_schema_declares_reverify_error_as_optional_string_or_null():
    prop = er._REVERIFY_SCHEMA["properties"]["reverify_error"]
    assert prop == {"type": ["string", "null"]}
    assert "reverify_error" not in er._REVERIFY_SCHEMA["required"]


def test_grind_withholds_an_indeterminate_fix_and_still_withholds_a_failed_one():
    script = _compose()
    assert f"(_reviewReverify.status_after ?? _reviewReverify.status_before) === '{REVERIFY_INDETERMINATE}' || (" in script
    assert "=== 'fail' && !(" in script
    assert ") : !_verifyOnly;" in script


def test_wake_digest_projection_carries_reverify_error():
    params = wake_digest.next_action_parts(
        has_commit_request=True,
        review_vars={
            "prep": "_reviewPrep", "wave": "_reviewWave", "delivery": "_deliveryVerdict",
            "bookkeeping_stem": "'stem'", "plan_id_literal": "null", "reverify": "_reviewReverify",
        },
        test_var=None, falsifier_var=None, verification_var="_verifications",
        test_absent_status="not_run",
    )[2]
    assert "reverify_error: _reviewReverify.reverify_error ?? null" in params


def test_doe_reverify_inventory_command_runs_verbatim_in_powershell(monkeypatch):
    """DoE 03510a97 agent 14 hit a parser error before review.reverify ran."""
    import json
    import shutil
    import subprocess
    from types import SimpleNamespace

    import pytest

    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell is unavailable")

    monkeypatch.setattr(er, "os", SimpleNamespace(name="nt"))
    repo = "C:/dev/coordinator-content-repo"
    session = "01a1f578-6766-7e63-a9e1-0bbe1d4cd6a7"
    base = "d5cca1050624125ddd93f1bb121fe4ca9400d9b0"
    run_key = "ask-20261120T070448-96d73a"
    prefix = f"manifest-{run_key}-{base[:12]}-prep"
    block = _phase(
        repo_root=repo,
        snapshot_session_id=session,
        base_ref=base,
        run_key=run_key,
        prep_slice_id=prefix,
    )
    command = next(
        part.replace(r"\'", "'")
        for part in block.split(chr(96))
        if f'{{"base":"{base}","slice_prefix":"{prefix}"' in part
    )
    # Capture the operation argv without invoking a live engine or mutating DoE.
    capture = "function coordinator-invoke { ConvertTo-Json -Compress -InputObject @($args) }"
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", f"{capture}\n{command}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    op, payload, flag, root = json.loads(result.stdout)
    assert (op, flag, root) == ("review.reverify", "--repo", repo)
    assert json.loads(payload) == {
        "base": base,
        "slice_prefix": prefix,
        "run_key": run_key,
        "session_id": session,
    }


def test_reverify_test_leg_runs_pinned_module_from_product_cwd(monkeypatch, tmp_path):
    import json
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell is unavailable")
    engine = tmp_path / "pinned engine"
    product = tmp_path / "product"
    product.mkdir()
    module = engine / "coordinator_core" / "ops" / "test_leg_run.py"
    module.parent.mkdir(parents=True)
    for package in (module.parent, module.parent.parent):
        (package / "__init__.py").touch()
    module.write_text(
        "import json, os, sys\nprint(json.dumps([__file__, os.getcwd(), sys.argv[1:]]))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(engine))
    clause = er._test_leg_clause(
        "abc123", "scratch/base.txt", "scratch/result.json", repo_root=str(product), shell="powershell",
    )
    command = next(part for part in clause.split(chr(96)) if "coordinator_core.ops.test_leg_run" in part)
    run = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", command.replace("<the pytest targets>", "tests/test_sample.py")],
        cwd=product, capture_output=True, text=True, timeout=5,
    )
    assert run.returncode == 0, run.stderr
    origin, cwd, argv = json.loads(run.stdout)
    assert str(engine) not in clause
    assert Path(origin) == module
    assert Path(cwd) == product
    assert argv == [
        "--base-sha", "abc123", "--head-sha", "HEAD", "--base-export-out", "scratch/base.txt",
        "--out", "scratch/result.json", "--", "tests/test_sample.py",
    ]


def test_emitted_reverify_consumes_host_verdict_and_preserves_legacy_route():
    import json
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    block = _phase()
    script = (
        "async function run(engineOp) { let workerCalls = 0;"
        " const phase = () => {}; const _verifyOnly = false;"
        " const _reviewWave = []; const _reviewPrep = null;"
        " const agent = () => (++workerCalls, Promise.resolve({"
        " edited: [], tests_run: 0, status_before: 'not_run', reverted: [] }));"
        + block
        + " return [_reviewReverify, workerCalls]; }"
        " const host = { edited: ['a.py','b.py'], tests_run: 1, status_before: 'pass', reverted: [] };"
        " const pass = await run(async (op, params) => {"
        " if (op !== 'review.reverify.host_test_leg' || params.slice_prefixes[0] !== 'prep') throw Error(op);"
        " return host; });"
        " const red = await run(async () => ({...host, status_before: 'fail'}));"
        " const closed = await run(async () => { throw Error('host timeout'); });"
        " console.log(JSON.stringify([pass, red, closed, await run(undefined)]));"
    )
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    passed, red, closed, legacy = json.loads(proc.stdout)
    assert (passed[0]["status_before"], passed[0]["tests_run"], passed[1]) == ("pass", 1, 0)
    assert (red[0]["status_before"], red[0]["tests_run"], red[1]) == ("fail", 1, 1)
    assert (closed[0]["status_before"], closed[1]) == ("indeterminate", 0)
    assert "host timeout" in closed[0]["reverify_error"]
    assert (legacy[0]["status_before"], legacy[1]) == ("not_run", 1)
