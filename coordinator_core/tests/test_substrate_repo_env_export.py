"""
Tests for coordinator_core.install.substrate's Python port of
claude-machine-local.sh's key-normalization + export-resolution loop.

Port source: coordinator/templates/bin/claude-machine-local.sh [coordinator-claude
repo]. The .sh remains sourced-only (unported, unchanged) — these tests
cover the standalone Python helper only, not a trampoline.
"""
from coordinator_core.install.substrate import (
    repo_key_to_env_var,
    resolve_repo_env_exports,
)


def test_repo_key_to_env_var_dots_and_dashes():
    assert repo_key_to_env_var("repos.example-retrieval-repo") == "REPO_EXAMPLE_RETRIEVAL_REPO"
    assert repo_key_to_env_var("repos.foo.bar") == "REPO_FOO_BAR"
    assert repo_key_to_env_var("repos.plain") == "REPO_PLAIN"


def test_resolve_exports_rc0_resolved():
    def getter(key):
        return (0, "/Users/alice/X/example-retrieval-repo")

    exports, warnings, errors = resolve_repo_env_exports(
        ["repos.example-retrieval-repo"], getter, preexisting_env={}
    )
    assert exports == {"REPO_EXAMPLE_RETRIEVAL_REPO": "/Users/alice/X/example-retrieval-repo"}
    assert warnings == []
    assert errors == []


def test_resolve_exports_rc1_clean_absence_not_exported():
    def getter(key):
        return (1, "")

    exports, warnings, errors = resolve_repo_env_exports(
        ["repos.missing"], getter, preexisting_env={}
    )
    assert exports == {}
    assert errors == []
    assert len(warnings) == 1
    assert "not resolved by ladder" in warnings[0]
    # Negative-spec: never exports "" for a clean absence (would corrupt
    # "$REPO_X/subdir" path joins to "/subdir").
    assert "REPO_MISSING" not in exports


def test_resolve_exports_rc_ge2_is_operational_error():
    def getter(key):
        return (2, "")

    exports, warnings, errors = resolve_repo_env_exports(
        ["repos.broken"], getter, preexisting_env={}
    )
    assert exports == {}
    assert warnings == []
    assert len(errors) == 1
    assert "rc=2" in errors[0]


def test_resolve_exports_idempotency_gate_honours_preexisting():
    def getter(key):  # pragma: no cover - must not be called
        raise AssertionError("getter should not be called when pre-set")

    exports, warnings, errors = resolve_repo_env_exports(
        ["repos.example-retrieval-repo"],
        getter,
        preexisting_env={"REPO_EXAMPLE_RETRIEVAL_REPO": "/already/set/override"},
    )
    assert exports == {}
    assert warnings == []
    assert errors == []


def test_resolve_exports_skips_non_conformant_identifier():
    def getter(key):  # pragma: no cover - must not be called
        raise AssertionError("getter should not be called for bad identifier")

    exports, warnings, errors = resolve_repo_env_exports(
        ["repos.foo@bar"], getter, preexisting_env={}
    )
    assert exports == {}
    assert errors == []
    assert len(warnings) == 1
    assert "non-conformant shell identifier" in warnings[0]
