"""
Tests for coordinator_core/person_resolver.py — Tier T.

This is a solo-user box: every live identity value resolves to the same
human, so a resolver that ignores its inputs and returns a constant passes
every real-world check. These tests therefore MUST drive synthetic fixtures
(a tmp_path hosts.yml + a monkeypatched `_git_config`), never the live box —
see the authoring plan's § The test-design constraint that makes this harder
than it looks.

Each case uses a DISTINCT sentinel value per field (github=gh-fixture,
display=Display Fixture, email=email@fixture, github_id=999) so a
CROSS-WIRED resolver (right-shaped value pulled from the wrong source) fails
these cases too, not only a naive constant-returner.
"""

from __future__ import annotations

import textwrap

import pytest

from coordinator_core import person_resolver, tracker_entities


@pytest.fixture(autouse=True)
def _reset_cache():
    person_resolver.reset_person_resolver_git_config_cache()
    yield
    person_resolver.reset_person_resolver_git_config_cache()


def _write_hosts_yml(home, user: str) -> None:
    gh_dir = home / ".config" / "gh"
    gh_dir.mkdir(parents=True, exist_ok=True)
    (gh_dir / "hosts.yml").write_text(
        textwrap.dedent(
            f"""\
            github.com:
                git_protocol: ssh
                users:
                    {user}:
                user: {user}
            """
        ),
        encoding="utf-8",
    )


def _patch_git_config(monkeypatch, values: dict[str, str]) -> None:
    def _fake(key: str) -> str:
        return values.get(key, "")

    monkeypatch.setattr(person_resolver, "_git_config", _fake)


def test_pinned_signature_and_keys():
    assert person_resolver.ALIAS_BUNDLE_KEYS == ("github", "github_id", "display", "email")


def test_both_sources_agree(tmp_path, monkeypatch):
    _write_hosts_yml(tmp_path, "gh-fixture")
    _patch_git_config(
        monkeypatch,
        {
            "user.email": "999+gh-fixture@users.noreply.github.com",
            "user.name": "Display Fixture",
        },
    )

    result = person_resolver.resolve_operating_person(home=tmp_path)

    assert result["github"] == "gh-fixture"
    assert result["github_id"] == "999"
    assert result["display"] == "Display Fixture"
    assert result["email"] == "999+gh-fixture@users.noreply.github.com".casefold()


def test_sources_disagree_hosts_yml_wins(tmp_path, monkeypatch):
    _write_hosts_yml(tmp_path, "hosts-yml-winner")
    _patch_git_config(
        monkeypatch,
        {
            "user.email": "999+noreply-loser@users.noreply.github.com",
            "user.name": "Display Fixture",
        },
    )

    result = person_resolver.resolve_operating_person(home=tmp_path)

    assert result["github"] == "hosts-yml-winner"
    # github_id resolves from the noreply parse only, regardless of the
    # github-handle disagreement.
    assert result["github_id"] == "999"


def test_hosts_yml_absent_noreply_fallback(tmp_path, monkeypatch):
    # no hosts.yml written at all
    _patch_git_config(
        monkeypatch,
        {
            "user.email": "999+gh-fixture@users.noreply.github.com",
            "user.name": "Display Fixture",
        },
    )

    result = person_resolver.resolve_operating_person(home=tmp_path)

    assert result["github"] == "gh-fixture"
    assert result["github_id"] == "999"


def test_noreply_absent_plain_email_github_unresolved(tmp_path, monkeypatch):
    _patch_git_config(
        monkeypatch,
        {
            "user.email": "email@fixture",
            "user.name": "Display Fixture",
        },
    )

    result = person_resolver.resolve_operating_person(home=tmp_path)

    assert "github" not in result
    assert "github_id" not in result
    assert result["display"] == "Display Fixture"
    assert result["email"] == "email@fixture"


def test_nothing_configured_empty_dict(tmp_path, monkeypatch):
    _patch_git_config(monkeypatch, {})

    result = person_resolver.resolve_operating_person(home=tmp_path)

    assert result == {}


def test_malformed_hosts_yml_degrades_to_fallback(tmp_path, monkeypatch):
    gh_dir = tmp_path / ".config" / "gh"
    gh_dir.mkdir(parents=True, exist_ok=True)
    (gh_dir / "hosts.yml").write_text("not: valid: yaml: [unterminated", encoding="utf-8")
    _patch_git_config(
        monkeypatch,
        {
            "user.email": "999+gh-fixture@users.noreply.github.com",
            "user.name": "Display Fixture",
        },
    )

    result = person_resolver.resolve_operating_person(home=tmp_path)

    assert result["github"] == "gh-fixture"
    assert result["github_id"] == "999"


def test_mixed_case_hosts_yml_resolves_casefolded(tmp_path, monkeypatch):
    _write_hosts_yml(tmp_path, "DBC-example-operator")
    _patch_git_config(monkeypatch, {})

    result = person_resolver.resolve_operating_person(home=tmp_path)

    assert result["github"] == "dbc-example-operator"


# Review: coordinator:code-reviewer / EM ruling — the mixed-case hosts.yml
# case (test_mixed_case_hosts_yml_resolves_casefolded above) does not
# exercise the noreply-fallback branch's casefold call, and that fixture's
# sentinel handle is already lowercase, so it cannot distinguish "casefolds"
# from "passes through unchanged". This drives a mixed-case handle through
# the noreply-parse leg specifically (no hosts.yml written) so a regression
# that stops casefolding `github` on the fallback path is caught.
def test_mixed_case_noreply_fallback_resolves_casefolded(tmp_path, monkeypatch):
    # no hosts.yml written at all — forces resolution through the
    # noreply-email fallback branch.
    _patch_git_config(
        monkeypatch,
        {
            "user.email": "999+DBC-example-operator@users.noreply.github.com",
            "user.name": "Display Fixture",
        },
    )

    result = person_resolver.resolve_operating_person(home=tmp_path)

    assert result["github"] == "dbc-example-operator"
    assert result["github_id"] == "999"


# Review: coordinator:code-reviewer / EM ruling — person_resolver's casefold
# set is a second, hardcoded decision independent of
# tracker_entities.normalize_alias's namespace split; nothing enforced the
# two stayed in agreement, and F1 (github_id.casefold(), since removed) is a
# demonstrated instance of them silently diverging. This test asserts
# agreement per-key, driven off ALIAS_BUNDLE_KEYS so a future namespace
# addition is covered automatically. Intentionally NOT a shared-helper
# extraction — a loudly-failing coupling test is the scoped fix; unifying
# the implementations is out of remit here.
def test_casefold_policy_matches_tracker_entities_normalize_alias(tmp_path, monkeypatch):
    mixed = "Mixed-CaseValue"
    _write_hosts_yml(tmp_path, mixed)
    _patch_git_config(
        monkeypatch,
        {
            "user.email": f"1+{mixed}@users.noreply.github.com",
            "user.name": mixed,
        },
    )

    result = person_resolver.resolve_operating_person(home=tmp_path)

    raw_values = {
        "github": mixed,
        "github_id": "1",
        "display": mixed,
        "email": f"1+{mixed}@users.noreply.github.com",
    }

    for key in person_resolver.ALIAS_BUNDLE_KEYS:
        assert key in result, f"expected {key!r} to resolve for this fixture"
        expected = tracker_entities.normalize_alias(key, raw_values[key])
        assert result[key] == expected, (
            f"person_resolver's normalization of {key!r} diverges from "
            f"tracker_entities.normalize_alias({key!r}, ...)"
        )


def test_git_config_cache_reused_across_calls(tmp_path, monkeypatch):
    calls = {"count": 0}

    def _fake(key: str) -> str:
        calls["count"] += 1
        if key == "user.name":
            return "Display Fixture"
        return ""

    monkeypatch.setattr(person_resolver, "_git_config", _fake)

    person_resolver.resolve_operating_person(home=tmp_path)
    person_resolver.resolve_operating_person(home=tmp_path)

    # user.name resolves successfully and is cached (1 read total across
    # both calls); user.email resolves empty every time, which is NOT
    # memoized by design (a failed resolution must not poison the cache),
    # so it re-reads on each call: 1 + 2 = 3 total.
    assert calls["count"] == 3
