"""compose_execute_review(prep_run_id=...): the wave prep command names the run, not the paths."""

import re

import pytest

from coordinator_core.ops.review_mint.execute_review import compose_execute_review
from coordinator_core.ops.review_mint.tests.test_execute_review import (
    _STAGE_SCHEMAS,
    _ratio_review,
)


def _compose(**extra):
    kwargs = dict(
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["x.py"],
        executor_rows=9,
        prep_in_wave=True,
    )
    if extra.get("prep_run_id") is not None:
        kwargs["plan_path"] = "state/warp/r/manifest.json"
    kwargs.update(extra)
    return compose_execute_review(_ratio_review(), **kwargs)


def _prep_commands(phases):
    block = dict(phases)["Review wave"]
    return re.findall(r"review\.prep \\'\{.*?\}\\'", block)


def test_prep_run_id_replaces_the_path_list_in_every_prep_command():
    commands = _prep_commands(_compose(prep_run_id="ask-x"))
    assert commands
    for command in commands:
        assert '"run_id":"ask-x"' in command.replace('\\"', '"')
        assert "paths" not in command
    block = dict(_compose(prep_run_id="ask-x"))["Review wave"]
    assert "<every declared path>" not in block
    assert '"leg":true' in block.replace('\\"', '"')


def test_without_prep_run_id_the_explicit_path_placeholder_stays():
    block = dict(_compose())["Review wave"]
    assert "<every declared path>" in block
    assert "run_id" not in "".join(_prep_commands(_compose()))


def test_none_is_byte_identical_to_omitting_the_keyword():
    assert _compose(prep_run_id=None) == _compose()


def test_prep_run_id_may_equal_run_key():
    assert _compose(prep_run_id="ask-x", run_key="ask-x")


@pytest.mark.parametrize(
    "extra",
    [
        {"prep_in_wave": False},
        {"run_key": "other"},
    ],
)
def test_prep_run_id_guards_raise(extra):
    with pytest.raises(ValueError):
        _compose(prep_run_id="ask-x", **extra)


@pytest.mark.parametrize(
    "extra",
    [
        {"slice_identity_sha": "b" * 40},
        {"plan_path": "docs/plans/example.md"},
        {"slice_key_js": "x"},
    ],
)
def test_prep_run_id_refuses_inputs_that_fork_the_composer_id_from_the_op(extra):
    with pytest.raises(ValueError):
        _compose(prep_run_id="ask-x", **extra)


@pytest.mark.parametrize("base", [None, ""])
def test_prep_run_id_without_a_base_uses_the_nobase_prefix(base):
    from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

    run = "ask-x"
    expected = run_prep_slice_prefix(run, base)
    assert expected == "manifest-ask-x-nobase-prep"
    block = dict(_compose(prep_run_id=run, run_key=run, run_base_sha=base))["Review wave"]
    assert expected in block


def test_prep_run_id_accepts_matching_slice_identity_sha():
    assert _compose(prep_run_id="ask-x", slice_identity_sha="a" * 40)


def test_prep_run_id_slice_id_is_the_canonical_run_prefix_and_unchanged():
    from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

    run = "warp-x-20261026T133215-c4413e"
    legacy = f"manifest-{run}-{'a' * 12}-prep"
    assert run_prep_slice_prefix(run, "a" * 40) == legacy
    block = dict(_compose(prep_run_id=run, run_key=run))["Review wave"]
    assert legacy in block
    assert legacy not in "".join(_prep_commands(_compose(prep_run_id=run, run_key=run)))


def test_no_run_id_mode_prep_command_carries_slice_prefix():
    commands = _prep_commands(_compose(prep_run_id="ask-x"))
    assert commands
    assert all("slice_prefix" not in c for c in commands)
    assert all("slice_prefix" in c for c in _prep_commands(_compose()))


def test_run_id_leg_note_never_quotes_a_leg_value_line():
    from coordinator_core.ops.review_mint.execute_review import _wave_prep_instruction

    text = _wave_prep_instruction("m", "b", "p", None, grouped=False, run_id="r")
    assert "leg: true" not in text
    assert "only when the values below carry a `leg:` line" in text
    assert '"leg"' not in _wave_prep_instruction("m", "b", "p", None, grouped=False, paths_literal="[]")


_FOREIGN_PREFIX = "manifest-warp-720eea-20261116T190245-683d89-debe14b5896b-prep"


def _guard_verdict(expected: str, reply: dict):
    import json
    import shutil
    import subprocess

    from coordinator_core.ops.review_mint.execute_review import _foreign_prep_guard_js
    from coordinator_core.win_portability import no_console_creationflags

    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    script = (
        f"const _reviewPrep = {json.dumps(reply)};\n"
        f"{_foreign_prep_guard_js(expected)}\n"
        "console.log(JSON.stringify({slices: _reviewPrep.slices.length, verdict: _reviewPrep.verdict ?? null, "
        "error: _reviewPrep.error ?? null}));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True, **no_console_creationflags())
    return json.loads(out.stdout)


def _reply(prefix: str, sep: str = "/", whole: str = ""):
    d = f"state{sep}review-trail{sep}diffs{sep}"
    s = f".coordinator-local{sep}subagent-share{sep}sess{sep}"
    return {
        "error": None,
        "whole_diff_path": f"{d}{whole or prefix}.diff",
        "whole_diff_sidecars": {"personas": [f"{s}{prefix}.whole-slice.md"]},
        "slices": [{"id": f"{prefix}-1", "files": ["x.py"], "diff_path": f"{d}{prefix}-1.diff",
                    "sidecar_path": f"{s}{prefix}.whole-slice.md"}],
    }


def test_foreign_prefix_reply_is_emptied_with_a_verdict_naming_it():
    from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

    own = run_prep_slice_prefix("warp-aa0f1e-20261116T190110-ebf10b", "d" * 40)
    verdict = _guard_verdict(own, _reply(_FOREIGN_PREFIX))
    assert verdict["slices"] == 0
    assert verdict["verdict"].startswith("foreign-prep-prefix: ") and _FOREIGN_PREFIX in verdict["verdict"]
    assert verdict["error"]


@pytest.mark.parametrize("sep", ["/", "\\"])
def test_own_prefix_reply_passes_untouched_with_either_separator(sep):
    from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

    own = run_prep_slice_prefix("warp-aa0f1e-20261116T190110-ebf10b", "d" * 40)
    verdict = _guard_verdict(own, _reply(own, sep))
    assert verdict == {"slices": 1, "verdict": None, "error": None}


def test_foreign_whole_diff_path_alone_refuses_the_reply():
    from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

    own = run_prep_slice_prefix("warp-aa0f1e-20261116T190110-ebf10b", "d" * 40)
    reply = _reply(own, "\\", whole=_FOREIGN_PREFIX)
    assert _guard_verdict(own, reply)["slices"] == 0


@pytest.mark.parametrize("key", ["kira", "delivery"])
def test_foreign_kira_or_delivery_sidecar_alone_refuses_the_reply(key):
    from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

    own = run_prep_slice_prefix("warp-aa0f1e-20261116T190110-ebf10b", "d" * 40)
    reply = _reply(own)
    reply["whole_diff_sidecars"][key] = f".coordinator-local/subagent-share/sess/{_FOREIGN_PREFIX}.{key}.md"
    assert _guard_verdict(own, reply)["slices"] == 0
    reply["whole_diff_sidecars"][key] = f".coordinator-local/subagent-share/sess/{own}.{key}.md"
    assert _guard_verdict(own, reply)["slices"] == 1


def test_run_mode_compose_embeds_the_guard_before_the_slices_guard_and_paths_mode_does_not():
    from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

    run = "warp-aa0f1e-20261116T190110-ebf10b"
    base = "d" * 40
    block = dict(_compose(prep_run_id=run, run_key=run, run_base_sha=base))["Review wave"]
    assert "_foreignPrepName" in block
    from coordinator_core.ops.review_mint.execute_review import _js_string_literal

    assert f"const _ep = {_js_string_literal(run_prep_slice_prefix(run, base))};" in block
    from coordinator_core.ops.review_mint.execute_review import NO_SLICES_HALT

    assert block.index("_foreignPrepName") < block.index(NO_SLICES_HALT)
    assert "_foreignPrepName" not in dict(_compose())["Review wave"]


_SIBLING_A = "ask-20261124T195842-45c335"
_SIBLING_B = "ask-20261124T195842-bdb8b7"


def _reply_file(text: str) -> str:
    found = re.findall(r"`([^`\s]*\.review-prep-reply\.json)`", text)
    assert len(found) == 1
    return found[0]


def test_sibling_run_instructions_name_distinct_run_scoped_reply_files():
    from coordinator_core.ops.review_mint.execute_review import _wave_prep_instruction
    from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

    base = "a" * 40
    files = {}
    for run in (_SIBLING_A, _SIBLING_B):
        text = _wave_prep_instruction("m", base, "p", None, grouped=False, run_id=run)
        files[run] = _reply_file(text)
        assert files[run] == f"{run_prep_slice_prefix(run, base)}.review-prep-reply.json"
        assert "redirected to a file under your scratchpad" not in text
    assert files[_SIBLING_A] != files[_SIBLING_B]


def test_grouped_instruction_reply_file_carries_the_group_index_placeholder():
    from coordinator_core.ops.review_mint.execute_review import _wave_prep_instruction
    from coordinator_core.ops.review_slice_id import run_prep_slice_prefix

    base = "a" * 40
    text = _wave_prep_instruction("m", base, "p", None, grouped=True, run_id=_SIBLING_A)
    assert _reply_file(text) == f"{run_prep_slice_prefix(_SIBLING_A, base)}-g<group_index>.review-prep-reply.json"


def test_non_sha_base_falls_back_to_the_sanitised_run_id_and_paths_mode_uses_its_slice_prefix():
    from coordinator_core.ops.review_mint.execute_review import _wave_prep_instruction

    text = _wave_prep_instruction("m", "<base>", "p", None, grouped=False, run_id="ask x/1")
    assert _reply_file(text) == "ask-x-1.review-prep-reply.json"
    text = _wave_prep_instruction("m", "b", "plan-abc-prep", None, grouped=False, paths_literal="[]")
    assert _reply_file(text) == "plan-abc-prep.review-prep-reply.json"


def test_wave_prep_instruction_refuses_run_id_with_paths_literal():
    from coordinator_core.ops.review_mint.execute_review import _wave_prep_instruction

    with pytest.raises(ValueError):
        _wave_prep_instruction("m", "b", "p", None, grouped=False, paths_literal="[]", run_id="r")
