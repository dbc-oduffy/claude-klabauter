from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

# spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_gate", _BIN_DIR / "percolate-gate.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _run_cli(args: list[str]):
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod.main(args)
    return rc, buf.getvalue()


def _make_percolate_root(tmp_path: Path, target: str, with_hooks: bool = True) -> tuple[Path, Path]:
    percolate_root = tmp_path / "percolate-root"
    setup_dir = percolate_root / "setup"
    setup_dir.mkdir(parents=True)

    source_dir = tmp_path / "source" / target
    source_dir.mkdir(parents=True)
    (source_dir / ".percolate-ignore").write_text("", encoding="utf-8")

    dest_dir = tmp_path / "dest" / target
    dest_dir.mkdir(parents=True)

    (setup_dir / "publish-targets.portable").write_text(
        f"{target}|mirror|{source_dir}|{dest_dir}\n", encoding="utf-8"
    )

    if with_hooks:
        for hook_point in ("pre-rsync", "post-rsync", "pre-ci"):
            (setup_dir / "percolate-hooks" / target / hook_point).mkdir(parents=True)

    return percolate_root, source_dir


def test_branch0_gate_configured(tmp_path):
    percolate_root, source_dir = _make_percolate_root(tmp_path, "alpha")
    rc, out = _run_cli(
        ["branch0-gate", "alpha", "--percolate-root", str(percolate_root)]
    )
    assert rc == 0
    assert out.strip() == f"CONFIGURED:{source_dir}"


def test_branch0_gate_missing_target_entry(tmp_path):
    percolate_root, _ = _make_percolate_root(tmp_path, "alpha")
    rc, out = _run_cli(
        ["branch0-gate", "not-registered", "--percolate-root", str(percolate_root)]
    )
    assert rc == 1
    assert "MISSING_TARGET_ENTRY" in out


def _make_multi_row_root(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    percolate_root = tmp_path / "percolate-root"
    setup_dir = percolate_root / "setup"
    setup_dir.mkdir(parents=True)

    lines = []
    for target, dest in rows:
        source_dir = tmp_path / "source" / target
        source_dir.mkdir(parents=True)
        (source_dir / ".percolate-ignore").write_text("", encoding="utf-8")
        lines.append(f"{target}|mirror|{source_dir}|{dest}")

    (setup_dir / "publish-targets.portable").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return percolate_root


_MIRROR_ROWS = [
    ("claude-klabauter", "X:/claude-klabauter"),
    ("claude-klabauter-bin", "X:/claude-klabauter/bin"),
    ("claude-klabauter-docs-install", "X:/claude-klabauter/docs/install"),
]


def test_branch0_gate_routes_a_multi_row_mirror_to_coordinator_publish(tmp_path):
    """The name of the MIRROR matches every row and no row is named for it, so
    the answer is `coordinator-publish`, not a round and not first-run setup.
    Regression for the live shape: `/percolate klabauter` against nine
    `claude-klabauter*` rows previously emitted a bare MISSING_TARGET_ENTRY and
    percolate-round then offered to re-register rows that already existed.

    Matching is INFIX, which is the whole point — `klabauter` is not a prefix
    of `claude-klabauter`, so a prefix test would not fire for the exact input
    this exists to answer."""
    percolate_root = _make_multi_row_root(tmp_path, _MIRROR_ROWS)
    rc, out = _run_cli(
        ["branch0-gate", "klabauter", "--percolate-root", str(percolate_root)]
    )
    assert rc == 1
    assert "MISSING_TARGET_ENTRY" in out
    route = [line for line in out.splitlines() if line.startswith("route:")]
    assert len(route) == 1, out
    assert "coordinator-publish" in route[0]
    assert route[0].rstrip().endswith("coordinator-publish klabauter")


def test_branch0_gate_route_names_a_partial_match_explicitly(tmp_path):
    rows = _MIRROR_ROWS + [("other-mirror-lib", "X:/other-mirror/lib")]
    percolate_root = _make_multi_row_root(tmp_path, rows)
    rc, out = _run_cli(
        ["branch0-gate", "klabauter", "--percolate-root", str(percolate_root)]
    )
    assert rc == 1
    route = next(line for line in out.splitlines() if line.startswith("route:"))
    assert "coordinator-publish claude-klabauter,claude-klabauter-bin," \
        "claude-klabauter-docs-install" in route
    assert "other-mirror-lib" not in route


def test_branch0_gate_does_not_route_across_separate_destinations(tmp_path):
    """Rows that match the name but publish to DIFFERENT mirrors are not one
    `coordinator-publish` job, so no route is offered — the operator gets the
    registered-names line and picks."""
    rows = [
        ("shared-name-alpha", "X:/mirror-one"),
        ("shared-name-beta", "X:/mirror-two"),
    ]
    percolate_root = _make_multi_row_root(tmp_path, rows)
    rc, out = _run_cli(
        ["branch0-gate", "shared-name", "--percolate-root", str(percolate_root)]
    )
    assert rc == 1
    assert not any(line.startswith("route:") for line in out.splitlines())
    assert "registered: shared-name-alpha, shared-name-beta" in out


def test_branch0_gate_names_the_single_near_miss(tmp_path):
    percolate_root = _make_multi_row_root(tmp_path, _MIRROR_ROWS)
    rc, out = _run_cli(
        ["branch0-gate", "docs-install", "--percolate-root", str(percolate_root)]
    )
    assert rc == 1
    assert "did you mean: claude-klabauter-docs-install" in out


def test_branch0_gate_typo_falls_through_to_registered_names(tmp_path):
    percolate_root = _make_multi_row_root(tmp_path, _MIRROR_ROWS)
    rc, out = _run_cli(
        ["branch0-gate", "claude-klabautr", "--percolate-root", str(percolate_root)]
    )
    assert rc == 1
    assert not any(line.startswith("route:") for line in out.splitlines())
    assert "did you mean" not in out
    assert "registered: claude-klabauter, claude-klabauter-bin" in out


def test_shares_one_destination_is_path_segment_aware():
    assert _mod._shares_one_destination(["X:/m", "X:/m/a", "X:/m/b/c"]) is True
    assert _mod._shares_one_destination(["X:/mirror", "X:/mirror-two"]) is False
    assert _mod._shares_one_destination([r"X:\m", "X:/m/a"]) is True
    assert _mod._shares_one_destination([]) is False


def test_shares_one_destination_is_case_insensitive():
    assert _mod._shares_one_destination(["X:/Foo", "x:/foo/a"]) is True
    assert _mod._shares_one_destination(["X:/Mirror", "x:/MIRROR-two"]) is False


def test_shares_one_destination_does_not_conflate_sharp_s():
    assert _mod._shares_one_destination(["X:/a\u00df", "X:/ass/sub"]) is False
    assert _mod._shares_one_destination(["X:/a\u00df", "X:/A\u00df/sub"]) is True


def test_branch0_gate_configured_with_hook_dirs_absent(tmp_path):
    """Regression (2026-07-24, extirpate-orphaned-claude-central-publish-shell
    chunk C1): the per-target pre-rsync/post-rsync/pre-ci hook subdirectories
    are vestigial now that the percolate engine consumes the declarative
    `percolate-store.yaml` instead. branch0-gate must return CONFIGURED for a
    valid target whose hook subdirs are absent -- only `publish-targets.portable`
    and `.percolate-ignore` are required."""
    percolate_root, source_dir = _make_percolate_root(tmp_path, "alpha", with_hooks=False)
    rc, out = _run_cli(
        ["branch0-gate", "alpha", "--percolate-root", str(percolate_root)]
    )
    assert rc == 0
    assert out.strip() == f"CONFIGURED:{source_dir}"
    assert "MISSING_HOOK_DIR" not in out


def test_branch0_gate_missing_ignore_file(tmp_path):
    percolate_root, source_dir = _make_percolate_root(tmp_path, "alpha")
    (source_dir / ".percolate-ignore").unlink()
    rc, out = _run_cli(
        ["branch0-gate", "alpha", "--percolate-root", str(percolate_root)]
    )
    assert rc == 1
    assert "MISSING_IGNORE" in out


def test_scan_secrets_high_hit_blocks(tmp_path):
    target_file = tmp_path / "leaky.md"
    token = "sk" + "-" + "abcdefghijklmnopqrstuvwx"
    target_file.write_text(f"here is a token: {token}\n", encoding="utf-8")
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 2
    assert "HIGH" in out
    assert "sk-a..." in out
    assert token not in out


def test_scan_secrets_medium_hit_does_not_block(tmp_path):
    target_file = tmp_path / "wiki.md"
    target_file.write_text(
        "See ~/.claude/tasks/3f9c2a7e-task-list for details.\n", encoding="utf-8"
    )
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 0
    assert "MEDIUM" in out
    assert "~/.claude/tasks/3f9c2a7e-task-list" in out
    assert "HIGH" in out and "(none)" in out


_IDENTITY_FIXTURE = (
    'PERSONAL_EXPECTED_PATTERNS=("codename-alpha")\n'
    'PERSONAL_REVIEW_PATTERNS=("codename-alpha")\n'
    'PERSONAL_ALLOW_TOKENS=("dbc-alpha")\n'
)


def test_scan_secrets_identity_per_repo_rung_present_no_note(tmp_path, monkeypatch):
    """Regression guard for the ladder itself: a populated per-repo
    setup/.percolate-identity must not trip the UNCONFIGURED NOTE, and the
    machine-local rung must not even be consulted when the per-repo rung
    already resolves."""
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "no-such-claude-home"))

    setup_dir = tmp_path / "setup"
    setup_dir.mkdir()
    identity_file = setup_dir / ".percolate-identity"
    identity_file.write_text(_IDENTITY_FIXTURE, encoding="utf-8")

    target_file = tmp_path / "clean.md"
    target_file.write_text("nothing interesting here\n", encoding="utf-8")
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(
        ["scan-secrets", "--files", str(file_list), "--identity-file", str(identity_file)]
    )
    assert rc == 0
    assert "UNCONFIGURED" not in out
    assert "machine-local rung" not in out


def test_scan_secrets_identity_machine_local_rung_present_no_note(tmp_path, monkeypatch):
    """AC (the regression this stub exists to fix): the per-repo rung is
    absent but the machine-local rung resolves and is populated -- the
    UNCONFIGURED NOTE must NOT fire. `COORDINATOR_SETTINGS_HOME` is pointed
    at an isolated tmp_path fixture so this never depends on the real
    ~/.coordinator-claude-settings/.percolate-identity on the running
    machine."""
    settings_home = tmp_path / "settings-home"
    settings_home.mkdir()
    machine_local_identity = settings_home / ".percolate-identity"
    machine_local_identity.write_text(_IDENTITY_FIXTURE, encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    setup_dir = tmp_path / "setup"
    setup_dir.mkdir()
    identity_file = setup_dir / ".percolate-identity"

    target_file = tmp_path / "clean.md"
    target_file.write_text("nothing interesting here\n", encoding="utf-8")
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(
        ["scan-secrets", "--files", str(file_list), "--identity-file", str(identity_file)]
    )
    assert rc == 0
    assert "UNCONFIGURED" not in out
    assert str(machine_local_identity) in out
    assert "machine-local rung" in out


def test_scan_secrets_identity_both_rungs_absent_note_fires(tmp_path, monkeypatch):
    settings_home = tmp_path / "settings-home"
    settings_home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    setup_dir = tmp_path / "setup"
    setup_dir.mkdir()
    identity_file = setup_dir / ".percolate-identity"

    target_file = tmp_path / "clean.md"
    target_file.write_text("nothing interesting here\n", encoding="utf-8")
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(
        ["scan-secrets", "--files", str(file_list), "--identity-file", str(identity_file)]
    )
    assert rc == 0
    assert "UNCONFIGURED" in out
    assert "populate PERSONAL_REVIEW_PATTERNS" in out


def test_tier_medium_python_decorator_not_classified_as_email(tmp_path):
    target_file = tmp_path / "test_something.py"
    target_file.write_text(
        "@pytest.mark.parametrize(\"x\", [1, 2])\n"
        "def test_x(x):\n"
        "    pass\n"
        "\n"
        "# contact: real.person@some-real-domain.io\n",
        encoding="utf-8",
    )
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 0
    assert "@pytest.mark.parametrize" not in out
    assert "real.person@some-real-domain.io" in out


def test_tier_medium_reserved_test_domain_exempted(tmp_path):
    target_file = tmp_path / "test_fixture.py"
    target_file.write_text(
        '_git(repo, "config", "user.email", "test@example.com")\n'
        '_git(repo, "config", "user.email", "test@notexempt-domain.io")\n',
        encoding="utf-8",
    )
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 0
    assert "test@example.com" not in out
    assert "test@notexempt-domain.io" in out


def test_tier_medium_reserved_domain_exemption_is_suffix_anchored(tmp_path):
    target_file = tmp_path / "test_fixture.py"
    target_file.write_text(
        '_git(repo, "config", "user.email", "user@test-domain.com")\n'
        '_git(repo, "config", "user.email", "user@invalid-corp.io")\n'
        '_git(repo, "config", "user.email", "user@localhost.internal.io")\n'
        '_git(repo, "config", "user.email", "user@foo.test")\n'
        '_git(repo, "config", "user.email", "user@sub.example.test")\n'
        '_git(repo, "config", "user.email", "user@bar.invalid")\n',
        encoding="utf-8",
    )
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 0
    assert "user@test-domain.com" in out
    assert "user@invalid-corp.io" in out
    assert "user@localhost.internal.io" in out
    assert "user@foo.test" not in out
    assert "user@sub.example.test" not in out
    assert "user@bar.invalid" not in out


_GATING_HEADER = "MEDIUM (identity / internal paths / peer-repo names -- surfaces to gate):"


def _gating_panel(out: str) -> str:
    return out.split(_GATING_HEADER)[1].split("LOW (")[0]


def test_tier_medium_placeholders_and_marked_paths_do_not_gate(tmp_path):
    target_file = tmp_path / "docstrings.py"
    target_file.write_text(
        "renders `X:/a` as `X:\\a`\n"
        "drive letter (`\"C:/foo\"`, `\"C:foo\"`)\n"
        "POSIX `/x` or Windows `X:\\x` / `X:/x`\n"
        "``~/.claude/projects/<slug>/`` naming\n"
        "``~/.claude/projects/<mangled-repo-path>/<session-id>.jsonl``\n"
        '_real_git(["config", "user.email", "t@t.example"], repo)\n'
        'detect("C:/home/${USER}/src")  # abs-path-ok: synthetic test fixture\n'
        "12345678+<handle>@users.noreply.github.com\n",
        encoding="utf-8",
    )
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 0
    assert _gating_panel(out).strip() == "(none)"


def test_tier_medium_concrete_paths_and_identities_still_gate(tmp_path):
    lines = [
        "moved to `X:/claude-klabauter`",
        'registry_set("repos.k", "/x/claude-klabauter")',
        "see ~/.claude/projects/X--claude-klabauter/memory",
        "#   240204332+real-handle@users.noreply.github.com",
        "at C:/work/repo  # abs-path-ok:",
        "user real.person@some-real-domain.io  # abs-path-ok: path marker only",
        "`C:/foo` versus `C:/work`",
    ]
    target_file = tmp_path / "leaks.py"
    target_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 0
    panel = _gating_panel(out)
    for line in lines:
        assert line in panel
    assert sum(1 for row in panel.splitlines() if row.strip() and row.strip() != "(none)") == len(lines)


def test_tier_medium_interior_root_and_forge_service_addresses_do_not_gate(tmp_path):
    target_file = tmp_path / "shapes.py"
    target_file.write_text(
        '`"delete refs/x/old blobsha"` or `"create refs/x/new blobsha"`\n'
        "hosts = ['git@github.com', 'git@gitlab.com']\n"
        "    subprocess.run(['ssh', '-T', 'git@bitbucket.org'])\n",
        encoding="utf-8",
    )
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 0
    assert _gating_panel(out).strip() == "(none)"


def test_tier_medium_real_identities_and_rooted_paths_still_gate(tmp_path):
    lines = [
        "contact someone@company.com for access",
        "author someone@github.com owns it",
        "remote git@gitlab.internal-corp.io:team/repo.git",
        "clone into X:/real-internal-path",
        'registry_set("repos.k", "/x/real-internal-path")',
    ]
    target_file = tmp_path / "leaks.py"
    target_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 0
    panel = _gating_panel(out)
    for line in lines:
        assert line in panel
    assert sum(1 for row in panel.splitlines() if row.strip() and row.strip() != "(none)") == len(lines)


def test_tier_medium_placeholder_under_an_extension_does_not_gate(tmp_path):
    target_file = tmp_path / "fixtures.py"
    target_file.write_text(
        'assert _compose("/x/y.md", is_named=True) == _compose("/x/y.md")\n'
        'bodies = {"sentinel": _compose("/x/y.md")}\n'
        'assert body.endswith(PREFIX + "/x/y.md")\n'
        'provenance("/x/y.py", root)\n'
        'detect("/x/foo.txt")\n'
        "walk(`/x/<slug>.md`)\n",
        encoding="utf-8",
    )
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 0
    assert _gating_panel(out).strip() == "(none)"


def test_tier_medium_real_stem_under_an_extension_still_gates(tmp_path):
    lines = [
        'registry_set("repos.k", "/x/claude-klabauter")',
        'archived to "/x/cross-repo/archive/a.md"',
        'open("/x/notes.md")',
        'open("/x/real-internal-path.tar.gz")',
        'open("/x/session-id.jsonl")',
    ]
    target_file = tmp_path / "leaks.py"
    target_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 0
    panel = _gating_panel(out)
    for line in lines:
        assert line in panel
    assert sum(1 for row in panel.splitlines() if row.strip() and row.strip() != "(none)") == len(lines)


def test_scan_secrets_clean(tmp_path):
    target_file = tmp_path / "clean.md"
    target_file.write_text("nothing sensitive here\n", encoding="utf-8")
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    rc, out = _run_cli(["scan-secrets", "--files", str(file_list)])
    assert rc == 0
    assert out.count("(none)") == 3


def test_scan_secrets_peer_repo_extension(tmp_path):
    target_file = tmp_path / "mentions.md"
    target_file.write_text("cross-reference example-retrieval-repo here\n", encoding="utf-8")
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(target_file) + "\n", encoding="utf-8")

    registry = tmp_path / "repo-registry.md"
    registry.write_text(
        "- shortname: example-retrieval-repo\n  path: /x/example-retrieval-repo\n"
        "- shortname: claude-klabauter\n  path: /x/claude-klabauter\n",
        encoding="utf-8",
    )

    rc, out = _run_cli(
        [
            "scan-secrets",
            "--files",
            str(file_list),
            "--peer-repos-file",
            str(registry),
            "--target",
            "coordinator-claude",
        ]
    )
    assert rc == 0
    assert "project-rag" in out.split("MEDIUM")[1]


def _init_dest_repo(tmp_path: Path) -> Path:
    dest = tmp_path / "dest"
    dest.mkdir()
    subprocess.run(["git", "init", str(dest)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(dest), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(dest), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    return dest


def test_inverse_drift_marker_mode_detects_commit(tmp_path):
    dest = _init_dest_repo(tmp_path)
    tracked = dest / "file.md"
    tracked.write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(dest), "add", "file.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(dest), "commit", "-m", "initial"], check=True, capture_output=True
    )
    marker_sha = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    tracked.write_text("v2 hand-fixed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(dest), "add", "file.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(dest), "commit", "-m", "dest-side hand fix"],
        check=True,
        capture_output=True,
    )

    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup" / "percolate-state").mkdir(parents=True)
    (percolate_root / "setup" / "percolate-state" / "alpha.lastsync").write_text(
        marker_sha, encoding="utf-8"
    )

    file_list = tmp_path / "files.txt"
    file_list.write_text(str(tracked) + "\n", encoding="utf-8")

    rc, out = _run_cli(
        [
            "inverse-drift",
            "alpha",
            "--percolate-root",
            str(percolate_root),
            "--dest",
            str(dest),
            "--files",
            str(file_list),
        ]
    )
    assert rc == 0
    assert "anchor_mode: marker" in out
    assert "dest-side hand fix" in out


def test_inverse_drift_marker_stale_falls_back(tmp_path):
    dest = _init_dest_repo(tmp_path)
    tracked = dest / "file.md"
    tracked.write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(dest), "add", "file.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(dest), "commit", "-m", "initial"], check=True, capture_output=True
    )

    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup" / "percolate-state").mkdir(parents=True)
    (percolate_root / "setup" / "percolate-state" / "alpha.lastsync").write_text(
        "not-a-real-ref-anywhere", encoding="utf-8"
    )

    file_list = tmp_path / "files.txt"
    file_list.write_text(str(tracked) + "\n", encoding="utf-8")

    rc, out = _run_cli(
        [
            "inverse-drift",
            "alpha",
            "--percolate-root",
            str(percolate_root),
            "--dest",
            str(dest),
            "--files",
            str(file_list),
        ]
    )
    assert rc == 0
    assert "anchor_mode: marker-stale" in out


def test_inverse_drift_no_marker_30day_fallback_no_hits(tmp_path):
    dest = _init_dest_repo(tmp_path)
    tracked = dest / "file.md"
    tracked.write_text("v1\n", encoding="utf-8")

    percolate_root = tmp_path / "percolate-root"
    file_list = tmp_path / "files.txt"
    file_list.write_text(str(tracked) + "\n", encoding="utf-8")

    rc, out = _run_cli(
        [
            "inverse-drift",
            "alpha",
            "--percolate-root",
            str(percolate_root),
            "--dest",
            str(dest),
            "--files",
            str(file_list),
        ]
    )
    assert rc == 0
    assert "anchor_mode: 30day-fallback" in out
    assert "Inverse drift" not in out


def _make_multi_target_percolate_root(tmp_path: Path) -> Path:
    percolate_root = tmp_path / "percolate-root"
    setup_dir = percolate_root / "setup"
    setup_dir.mkdir(parents=True)

    rows = []
    for name in ("alpha", "beta", "gamma"):
        source_dir = tmp_path / "source" / name
        source_dir.mkdir(parents=True)
        dest_dir = tmp_path / "dest" / name
        dest_dir.mkdir(parents=True)
        rows.append(f"{name}|mirror|{source_dir}|{dest_dir}")

    (setup_dir / "publish-targets.portable").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return percolate_root


def test_list_targets_no_filter_lists_all_names_in_order(tmp_path):
    percolate_root = _make_multi_target_percolate_root(tmp_path)

    rc, out = _run_cli(["list-targets", "--percolate-root", str(percolate_root)])

    assert rc == 0
    assert out.strip().splitlines() == ["alpha", "beta", "gamma"]


def test_list_targets_with_target_prints_only_dest_path(tmp_path):
    percolate_root = _make_multi_target_percolate_root(tmp_path)
    expected_dest = tmp_path / "dest" / "beta"

    rc, out = _run_cli(
        ["list-targets", "--percolate-root", str(percolate_root), "--target", "beta"]
    )

    assert rc == 0
    assert out.strip() == str(expected_dest)


def test_list_targets_unknown_target_exits_nonzero_no_stdout(tmp_path):
    percolate_root = _make_multi_target_percolate_root(tmp_path)

    rc, out = _run_cli(
        ["list-targets", "--percolate-root", str(percolate_root), "--target", "not-registered"]
    )

    assert rc == 1
    assert out == ""


def test_list_targets_no_targets_registered_errors_to_stderr(tmp_path):
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    err_buf = io.StringIO()
    out_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = _mod.main(["list-targets", "--percolate-root", str(percolate_root)])

    assert rc == 1
    assert out_buf.getvalue() == ""
    assert err_buf.getvalue() != ""


def test_resolve_root_bare_prints_path_only(monkeypatch, tmp_path):
    resolved = tmp_path / "some-root"
    resolved.mkdir()

    monkeypatch.setattr(
        "coordinator_core.percolate.runtime_root.coordinator_percolate_runtime_root_explained",
        lambda: (str(resolved), "repo-local-git"),
    )

    rc, out, err = _run_cli_capturing_stderr(["resolve-root"])
    assert rc == 0
    assert out.strip() == str(resolved)
    assert err == ""


def test_resolve_root_explain_prints_path_and_rung(monkeypatch, tmp_path):
    resolved = tmp_path / "some-root"
    resolved.mkdir()

    monkeypatch.setattr(
        "coordinator_core.percolate.runtime_root.coordinator_percolate_runtime_root_explained",
        lambda: (str(resolved), "doe-root-pointer"),
    )

    rc, out, err = _run_cli_capturing_stderr(["resolve-root", "--explain"])
    assert rc == 0
    assert out.strip() == f"{resolved}\tdoe-root-pointer"
    assert err == ""


def test_resolve_root_ladder_failure_writes_stderr_verbatim_no_stdout(monkeypatch):
    message = "coordinator_percolate_runtime_root: cannot resolve PERCOLATE_ROOT.\n  (details)"

    def _raise():
        raise RuntimeError(message)

    monkeypatch.setattr(
        "coordinator_core.percolate.runtime_root.coordinator_percolate_runtime_root_explained",
        _raise,
    )

    rc, out, err = _run_cli_capturing_stderr(["resolve-root"])
    assert rc == 1
    assert out == ""
    assert err.strip() == message


def _run_cli_capturing_stderr(args: list[str]):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = _mod.main(args)
    return rc, out.getvalue(), err.getvalue()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def drift_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "dest"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_git_log_batched_survives_a_pathspec_set_over_the_windows_cmdline_cap(
    drift_repo: Path,
) -> None:
    names = [f"file_{i:04d}_{'p' * 60}.py" for i in range(600)]
    for name in names:
        (drift_repo / name).write_text("x\n", encoding="utf-8")
    _git(drift_repo, "add", "-A")
    _git(drift_repo, "commit", "-qm", "bulk add")

    assert sum(len(n) + 1 for n in names) > 32767, "fixture must exceed the cap"

    base = _mod._drift_log_cmd_base(drift_repo)
    lines = _mod._git_log_batched(base, ["--since=30 days ago"], names).drift_lines

    assert len(lines) == 1
    assert "bulk add" in lines[0]


def test_git_log_batched_raises_instead_of_swallowing_a_git_failure(
    drift_repo: Path,
) -> None:
    base = _mod._drift_log_cmd_base(drift_repo)
    with pytest.raises(RuntimeError, match="git log failed"):
        _mod._git_log_batched(base, ["no-such-ref..HEAD"], ["seed.txt"])


_STAMP = " [source-head 0123456789ab]"


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit_file(repo: Path, name: str, body: str, subject: str) -> None:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", subject)


def _drift_cli(repo: Path, tmp_path: Path, anchor: str, names: List[str], *extra: str):
    percolate_root = tmp_path / "percolate-root"
    state = percolate_root / "setup" / "percolate-state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "alpha.lastsync").write_text(anchor, encoding="utf-8")
    file_list = tmp_path / "files.txt"
    file_list.write_text("".join(f"{repo / n}\n" for n in names), encoding="utf-8")
    return _run_cli(
        [
            "inverse-drift", "alpha",
            "--percolate-root", str(percolate_root),
            "--dest", str(repo),
            "--files", str(file_list),
            *extra,
        ]
    )


def test_inverse_drift_excludes_the_publishers_own_commits_but_reports_a_hand_edit(
    drift_repo: Path, tmp_path: Path
) -> None:
    anchor = _head(drift_repo)
    _commit_file(drift_repo, "seed.txt", "published v2\n", f"percolate publish: alpha (1 file(s)){_STAMP}")
    _commit_file(drift_repo, "other.txt", "hand fix\n", "dest-side hand fix")
    _commit_file(drift_repo, "seed.txt", "published v3\n", f"percolate: sync 1 path(s) to dest (alpha){_STAMP}")

    rc, out = _drift_cli(drift_repo, tmp_path, anchor, ["seed.txt", "other.txt"])

    assert rc == 0
    assert "dest-side hand fix" in out
    assert "percolate publish:" not in out
    assert "percolate: sync" not in out
    assert "not drift: 2 publisher commit(s), 0 hand commit(s)" in out


def test_inverse_drift_drops_a_hand_edit_a_later_publish_rewrote(
    drift_repo: Path, tmp_path: Path
) -> None:
    anchor = _head(drift_repo)
    _commit_file(drift_repo, "seed.txt", "hand edit, since overwritten\n", "overwritten hand edit")
    _commit_file(drift_repo, "live.txt", "hand edit, still live\n", "live hand edit")
    _commit_file(drift_repo, "seed.txt", "published\n", f"percolate publish: alpha (1 file(s)){_STAMP}")

    rc, out = _drift_cli(drift_repo, tmp_path, anchor, ["seed.txt", "live.txt"])

    assert rc == 0
    assert "live hand edit" in out
    assert "overwritten hand edit" not in out
    assert "not drift: 1 publisher commit(s), 1 hand commit(s)" in out


def test_inverse_drift_a_hand_edit_after_the_last_publish_is_still_drift(
    drift_repo: Path, tmp_path: Path
) -> None:
    anchor = _head(drift_repo)
    _commit_file(drift_repo, "seed.txt", "published\n", f"percolate publish: alpha (1 file(s)){_STAMP}")
    _commit_file(drift_repo, "seed.txt", "hand fix on top\n", "hand fix on top of a publish")

    rc, out = _drift_cli(drift_repo, tmp_path, anchor, ["seed.txt"])

    assert rc == 0
    assert "hand fix on top of a publish" in out


def test_inverse_drift_recognises_the_stamps_earlier_spelling(
    drift_repo: Path, tmp_path: Path
) -> None:
    anchor = _head(drift_repo)
    _commit_file(drift_repo, "seed.txt", "published\n", "percolate publish: alpha (1 file(s)) [source 0123456789ab]")

    rc, out = _drift_cli(drift_repo, tmp_path, anchor, ["seed.txt"])

    assert rc == 0
    assert "Inverse drift" not in out
    assert "not drift: 1 publisher commit(s)" in out


def test_inverse_drift_a_stamp_mid_subject_is_not_a_publisher_commit(
    drift_repo: Path, tmp_path: Path
) -> None:
    anchor = _head(drift_repo)
    _commit_file(drift_repo, "seed.txt", "hand\n", f"Revert \"percolate publish: alpha{_STAMP}\" by hand")

    rc, out = _drift_cli(drift_repo, tmp_path, anchor, ["seed.txt"])

    assert rc == 0
    assert "by hand" in out


def test_inverse_drift_json_verdict_counts_exclusions_and_lists_only_drift(
    drift_repo: Path, tmp_path: Path
) -> None:
    anchor = _head(drift_repo)
    _commit_file(drift_repo, "seed.txt", "published\n", f"percolate publish: alpha (1 file(s)){_STAMP}")
    _commit_file(drift_repo, "other.txt", "hand fix\n", "dest-side hand fix")
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "seed.txt").write_text("published\n", encoding="utf-8")
    (source_dir / "other.txt").write_text("source version\n", encoding="utf-8")

    rc, out = _drift_cli(
        drift_repo, tmp_path, anchor, ["seed.txt", "other.txt"],
        "--source-dir", str(source_dir), "--json",
    )

    assert rc == 0
    verdict = json.loads(out)
    assert verdict["commits"] == 1
    assert "dest-side hand fix" in verdict["commit_lines"][0]
    assert verdict["publisher_commits_excluded"] == 1
    assert verdict["superseded_commits_excluded"] == 0
    assert verdict["real_drift"] is True


def test_inverse_drift_maps_source_paths_onto_the_dest_tree(
    drift_repo: Path, tmp_path: Path
) -> None:
    (drift_repo / "seed.txt").write_text("changed in dest\n", encoding="utf-8")
    _git(drift_repo, "commit", "-qam", "dest-authored fix")

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "seed.txt").write_text("seed\n", encoding="utf-8")

    files_list = tmp_path / "files.txt"
    files_list.write_text(str(source_dir / "seed.txt"), encoding="utf-8")

    percolate_root = tmp_path / "root"
    (percolate_root / "setup" / "percolate-state").mkdir(parents=True)

    code, out, err = _run_cli_capturing_stderr([
        "inverse-drift", "some-target",
        "--percolate-root", str(percolate_root),
        "--dest", str(drift_repo),
        "--source-dir", str(source_dir),
        "--files", str(files_list),
    ])

    assert code == 0, err
    assert "dest-authored fix" in out, out


def test_inverse_drift_fails_loud_when_paths_resolve_against_nothing(
    drift_repo: Path, tmp_path: Path
) -> None:
    files_list = tmp_path / "files.txt"
    files_list.write_text(str(tmp_path / "elsewhere" / "orphan.py"), encoding="utf-8")

    percolate_root = tmp_path / "root"
    (percolate_root / "setup" / "percolate-state").mkdir(parents=True)

    code, _out, err = _run_cli_capturing_stderr([
        "inverse-drift", "no-such-target",
        "--percolate-root", str(percolate_root),
        "--dest", str(drift_repo),
        "--files", str(files_list),
    ])

    assert code == 1
    assert "--source-dir" in err
