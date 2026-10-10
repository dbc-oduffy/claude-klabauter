"""Synthetic-tree tests for _session_census."""

from __future__ import annotations

from coordinator_core.bash_guards._heavy_admission_contract import ProcRow
from coordinator_core.bash_guards._session_census import heavy_roots, resolve_anchor, session_census


class _Prims:
    def __init__(self, rows):
        self._rows = rows

    def snapshot(self):
        return self._rows

    def alive(self, pid, ctime):
        return True

    def creation_time(self, pid):
        return None

    def parent(self, pid):
        return None

    def image_name(self, pid):
        return None


def R(pid, ppid, ctime, name):
    return ProcRow(pid, ppid, ctime, name)


def test_chain_through_wrappers_finds_claude():
    rows = [R(1, 0, 10, "claude.exe"), R(2, 1, 20, "bash.exe"), R(3, 2, 30, "cmd.exe"),
            R(4, 3, 40, "pwsh.exe"), R(5, 4, 50, "python.exe")]
    a = resolve_anchor({"pid": 5}, _Prims(rows))
    assert a is not None and a.pid == 1


def test_nested_claude_is_nearest_anchor():
    rows = [R(1, 0, 10, "claude.exe"), R(2, 1, 20, "bash"), R(3, 2, 30, "claude"),
            R(4, 3, 40, "python")]
    assert resolve_anchor({"pid": 4}, _Prims(rows)).pid == 3


def test_pid_reuse_hop_fails_closed():
    rows = [R(1, 0, 10, "claude.exe"), R(2, 1, 99, "bash"), R(3, 2, 30, "python")]
    assert resolve_anchor({"pid": 3}, _Prims(rows)) is None


def test_unresolvable_anchor_inputs():
    rows = [R(1, 0, 10, "systemd"), R(2, 1, 20, "python")]
    p = _Prims(rows)
    assert resolve_anchor({"pid": 2}, p) is None
    assert resolve_anchor({}, p) is None
    assert resolve_anchor({"pid": "x"}, p) is None
    assert resolve_anchor({"pid": 77}, p) is None
    assert resolve_anchor({"pid": 2}, _Prims(None)) is None


def test_census_counts_heavy_and_shells_and_stops_at_nested_claude():
    rows = [R(1, 0, 10, "claude.exe"), R(2, 1, 20, "bash"), R(3, 2, 30, "node.exe"),
            R(4, 1, 25, "pwsh"), R(5, 4, 26, "claude.exe"), R(6, 5, 40, "node.exe"),
            R(7, 1, 27, "conhost.exe")]
    p = _Prims(rows)
    c = session_census(rows[0], p)
    assert [r.pid for r in c.heavy] == [3]
    assert sorted(r.pid for r in c.shells) == [2, 4]


def test_mcp_and_language_servers_are_never_counted():
    # The live Windows shape: cmd-wrapped MCP, a native MCP exe, an LSP, and the Bash tool shell.
    rows = [R(1, 0, 10, "claude.exe"),
            R(2, 1, 11, "cmd.exe"), R(3, 2, 12, "node.exe"), R(4, 3, 13, "cmd.exe"), R(5, 4, 14, "node.exe"),
            R(6, 1, 15, "notebooklm-mcp.exe"), R(7, 6, 16, "python.exe"),
            R(8, 1, 17, "pyright-langserver.exe"), R(9, 8, 18, "python.exe"), R(10, 9, 19, "node.exe"),
            R(11, 1, 20, "node.exe"),
            R(12, 1, 21, "bash.exe"), R(13, 12, 22, "bash.exe"), R(14, 13, 23, "node.exe")]
    c = session_census(rows[0], _Prims(rows))
    assert [r.pid for r in c.heavy] == [14]
    assert [r.pid for r in c.shells] == [12]


def test_an_orphan_is_not_charged_to_the_session():
    rows = [R(1, 0, 10, "claude.exe"), R(8, 999, 50, "vitest"), R(9, 999, 5, "node"),
            R(10, 999, 60, "notepad")]
    c = session_census(rows[0], _Prims(rows))
    assert c.heavy == ()


def test_reused_pid_child_not_descended():
    rows = [R(1, 0, 10, "claude.exe"), R(2, 1, 20, "bash"), R(3, 2, 15, "node")]
    c = session_census(rows[0], _Prims(rows))
    assert c.heavy == ()


def test_exclude_pids_drops_own_wrapper():
    rows = [R(1, 0, 10, "claude.exe"), R(2, 1, 20, "bash"), R(3, 1, 21, "bash")]
    c = session_census(rows[0], _Prims(rows), exclude_pids={2})
    assert [r.pid for r in c.shells] == [3]


def test_unreadable_snapshot_returns_none():
    assert session_census(R(1, 0, 10, "claude"), _Prims(None)) is None


def test_orphan_trees_finds_a_fork_stub_orphan_holding_a_heavy_image():
    # The live Windows shape: the tool command's root bash has a parent (the fork stub) that exited.
    from coordinator_core.bash_guards._session_census import orphan_trees

    rows = [R(1, 0, 10, "claude.exe"), R(12, 999, 50, "bash.exe"), R(13, 12, 51, "bash.exe"),
            R(14, 13, 52, "node.exe"), R(15, 14, 53, "cmd.exe"), R(16, 15, 54, "node.exe"),
            R(20, 998, 60, "bash.exe"), R(21, 20, 61, "grep.exe")]
    trees = orphan_trees(rows, since_ctime=40)
    assert [t.root.pid for t in trees] == [12]
    assert trees[0].lead == "node"
    assert sorted(r.pid for r in trees[0].heavy) == [14, 16]


def test_orphan_trees_ignores_roots_older_than_since_and_live_parented_rows():
    from coordinator_core.bash_guards._session_census import orphan_trees

    rows = [R(1, 0, 10, "claude.exe"), R(2, 1, 20, "bash"), R(3, 2, 30, "node"), R(12, 999, 5, "node")]
    assert orphan_trees(rows, since_ctime=8) == []


def test_a_reused_pid_parent_makes_its_child_an_orphan():
    from coordinator_core.bash_guards._session_census import orphan_trees

    rows = [R(7, 0, 90, "explorer.exe"), R(8, 7, 50, "node.exe")]
    assert [t.root.pid for t in orphan_trees(rows, since_ctime=0)] == [8]


def test_claimed_heavy_counts_one_command_per_tree():
    from coordinator_core.bash_guards._session_census import claimed_heavy

    rows = [R(12, 999, 50, "bash.exe"), R(14, 12, 52, "node.exe"), R(16, 14, 54, "node.exe")]
    assert [r.pid for r in claimed_heavy(rows, {(12, 50)})] == [14]
    assert claimed_heavy(rows, set()) == []


def test_one_command_chain_counts_once():
    # pnpm -> tsx cli (node) -> node: one command, three heavy images.
    rows = [
        R(1, 0, 1, "claude"), R(2, 1, 2, "bash"),
        R(3, 2, 3, "pnpm"), R(4, 3, 4, "node"), R(5, 4, 5, "node"),
    ]
    assert [r.pid for r in heavy_roots(rows[2:], rows)] == [3]


def test_two_commands_count_twice():
    rows = [
        R(1, 0, 1, "claude"), R(2, 1, 2, "bash"), R(6, 1, 6, "bash"),
        R(3, 2, 3, "pnpm"), R(4, 3, 4, "node"), R(7, 6, 7, "vitest"),
    ]
    assert sorted(r.pid for r in heavy_roots([rows[3], rows[4], rows[5]], rows)) == [3, 7]


def test_a_heavy_launcher_above_the_anchor_does_not_zero_the_count():
    # npx -> claude -> bash -> pnpm -> node: the launcher is not part of the session.
    rows = [
        R(1, 0, 1, "npx"), R(2, 1, 2, "claude"), R(3, 2, 3, "bash"),
        R(4, 3, 4, "pnpm"), R(5, 4, 5, "node"),
    ]
    heavy = [rows[0]] + rows[3:]
    assert [r.pid for r in heavy_roots(heavy, rows)] == [1]
    assert sorted(r.pid for r in heavy_roots(heavy, rows, anchor_pid=2)) == [1, 4]
