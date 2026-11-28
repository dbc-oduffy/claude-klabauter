"""Ratchet: no installer re-declares a host list; host_registry is the one place that names the hosts.

An installer offends when a tuple/list/set literal spells two or more distinct registry host ids (or
several sub-sequences each led by a host id, the `(("agy", ...), ("grok", ...))` shape), or a dict literal
keys two or more of them. A per-host dispatch map is legitimate only when named in SUBSETS. Single-id
constants (`HOST_CODEX = "codex"`) and binary names are names or data, not host lists.
Pure source scan: no shell-out, repo root resolved from this file.
"""
from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core.install import host_registry

REPO = Path(__file__).resolve().parents[3]

#: The five files that install a host. Adding an installer means adding it here.
INSTALLERS = (
    "scripts/setup.py",
    "coordinator/lib/install/coordinator_install.py",
    "coordinator/bin/unimatrix-install.py",
    "coordinator/bin/unimatrix-autolink.py",
    "coordinator_core/install/host_targets.py",
)

#: Literals that name a host subset by capability, not the host list. Keyed (file, ast.unparse text) so a
#: line shift never stales an entry; an entry that stops matching fails test_allowlist_has_no_stale_entries.
#: The set only shrinks.
SUBSETS = {
    ("scripts/setup.py", "('codex', 'agy')"): "--enroll-runtime applies to the hosts with a runtime to enroll",
    ("scripts/setup.py", "{'codex': host_install_codex.apply, 'agy': host_install_agy.apply, 'grok': host_install_grok.apply}"):
        "per-host installer dispatch",
    ("coordinator/bin/unimatrix-install.py", "{'codex': ['codex-hook-trust'], 'agy': ['agy-plugin-install'], 'grok': []}"):
        "per-host deferred-action table",
    ("coordinator/bin/unimatrix-install.py", "{'agy': agy_status, 'grok': grok_status}"):
        "status-line config path per host that has a status line",
    ("coordinator/bin/unimatrix-install.py", "{'codex': codex_rp, 'agy': agy_rp, 'grok': grok_rp}"):
        "rules path per host",
    ("coordinator/bin/unimatrix-install.py",
     "{'codex': (want_codex, codex_rp, seed.CODEX_CAP_BYTES), 'agy': (want_agy, agy_rp, seed.AGY_CAP_BYTES)}"):
        "standing-doctrine seed parameters per host",
    ("coordinator/bin/unimatrix-install.py",
     "{'codex': want_codex and 'codex' not in absent and verified, 'grok': want_grok and 'grok' not in absent}"):
        "per-host arrival flag",
}


def _host_id(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in host_registry.IDS:
        return node.value
    return None


def _unpack_values(tree: ast.AST) -> set[int]:
    """`A, B = "a", "b"` binds names; its value tuple is not a host list."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, (ast.Tuple, ast.List)) and all(isinstance(e, ast.Name) for e in t.elts)
            for t in node.targets
        ):
            skip.add(id(node.value))
    return skip


def scan_source(source: str) -> list[tuple[int, str]]:
    """(line, unparsed literal) for every sequence literal that declares two or more registry host ids."""
    tree = ast.parse(source)
    skip = _unpack_values(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)) or id(node) in skip:
            continue
        elts = [k for k in node.keys if k is not None] if isinstance(node, ast.Dict) else node.elts
        named = {h for h in map(_host_id, elts) if h}
        for child in elts:
            if isinstance(child, (ast.Tuple, ast.List)) and child.elts and _host_id(child.elts[0]):
                named.add(_host_id(child.elts[0]))
        if len(named) >= 2:
            hits.append((node.lineno, ast.unparse(node)))
    return sorted(hits)


def find_offenders(root: Path, rels=INSTALLERS, subsets=SUBSETS) -> list[str]:
    """`file:line: literal` for each host-list literal under root/rels not named in subsets."""
    out = []
    for rel in rels:
        for line, text in scan_source((root / rel).read_text(encoding="utf-8")):
            if (rel, text) not in subsets:
                out.append(f"{rel}:{line}: {text}")
    return out


def test_installer_files_exist():
    assert [r for r in INSTALLERS if not (REPO / r).is_file()] == []


def test_installers_read_the_registry_not_a_private_host_list():
    offenders = find_offenders(REPO)
    assert not offenders, "host list re-declared; read coordinator_core.install.host_registry instead:\n" + "\n".join(offenders)


def test_allowlist_has_no_stale_entries():
    seen = {(rel, text) for rel in INSTALLERS for _, text in scan_source((REPO / rel).read_text(encoding="utf-8"))}
    assert sorted(set(SUBSETS) - seen) == []


def test_guard_names_file_and_line_of_a_two_host_tuple(tmp_path):
    (tmp_path / "inst.py").write_text('x = 1\nWANT = ("codex", "grok")\n', encoding="utf-8")
    assert find_offenders(tmp_path, ("inst.py",)) == ["inst.py:2: ('codex', 'grok')"]


def test_guard_catches_list_set_and_host_led_subtuples(tmp_path):
    src = 'a = ["agy", "claude"]\nb = {"codex", "agy"}\nc = (("agy", 1), ("grok", 2))\n'
    (tmp_path / "inst.py").write_text(src, encoding="utf-8")
    assert [o.split(":")[1] for o in find_offenders(tmp_path, ("inst.py",))] == ["1", "2", "3"]


def test_guard_passes_names_single_ids_and_binaries(tmp_path):
    src = (
        'HOST_CODEX, HOST_AGY = "codex", "agy"\n'
        'one = ("claude",)\n'
        'bins = ("grok", "grok.exe", "grok.cmd")\n'
        'twice = ("agy", "agy")\n'
        'other = ("alpha", "beta")\n'
    )
    (tmp_path / "inst.py").write_text(src, encoding="utf-8")
    assert find_offenders(tmp_path, ("inst.py",)) == []


def test_guard_catches_a_dict_keyed_by_two_hosts(tmp_path):
    src = 'a = {"codex": 1, "agy": 2}\nb = {"codex": 1, "x": 2}\n'
    (tmp_path / "inst.py").write_text(src, encoding="utf-8")
    assert find_offenders(tmp_path, ("inst.py",)) == ["inst.py:1: {'codex': 1, 'agy': 2}"]


def test_subset_allowlist_exempts_only_the_named_literal(tmp_path):
    (tmp_path / "inst.py").write_text('a = ("codex", "agy")\nb = ("codex", "grok")\n', encoding="utf-8")
    got = find_offenders(tmp_path, ("inst.py",), {("inst.py", "('codex', 'agy')"): "why"})
    assert got == ["inst.py:2: ('codex', 'grok')"]
