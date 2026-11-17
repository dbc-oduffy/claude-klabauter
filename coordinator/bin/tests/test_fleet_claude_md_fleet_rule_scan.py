"""Tests for fleet-claude-md-fleet-rule-scan.py against a contract, hook and CLAUDE.md fixtures built under tmp_path."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "fleet-claude-md-fleet-rule-scan.py"
_spec = importlib.util.spec_from_file_location("fleet_claude_md_fleet_rule_scan", _SRC)
assert _spec is not None and _spec.loader is not None
scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan)

_HOME = "coordinator/docs/wiki/cross-repo-communication/cross-repo-memo-lifecycle.md"
_STALE_LINE = "Memos are not delivered until the PM relays the receiver path."
_RESTATED_LINE = "Memos go through the `cross-repo-memo` CLI and stay open until the receiver closes them."

_HOOK_SRC = '''\
CLAUDE_MD_RELPATHS = ("CLAUDE.md", ".claude/CLAUDE.md")


def raw_repos():
    return {}


def fleet_repos(raw):
    """Existing directories among the registry values, de-duplicated by resolved path."""
    from pathlib import Path
    out, seen = {}, set()
    for key, val in raw.items():
        if isinstance(val, str) and Path(val).is_dir() and Path(val).resolve() not in seen:
            seen.add(Path(val).resolve())
            out[key] = Path(val)
    return out
'''


@pytest.fixture(autouse=True)
def contract(tmp_path_factory, monkeypatch):
    """A contract tree (retired claims, fleet-rule homes, hook, home file) wired into the scanner's constants."""
    root = tmp_path_factory.mktemp("contract")
    plugin = root / "coordinator"
    (plugin / "contract").mkdir(parents=True)
    (plugin / "hooks" / "scripts").mkdir(parents=True)
    home = root / _HOME
    home.parent.mkdir(parents=True)
    home.write_text("# cross-repo memo lifecycle\n", encoding="utf-8")
    retired = plugin / "contract" / "retired-claims.json"
    retired.write_text(json.dumps({"entries": [{
        "id": "memo-delivery-waits-on-pm-relay", "retired_by": "cross-repo-memo.md",
        "signatures": [r"not delivered until the PM relays", r"memo\b.*\bwithout PM-relay"]}]}), encoding="utf-8")
    homes = plugin / "contract" / "fleet-rule-homes.json"
    homes.write_text(json.dumps({"entries": [{
        "id": "memo-lifecycle", "home": _HOME,
        "signatures": [r"cross-repo-memo`? CLI"],
        "witness": "Route via the coordinator's `cross-repo-memo` CLI."}]}), encoding="utf-8")
    hook = plugin / "hooks" / "scripts" / "check-fleet-claude-md-handoff-convention.py"
    hook.write_text(_HOOK_SRC, encoding="utf-8")
    monkeypatch.setattr(scan, "PLUGIN_ROOT", plugin)
    monkeypatch.setattr(scan, "RETIRED_CLAIMS", retired)
    monkeypatch.setattr(scan, "FLEET_RULE_HOMES", homes)
    monkeypatch.setattr(scan, "_HOOK", hook)


@pytest.fixture
def fixtures(tmp_path):
    """CLAUDE.md files with the stale / restated line at the fixed line numbers the tests pin."""
    def build(name, line_no, text):
        path = tmp_path / name
        body = [f"filler {i}" for i in range(1, line_no)] + [text, "tail"]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        return path

    return {
        "addon-pre.md": build("addon-pre.md", 34, _STALE_LINE),
        "example-game-repo-pre.md": build("example-game-repo-pre.md", 31, _STALE_LINE),
        "addon-post.md": build("addon-post.md", 30, "Memos route through the cross-repo-memo skill."),
        "example-game-repo-post.md": build("example-game-repo-post.md", 30, _RESTATED_LINE),
    }


def _run(capsys, *argv):
    code = scan.main(list(argv))
    return code, capsys.readouterr()


@pytest.mark.parametrize("name,line", [("addon-pre.md", 34), ("example-game-repo-pre.md", 31)])
def test_pre_fix_fixture_is_stale(capsys, fixtures, name, line):
    code, out = _run(capsys, "--path", str(fixtures[name]))
    assert code == 1
    assert f"{name}:{line}: stale [memo-delivery-waits-on-pm-relay]" in out.out


@pytest.mark.parametrize("name", ["addon-post.md", "example-game-repo-post.md"])
def test_post_fix_fixture_passes(capsys, fixtures, name):
    code, out = _run(capsys, "--path", str(fixtures[name]))
    assert code == 0
    assert " stale " not in out.out


def test_example_game_repo_post_is_restated_but_exits_zero(capsys, fixtures):
    code, out = _run(capsys, "--path", str(fixtures["example-game-repo-post.md"]))
    assert code == 0
    assert "example-game-repo-post.md:30: restated [memo-lifecycle]" in out.out
    assert "point at coordinator/docs/wiki/cross-repo-communication/cross-repo-memo-lifecycle.md" in out.out


@pytest.mark.parametrize("home", [
    "coordinator/docs/wiki/cross-repo-communication/cross-repo-memo-lifecycle.md",
    "docs" + "/wiki/cross-repo-communication/cross-repo-memo-lifecycle.md",
])
def test_line_naming_the_home_is_a_pointer(tmp_path, capsys, home):
    f = tmp_path / "CLAUDE.md"
    f.write_text(f"Route via the coordinator's `cross-repo-memo` CLI; see {home}.\n", encoding="utf-8")
    code, out = _run(capsys, "--path", str(f))
    assert (code, out.out) == (0, "")


def test_crlf_is_normalised(tmp_path, capsys):
    f = tmp_path / "CLAUDE.md"
    f.write_bytes(b"ok\r\nare not delivered until the PM relays the receiver path.\r\n")
    code, out = _run(capsys, "--path", str(f))
    assert code == 1 and ":2: stale" in out.out


def test_stale_reported_even_with_legacy_site_covering_text(tmp_path, capsys, monkeypatch):
    retired = tmp_path / "retired.json"
    retired.write_text(json.dumps({"entries": [{
        "id": "x", "retired_by": "p.md", "signatures": ["foo bar"],
        "legacy_sites": [{"path": "CLAUDE.md", "excerpt": "foo bar", "reason": "r"}]}]}), encoding="utf-8")
    monkeypatch.setattr(scan, "RETIRED_CLAIMS", retired)
    f = tmp_path / "CLAUDE.md"
    f.write_text("foo bar\n", encoding="utf-8")
    code, out = _run(capsys, "--path", str(f))
    assert code == 1 and "stale [x]" in out.out


def test_every_homes_signature_matches_its_witness():
    data = json.loads(scan.FLEET_RULE_HOMES.read_text(encoding="utf-8"))
    for e in data["entries"]:
        assert (scan.PLUGIN_ROOT.parent / e["home"]).is_file()
        for sig in e["signatures"]:
            assert re.search(sig, e["witness"], re.I), (e["id"], sig)


@pytest.mark.parametrize("which", ["RETIRED_CLAIMS", "FLEET_RULE_HOMES"])
def test_malformed_contract_exits_2(tmp_path, capsys, monkeypatch, which):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(scan, which, bad)
    f = tmp_path / "CLAUDE.md"
    f.write_text("x\n", encoding="utf-8")
    code, out = _run(capsys, "--path", str(f))
    assert code == 2 and out.out == "" and "could not check" in out.err


_real_load_hook = scan._load_hook


def _fake_hook(raw):
    """The real hook module with only its registry read replaced."""
    def raw_repos():
        if isinstance(raw, Exception):
            raise raw
        return raw
    hook = _real_load_hook()
    hook.raw_repos = raw_repos
    return hook


def test_fleet_registry_failure_exits_2(capsys, monkeypatch):
    monkeypatch.setattr(scan, "_load_hook", lambda: _fake_hook(RuntimeError("no machine-local")))
    code, out = _run(capsys, "--fleet")
    assert code == 2 and "registry unreadable" in out.err


def test_fleet_names_skipped_keys(tmp_path, capsys, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
    (a / "CLAUDE.md").write_text("are not delivered until the PM relays the receiver path.\n", encoding="utf-8")
    raw = {"a": str(a), "a_alias": str(a), "b": str(b), "gone": str(tmp_path / "gone"), "num": 3}
    monkeypatch.setattr(scan, "_load_hook", lambda: _fake_hook(raw))
    code, out = _run(capsys, "--fleet")
    assert code == 1
    lines = out.out.splitlines()
    assert lines[0].startswith("a: CLAUDE.md:1: stale")
    assert lines[-1] == "skipped: a_alias, gone, num"


def test_repo_root_scans_claude_md_pair(tmp_path, capsys):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text("a memo without PM-relay is bad\n", encoding="utf-8")
    code, out = _run(capsys, "--repo-root", str(tmp_path))
    assert code == 1 and ".claude/CLAUDE.md:1: stale" in out.out


def test_missing_explicit_path_exits_2(tmp_path, capsys):
    code, out = _run(capsys, "--path", str(tmp_path / "nope.md"))
    assert code == 2 and "not a readable file" in out.err


def test_no_git_ancestor_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scan, "find_repo_root", lambda start: None)
    code, out = _run(capsys)
    assert code == 2
