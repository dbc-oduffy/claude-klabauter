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

import subprocess
import textwrap

import pytest

from coordinator_core import person_resolver, tracker_entities


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """Clean the process-lifetime git-config cache across tests, and stub
    ``_resolve_repo_root`` to a fixed sentinel so these Tier-T tests never
    spawn a real `git rev-parse` — repo-root-keying behaviour itself is
    covered by the dedicated collision-regression test below, which
    overrides this stub per-case."""
    person_resolver.reset_person_resolver_git_config_cache()
    monkeypatch.setattr(person_resolver, "_resolve_repo_root", lambda: "/fixture/repo")
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
    def _fake(key: str, cwd: str | None = None) -> str:
        return values.get(key, "")

    monkeypatch.setattr(person_resolver, "_git_config", _fake)


def test_pinned_signature_and_keys():
    assert person_resolver.ALIAS_BUNDLE_KEYS == (
        "github",
        "github_id",
        "display",
        "email",
        "contributor_slug",
    )


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

    # contributor_slug is a derived hash, not a tracker_entities.normalize_alias
    # namespace — it has no raw/normalized form to compare against here.
    for key in person_resolver.ALIAS_BUNDLE_KEYS:
        if key == "contributor_slug":
            continue
        assert key in result, f"expected {key!r} to resolve for this fixture"
        expected = tracker_entities.normalize_alias(key, raw_values[key])
        assert result[key] == expected, (
            f"person_resolver's normalization of {key!r} diverges from "
            f"tracker_entities.normalize_alias({key!r}, ...)"
        )


def test_git_config_cache_is_keyed_on_repo_root_not_collided(tmp_path, monkeypatch):
    """C7: the old cache keyed on ``key`` alone was a missing-key COLLISION
    under a process serving two different repos — the first repo's
    resolved `user.name` would leak into the second repo's resolution.
    Stub ``_resolve_repo_root`` to return two distinct roots across two
    calls (simulating a warm process's cwd changing between requests) with
    two distinct display names; each root must resolve and cache its OWN
    value."""
    # Two roots, repeated: resolve_operating_person calls _resolve_repo_root
    # twice per invocation (once via user.email, once via user.name), so
    # each simulated "request" needs its root to repeat before advancing.
    roots = iter(["/repo/one", "/repo/one", "/repo/two", "/repo/two"])
    monkeypatch.setattr(person_resolver, "_resolve_repo_root", lambda: next(roots))

    names = {"/repo/one": "Alice", "/repo/two": "Bob"}

    def _fake(key: str, cwd: str | None = None) -> str:
        if key == "user.name":
            return names.get(cwd, "")
        return ""

    monkeypatch.setattr(person_resolver, "_git_config", _fake)

    first = person_resolver.resolve_operating_person(home=tmp_path)
    second = person_resolver.resolve_operating_person(home=tmp_path)

    assert first["display"] == "Alice"
    assert second["display"] == "Bob"


def test_git_config_uncached_passes_cwd_through(monkeypatch):
    captured = {}

    def _fake_run(args, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(args, 0, stdout="v\n", stderr="")

    monkeypatch.setattr(person_resolver.subprocess, "run", _fake_run)
    person_resolver._git_config_uncached("user.email", cwd="/some/repo")
    assert captured["cwd"] == "/some/repo"


def test_git_config_cache_reused_across_calls(tmp_path, monkeypatch):
    calls = {"count": 0}

    def _fake(key: str, cwd: str | None = None) -> str:
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


# C1: contributor_slug pinned vectors, verified against example-cockpit-repo's own
# TypeScript (`src/lib/identity/contributor-id.ts`, their commit
# `e3d5726bd021b5ffe97b4148ad93ceba9ec95b8d`) via `npx tsx`, 2026-08-19.
# <!-- VERBATIM: these are measured values, not illustrative ones. -->
@pytest.mark.parametrize(
    "database_id,expected_slug",
    [
        (240204332, "67c9mio1h"),
        (12345, "ywv8h6znl"),
        (987654, "gkn32sma8"),
        (42, "8xgasyst2"),
        (1, "5woe8x06s"),
        (999999999, "0zdsdj8ag"),
    ],
)
def test_contributor_slug_pinned_vectors(database_id, expected_slug):
    assert person_resolver._derive_contributor_slug(database_id) == expected_slug


@pytest.mark.parametrize(
    "bad_id", [None, "not-an-int", 0, -1, float("nan"), float("inf"), True, False]
)
def test_contributor_slug_null_contract(bad_id):
    assert person_resolver._derive_contributor_slug(bad_id) is None


def test_contributor_slug_derived_in_bundle(tmp_path, monkeypatch):
    _patch_git_config(
        monkeypatch,
        {
            "user.email": "240204332+gh-fixture@users.noreply.github.com",
            "user.name": "Display Fixture",
        },
    )

    result = person_resolver.resolve_operating_person(home=tmp_path)

    assert result["contributor_slug"] == "67c9mio1h"


def test_contributor_slug_rename_invariant(tmp_path, monkeypatch):
    """AC2: the same databaseId with a different `github` handle in the
    bundle yields the same slug — the derivation depends only on the
    numeric id, never on the handle."""
    _patch_git_config(
        monkeypatch,
        {
            "user.email": "240204332+old-handle@users.noreply.github.com",
            "user.name": "Display Fixture",
        },
    )
    first = person_resolver.resolve_operating_person(home=tmp_path)

    person_resolver.reset_person_resolver_git_config_cache()
    _patch_git_config(
        monkeypatch,
        {
            "user.email": "240204332+new-handle@users.noreply.github.com",
            "user.name": "Display Fixture",
        },
    )
    second = person_resolver.resolve_operating_person(home=tmp_path)

    assert first["github"] == "old-handle"
    assert second["github"] == "new-handle"
    assert first["contributor_slug"] == second["contributor_slug"] == "67c9mio1h"


def test_contributor_slug_absent_when_github_id_unresolved(tmp_path, monkeypatch):
    _patch_git_config(
        monkeypatch,
        {
            "user.email": "email@fixture",
            "user.name": "Display Fixture",
        },
    )

    result = person_resolver.resolve_operating_person(home=tmp_path)

    assert "github_id" not in result
    assert "contributor_slug" not in result
