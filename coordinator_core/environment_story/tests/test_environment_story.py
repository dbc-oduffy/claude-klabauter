"""coordinator_core/environment_story/tests/test_environment_story.py

Ported subset of DoE-claude's `coordinator/tests/test_environment_story_composition.py`
and `coordinator/tests/test_guard_enforcement_join.py`
(docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W2-C2). One file per the
chunk's own `writes:` footprint -- the ported package is one family, so its
port test is one file.

WHAT DID NOT PORT, AND WHY
    From `test_environment_story_composition.py`:
      - `test_valid_postures_unchanged` and
        `test_posture_fail_open_promise_still_stated_in_docstring` pin
        DoE-claude's `_posture.py` twin. Claude-klabauter has no ported posture module
        in this chunk's footprint (posture is a separate engine-plane
        concern) -- there is nothing here for these two to assert against.
      - `test_every_rule_bearing_id_covered_by_story_or_omission_row` and
        the whole "ledger subtraction" block (`test_a_ratified_row_
        subtracts_its_rule` etc.) exercise `_read_ratified_omissions` /
        `_derive_register_rule_ids` against
        `state/audits/2026-09-06-doctrine-rule-class-register.yaml` and
        `_environment_story_omission_ledger` -- both DoE-resident register
        artifacts, and the two functions themselves do NOT port (see
        `stories.py`'s own PORTING NOTE): the moved package imports nothing
        named `omission_ledger` and carries no regeneration path to pin.
    From `test_guard_enforcement_join.py`:
      - `test_emitter_seam_reads_the_join_and_caches_it` and
        `test_a_delivered_join_changes_the_answer_end_to_end` exercise
        DoE-claude's `coordinator/bin/emit-omission-register.py`, which has
        no claude-klabauter counterpart. Claude-klabauter's own join-completeness/coverage claim
        is already pinned by
        `coordinator_core/tests/test_guard_enforcement_join_covers_every_registered_guard.py`
        (REUSED, not duplicated here, per this chunk's body) -- this module's
        `GuardEnforcementJoin` reader class and `load_join` are what is new
        here, and every test below exercises them directly.

Zero-spawn: no subprocess, no network. Registry/sentinel/join fixtures are
tmp_path files.
"""

from __future__ import annotations

import inspect
import re

import pytest
import yaml

from coordinator_core.environment_story import guard_enforcement_join as join_mod
from coordinator_core.environment_story import selection as sel
from coordinator_core.environment_story import stories as ess  # noqa: F401  (import registers ephemeral-cloud-vm)
from coordinator_core.environment_story import story as es


@pytest.fixture(autouse=True)
def _isolate_registry():
    snapshot = dict(es._registry)
    yield
    es._registry.clear()
    es._registry.update(snapshot)


def _write(path, content: str) -> str:
    path.write_text(content, encoding="utf-8")
    return str(path)


def _all_composed_stories():
    stories_by_name = {es.STRICTEST_STORY.name: es.STRICTEST_STORY}
    stories_by_name.update(es._registry)
    return list(stories_by_name.values())


@pytest.mark.parametrize(
    "module",
    [
        "coordinator_core.environment_story",
        "coordinator_core.environment_story.story",
        "coordinator_core.environment_story.stories",
        "coordinator_core.environment_story.selection",
        "coordinator_core.environment_story.guard_enforcement_join",
    ],
)
def test_moved_package_imports_nothing_named_omission_ledger(module):
    import ast
    import importlib

    mod = importlib.import_module(module)
    tree = ast.parse(inspect.getsource(mod))
    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.append(node.module or "")
            imported_names.extend(alias.name for alias in node.names)
    offending = [name for name in imported_names if "omission_ledger" in name]
    assert not offending, (
        f"{module} imports {offending!r} -- that module stays DoE-repo "
        "tooling and must not be imported from the moved package"
    )


@pytest.mark.parametrize(
    "module",
    [
        "coordinator_core.environment_story.story",
        "coordinator_core.environment_story.stories",
        "coordinator_core.environment_story.selection",
        "coordinator_core.environment_story.guard_enforcement_join",
    ],
)
def test_moved_package_has_no_main_regeneration_block(module):
    import ast
    import importlib

    mod = importlib.import_module(module)
    tree = ast.parse(inspect.getsource(mod))
    has_main_guard = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        for node in ast.walk(tree)
    )
    assert not has_main_guard, (
        f"{module} carries an `if __name__ == '__main__':` block -- "
        "regeneration stays with the DoE-resident ledger, per this chunk's spec"
    )


CONDITIONAL_ADDRESS_PATTERNS: tuple[str, ...] = (
    r"if you( a|')re in",
    r"if this is a",
    r"when running (on|in)",
    r"unless you are",
    r"^on a workstation,",
    r"^in the cloud,",
)

_COMPILED_CONDITIONAL_PATTERNS = [
    re.compile(pattern, re.IGNORECASE) for pattern in CONDITIONAL_ADDRESS_PATTERNS
]

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _sentences(prose: str) -> list[str]:
    flattened = prose.replace("\n", " ")
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(flattened) if s.strip()]


def _conditional_sentences(prose: str) -> list[str]:
    offending = []
    for sentence in _sentences(prose):
        for pattern in _COMPILED_CONDITIONAL_PATTERNS:
            if pattern.search(sentence):
                offending.append(sentence)
                break
    return offending


def test_no_composed_story_prose_contains_environment_conditional():
    stories_list = _all_composed_stories()
    assert {s.name for s in stories_list} >= {"strictest", "ephemeral-cloud-vm"}, (
        "expected both composed stories to be reachable for this check; "
        f"got {[s.name for s in stories_list]}"
    )
    for story in stories_list:
        offending = _conditional_sentences(story.prose)
        assert not offending, (
            f"story {story.name!r} prose contains an environment-"
            f"conditional sentence, which is a failed composition, not a "
            f"style preference: {offending!r}"
        )


# 2. An unrecognised environment resolves to STRICTEST_STORY, through the


def test_unrecognised_environment_resolves_to_strictest_through_selection_seam(
    tmp_path,
):
    registry = _write(tmp_path / "registry", "some-other-box: ephemeral-cloud-vm\n")
    sentinel = _write(tmp_path / "sentinel", "an-environment-nobody-registered\n")

    story = sel.resolve_selected_story(registry_path=registry, sentinel_path=sentinel)

    assert story is es.STRICTEST_STORY
    assert story.name == "strictest"


def test_environment_story_marker_differs_from_itself_and_from_posture():
    posture_marker_start = "<!-- coordinator:posture:start -->"
    posture_marker_end = "<!-- coordinator:posture:end -->"

    assert sel.MARKER_START != sel.MARKER_END
    assert sel.MARKER_START != posture_marker_start
    assert sel.MARKER_END != posture_marker_end
    assert sel.MARKER_START != posture_marker_end
    assert sel.MARKER_END != posture_marker_start


def _doc(**overrides):
    document = {
        "source_repo": "claude-klabauter",
        "source_sha": "0" * 40,
        "guard_population": "guard_roster() + discover_guard_names()",
        "complete_over_guards": True,
        "guards": [
            {
                "guard_id": "block-stash-destruction",
                "cloud_verdict": "premise-false",
                "enforces_rule_ids": ["rcr-aaaaaaaa"],
                "uncertain_rule_ids": [],
            },
            {
                "guard_id": "bump_out_of_repo_tool_write",
                "cloud_verdict": "premise-holds",
                "enforces_rule_ids": ["rcr-bbbbbbbb"],
                "uncertain_rule_ids": ["rcr-cccccccc"],
            },
        ],
    }
    document.update(overrides)
    return document


def _write_join(tmp_path, document) -> str:
    target = tmp_path / "join.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    return str(target)


def test_absent_join_resolves_nothing(tmp_path):
    assert join_mod.load_join(str(tmp_path / "nothing-here.yaml")) is None


def test_an_empty_join_is_refused_outright():
    for completeness in (True, False):
        with pytest.raises(join_mod.JoinError) as caught:
            join_mod.GuardEnforcementJoin(_doc(guards=[], complete_over_guards=completeness))
        assert "zero guard rows" in str(caught.value)


def test_an_incomplete_join_resolves_nothing():
    incomplete = join_mod.GuardEnforcementJoin(_doc(complete_over_guards=False))
    assert incomplete.resolve("rcr-unnamed") is None, (
        "an incomplete join must answer unresolved, never 'no guard enforces this'"
    )


def test_completeness_must_be_a_real_boolean():
    for bad in ("false", "partial", 1, None):
        with pytest.raises(join_mod.JoinError) as caught:
            join_mod.GuardEnforcementJoin(_doc(complete_over_guards=bad))
        assert "real boolean" in str(caught.value)


def test_a_truncated_delivery_is_refused_against_its_own_count():
    doc = _doc()
    doc["counts"] = {"guards": len(doc["guards"]) + 1}
    with pytest.raises(join_mod.JoinError) as caught:
        join_mod.GuardEnforcementJoin(doc)
    assert "truncated" in str(caught.value)


def test_a_complete_join_licenses_the_negative():
    complete = join_mod.GuardEnforcementJoin(_doc())
    assert complete.resolve("rcr-unnamed") == ("none", "n/a")


def test_a_named_guard_keeps_its_rule_present():
    complete = join_mod.GuardEnforcementJoin(_doc())
    guard, verdict = complete.resolve("rcr-bbbbbbbb")
    assert guard == "bump_out_of_repo_tool_write" and verdict == "premise-holds"


@pytest.mark.parametrize(
    "document, fragment",
    [
        (_doc(source_repo=""), "source_repo"),
        (_doc(source_sha=""), "source_sha"),
        (
            _doc(guards=[{"guard_id": "g", "cloud_verdict": "premise-false", "uncertain_rule_ids": []}]),
            "enforces_rule_ids",
        ),
        (
            _doc(guards=[{"guard_id": "g", "cloud_verdict": "premise-false", "enforces_rule_ids": []}]),
            "uncertain_rule_ids",
        ),
        (
            _doc(guards=[{"guard_id": "g", "enforces_rule_ids": [], "uncertain_rule_ids": []}]),
            "cloud_verdict",
        ),
        (
            _doc(guards=[{"guard_id": "none", "cloud_verdict": "x", "enforces_rule_ids": [], "uncertain_rule_ids": []}]),
            "reserved",
        ),
    ],
)
def test_a_malformed_join_raises_rather_than_degrading_to_unresolved(document, fragment):
    with pytest.raises(join_mod.JoinError) as caught:
        join_mod.GuardEnforcementJoin(document)
    assert fragment in str(caught.value)


def test_a_guard_with_an_empty_rule_list_is_not_the_same_as_an_absent_key():
    document = _doc(
        guards=[
            {
                "guard_id": "g",
                "cloud_verdict": "premise-false",
                "enforces_rule_ids": [],
                "uncertain_rule_ids": [],
            }
        ]
    )
    parsed = join_mod.GuardEnforcementJoin(document)
    assert parsed.guard_count == 1 and parsed.enforced_rule_count == 0


def test_an_uncertain_rule_never_resolves_to_unenforced():
    parsed = join_mod.GuardEnforcementJoin(_doc())
    assert parsed.resolve("rcr-cccccccc") is None
    assert parsed.uncertain_rule_count == 1
    assert parsed.resolve("rcr-never-mentioned") == ("none", "n/a"), (
        "the uncertain list must narrow the negative, not abolish it"
    )


def test_uncertainty_on_one_guard_covers_a_rule_no_other_guard_named():
    document = _doc(
        guards=[
            {
                "guard_id": "voiced-the-doubt",
                "cloud_verdict": "premise-false",
                "enforces_rule_ids": [],
                "uncertain_rule_ids": ["rcr-doubted"],
            },
            {
                "guard_id": "silent-about-it",
                "cloud_verdict": "premise-holds",
                "enforces_rule_ids": ["rcr-other"],
                "uncertain_rule_ids": [],
            },
        ]
    )
    assert join_mod.GuardEnforcementJoin(document).resolve("rcr-doubted") is None


def test_default_join_path_is_the_repo_root_state_audits():
    """claude-klabauter's own DEFAULT_JOIN_PATH names this repo's committed join
    (rebased from the DoE original's copy-of-a-copy path -- see this
    module's own module docstring)."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    expected = repo_root / "state" / "audits" / "2026-09-07-guard-enforcement-join"
    assert Path(join_mod.DEFAULT_JOIN_PATH).parent == expected, join_mod.DEFAULT_JOIN_PATH


def test_delivered_join_parses():
    """claude-klabauter's join is delivered (it is this repo's own emitted artifact,
    not an awaited cross-repo copy) -- unlike the DoE original this is not
    skip-if-absent."""
    from pathlib import Path

    delivered = Path(join_mod.DEFAULT_JOIN_PATH)
    assert delivered.is_file(), f"expected a committed join at {delivered}"
    parsed = join_mod.load_join()
    assert parsed is not None and parsed.guard_count > 0


def test_premise_false_is_not_treated_as_a_guard_that_does_not_fire():
    document = _doc(
        guards=[
            {
                "guard_id": "nudge_windows_subprocess_popup",
                "cloud_verdict": "premise-false",
                "enforces_rule_ids": ["rcr-x"],
                "uncertain_rule_ids": [],
            }
        ]
    )
    guard, _verdict = join_mod.GuardEnforcementJoin(document).resolve("rcr-x")
    assert guard == "nudge_windows_subprocess_popup"
    assert guard != "none", "a premise-false guard must not read as 'nothing enforces this'"
