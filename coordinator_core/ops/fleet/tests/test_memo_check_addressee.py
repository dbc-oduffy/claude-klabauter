"""
Tests for coordinator_core.ops.fleet.memo_check_addressee — memo.check_addressee
COMPUTE_ONLY UDS op.

Ratifying spinoff: state/handoffs/2026-07-21_184526_claude_klabauter-check-addressee-verb.md

Cover:
  - MATCH — self_root resolves to the same repo as `to`.
  - MISMATCH — self is repo A, `to` resolves to registered repo B.
  - UNRESOLVED + suggestion — `to` is a near-miss of a registered receiver
    (defect 2, GREEN).
  - setup errors — missing `to`; `dry_run: false`; `repo_root=None`.
  - defect-1 redirect-MATCH — `to` is a example-doctrine-repo redirect-alias literal that
    resolves (via the manifest's declared central receiver id) to the same
    repo as self. Example-doctrine-repo promoted `identity.redirectAliases` into the manifest
    2026-07-21; this is now a real passing assertion, not a gated xfail.

Harness: asyncio.run() in sync test functions — no pytest-asyncio dependency
(mirrors test_memo_list.py).

Spec backlink: state/handoffs/2026-07-21_184526_claude_klabauter-check-addressee-verb.md
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from coordinator_core.ops.fleet.memo_check_addressee import (
    _MODE,
    _memo_check_addressee,
    _validate_check_addressee_params,
)


def _run(result):
    """Run an async coroutine synchronously, or pass a plain (already
    computed) result through unchanged (2026-08-07: `_memo_check_addressee`
    is now plain `def`)."""
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _make_claude_home(tmp_path: Path, receiver_repos: dict) -> Path:
    """Minimal machine-local registry fixture (mirrors test_memo_list.py's factory).

    receiver_repos: {registry_key_suffix: repo_path_str} e.g. {"example_retrieval_repo": "/..."}
    -> writes "repos.example_retrieval_repo" = <path> in registry.local.toml.
    """
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    lines = []
    for key_suffix, repo_path in receiver_repos.items():
        toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return claude_home


def _registered_example_doctrine_repo(machine_local: Path) -> Path | None:
    """Return this fixture's `repos.example_doctrine_repo` path, or None if unregistered.

    Handles both spellings the fixtures use: the flat quoted-dotted key
    (`"repos.example_doctrine_repo" = "..."`) and the nested `[repos]` table.
    """
    import tomllib

    for fname in ("registry.local.toml", "registry.toml"):
        path = machine_local / fname
        if not path.is_file():
            continue
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            continue
        value = data.get("repos.example_doctrine_repo") or (data.get("repos") or {}).get("example_doctrine_repo")
        if value:
            return Path(str(value))
    return None


def _write_doe_manifest(
    claude_home: Path, tmp_path: Path, manifest: dict, doe_root: Path | None = None
) -> None:
    """Write a .doe-root sentinel + coordinator-registry.manifest.json fixture.

    Mirrors test_memo_send.py's `_make_doe_manifest`: the sentinel lands on the
    DR-071 ladder's durable rung (`<settings-home>/machine-local/.doe-root`),
    and the doe-root defaults to this fixture's own `repos.example_doctrine_repo` when
    registered — the ladder's canonical registry rung outranks the pointer file,
    and on a real machine the two agree.
    """
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    doe_root = (
        doe_root or _registered_example_doctrine_repo(machine_local) or (tmp_path / "doe-root")
    )
    schemas_dir = doe_root / "coordinator" / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    machine_local.mkdir(parents=True, exist_ok=True)
    (machine_local / ".doe-root").write_text(str(doe_root), encoding="utf-8")
    (schemas_dir / "coordinator-registry.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _git_common_dir(repo_root: Path) -> Path:
    """Init a minimal git repo at repo_root and return its .git common dir."""
    import subprocess

    subprocess.run(
        ["git", "init", "-b", "main"], cwd=str(repo_root), capture_output=True, check=True
    )
    return (repo_root / ".git").resolve()


# ===========================================================================
# 1. MATCH
# ===========================================================================

class TestMatch:
    def test_self_resolves_to_same_repo_as_to(self, tmp_path, monkeypatch):
        self_repo = tmp_path / "claude-klabauter"
        self_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"claude_klabauter": str(self_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        common_dir = _git_common_dir(self_repo)

        result = _run(
            _memo_check_addressee(
                {"dry_run": True, "to": "claude-klabauter-em"}, repo_root=common_dir
            )
        )

        assert result["exit_code"] == 0
        assert result["mode"] == _MODE
        candidate = result["candidates"][0]
        assert candidate["verdict"] == "MATCH"
        assert candidate["resolved"] is True
        assert candidate["self_repo"] == str(self_repo)
        assert candidate["to_repo"] == str(self_repo)


# ===========================================================================
# 2. MISMATCH
# ===========================================================================

class TestMismatch:
    def test_to_resolves_to_a_different_registered_repo(self, tmp_path, monkeypatch):
        self_repo = tmp_path / "claude-klabauter"
        other_repo = tmp_path / "example-retrieval-repo"
        self_repo.mkdir()
        other_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            {"claude_klabauter": str(self_repo), "example_retrieval_repo": str(other_repo)},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        common_dir = _git_common_dir(self_repo)

        result = _run(
            _memo_check_addressee(
                {"dry_run": True, "to": "example-retrieval-repo-em"}, repo_root=common_dir
            )
        )

        assert result["exit_code"] == 0
        candidate = result["candidates"][0]
        assert candidate["verdict"] == "MISMATCH"
        assert candidate["resolved"] is True
        assert candidate["self_repo"] == str(self_repo)
        assert candidate["to_repo"] == str(other_repo)


# ===========================================================================
# 3. UNRESOLVED + suggestion (defect 2, GREEN)
# ===========================================================================

class TestUnresolvedWithSuggestion:
    def test_near_miss_receiver_gets_did_you_mean_suggestion(self, tmp_path, monkeypatch):
        self_repo = tmp_path / "some-self-repo"
        self_repo.mkdir()
        claude_klabauter_repo = tmp_path / "claude-klabauter"
        claude_klabauter_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            {"some_self_repo": str(self_repo), "claude_klabauter": str(claude_klabauter_repo)},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        common_dir = _git_common_dir(self_repo)

        result = _run(
            _memo_check_addressee(
                {"dry_run": True, "to": "claude-klabauter-em"}, repo_root=common_dir
            )
        )

        assert result["exit_code"] == 0
        candidate = result["candidates"][0]
        assert candidate["verdict"] == "UNRESOLVED"
        assert candidate["resolved"] is False
        assert candidate["to_repo"] is None
        assert "Did you mean" in candidate["note"]
        assert "claude-klabauter-em" in candidate["note"]


# ===========================================================================
# 4. setup errors
# ===========================================================================

class TestSetupErrors:
    def test_missing_to_rejected(self):
        result = _validate_check_addressee_params({"dry_run": True})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_empty_to_rejected(self):
        result = _validate_check_addressee_params({"dry_run": True, "to": "   "})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_dry_run_false_rejected(self):
        result = _validate_check_addressee_params({"dry_run": False, "to": "some-em"})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_handler_dry_run_false_returns_setup_error(self, tmp_path, monkeypatch):
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(
            _memo_check_addressee({"dry_run": False, "to": "some-em"}, repo_root=tmp_path)
        )
        assert result["exit_code"] == 1
        assert result["dry_run"] is False

    def test_repo_root_none_returns_setup_error(self, tmp_path, monkeypatch):
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(
            _memo_check_addressee({"dry_run": True, "to": "some-em"}, repo_root=None)
        )
        assert result["exit_code"] == 1


# ===========================================================================
# 4b. resolver-exception -> setup-error mapping (Review: code-reviewer, Finding 2)
# ===========================================================================

class TestResolverExceptionMapping:
    """Cover the RegistryReadError/AmbiguousReceiverError -> setup-error mapping.

    Dispatch-brief correctness property #4: "Resolver-read failures fail loud,
    never fall back to a scan — RegistryReadError and AmbiguousReceiverError
    from resolve_receiver_inbox must both become setup-error envelopes." The
    handler code already implements this; these tests are the regression net
    the review found missing.
    """

    def test_corrupt_registry_returns_setup_error(self, tmp_path, monkeypatch):
        """A present-but-unparseable registry.toml -> RegistryReadError -> exit_code:1.

        Mirrors test_memo_resolver.py's TestReadRegistryReposFailLoud corrupt-file
        fixture, but exercised through the handler's own exception->envelope path.
        """
        claude_home = tmp_path / "claude-home"
        machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
        machine_local.mkdir(parents=True)
        # Invalid TOML: unterminated string.
        (machine_local / "registry.toml").write_text(
            'schema = 1\n"repos.broken" = "unterminated\n', encoding="utf-8"
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(
            _memo_check_addressee({"dry_run": True, "to": "some-em"}, repo_root=tmp_path)
        )
        assert result["exit_code"] == 1

    def test_ambiguous_central_receiver_returns_setup_error(self, tmp_path, monkeypatch):
        """Two distinct central ids fan in to two different registered repos ->
        AmbiguousReceiverError -> exit_code:1.

        Mirrors test_memo_resolver.py's TestAmbiguousCentralReceiver fixture; `to`
        is itself one of the ambiguous central ids.
        """
        claude_home = _make_claude_home(
            tmp_path,
            {
                "central": str(tmp_path / "central-repo"),
                "example_doctrine_repo": str(tmp_path / "example-doctrine-repo-repo"),
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        _write_doe_manifest(
            claude_home,
            tmp_path,
            {
                "identity": {
                    "centralReceiverIds": ["central-em", "example-doctrine-repo-em"],
                    "repoAliases": [],
                }
            },
        )

        result = _run(
            _memo_check_addressee(
                {"dry_run": True, "to": "central-em"}, repo_root=tmp_path
            )
        )
        assert result["exit_code"] == 1


# ===========================================================================
# 5. defect-1 redirect-MATCH — example-doctrine-repo promoted identity.redirectAliases 2026-07-21
# ===========================================================================

class TestRedirectMatchDefect1:
    def test_redirect_alias_matches_central_self(self, tmp_path, monkeypatch):
        """`to` is a example-doctrine-repo redirect alias literal; self resolves to the central repo.

        Manifest declares `identity.redirectAliases: ["coordinator-claude"]` (the
        field example-doctrine-repo promoted 2026-07-21) plus a SINGLE central receiver id,
        `"example-doctrine-repo-em"`. read_redirect_aliases() picks up the normalized `to`
        ("coordinator-claude"), so the redirect branch fires: it takes
        `sorted(central_ids)[0]` == `"example-doctrine-repo-em"` and resolves it through
        `resolve_receiver_inbox()`, which (via `convention_repo_key`) maps to
        registry key `repos.example_doctrine_repo` — registered here to the SAME repo as
        self. Two distinct repo paths (self_root from the git common_dir, to_root
        from the registry) that happen to be the same directory -> MATCH.
        Manifest-driven end to end: no alias or central-id literal is hardcoded
        in the handler, only read declaratively via read_redirect_aliases()/
        read_central_receiver_ids().
        """
        central_repo = tmp_path / "example-doctrine-repo-repo"
        central_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_doctrine_repo": str(central_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        _write_doe_manifest(
            claude_home,
            tmp_path,
            {
                "identity": {
                    # Single central id, so sorted(central_ids)[0] is unambiguous
                    # and maps (via convention_repo_key) to the one registered
                    # repos.example_doctrine_repo key above -> the redirect branch resolves
                    # to central_repo, the same repo as self.
                    "centralReceiverIds": ["example-doctrine-repo-em"],
                    "repoAliases": [],
                    "redirectAliases": ["coordinator-claude"],
                }
            },
        )

        common_dir = _git_common_dir(central_repo)

        result = _run(
            _memo_check_addressee(
                {"dry_run": True, "to": "coordinator-claude"}, repo_root=common_dir
            )
        )

        candidate = result["candidates"][0]
        assert candidate["verdict"] == "MATCH"
        assert candidate["resolved"] is True
        assert candidate["self_repo"] == str(central_repo)
        assert candidate["to_repo"] == str(central_repo)
