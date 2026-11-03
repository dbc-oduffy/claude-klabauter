"""Tests for the G-EM home prune: verified backup before delete, baton archive."""

from __future__ import annotations

from coordinator_core.group_em import gem_home_prune as p


def _w(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _baton(home, name, status, dep="ready_to_fire"):
    return _w(home / "state/handoffs" / name, f"---\nstatus: {status}\ndeployment_state: {dep}\n---\nbody\n")


def test_residue_and_old_audits_backed_up_then_removed(tmp_path):
    home, cold = tmp_path / "home", tmp_path / "cold"
    r = _w(home / "state/residue/inbox-blitz/2026-11-02/a.json", "res")
    old = _w(home / "state/audits/2026-10-01-inbox-blitz-x.md", "old")
    new = _w(home / "state/audits/2026-11-02-inbox-blitz-x.md", "new")
    undated = _w(home / "state/audits/notes.md", "n")
    out = p.prune(home, cold)
    assert not r.exists() and not old.exists()
    assert new.exists() and undated.exists()
    assert sorted(out["removed"]) == sorted(
        ["state/residue/inbox-blitz/2026-11-02/a.json", "state/audits/2026-10-01-inbox-blitz-x.md"]
    )
    assert out["refused"] == []
    assert (cold / "group-em-home/state/audits/2026-10-01-inbox-blitz-x.md").read_text() == "old"
    assert not (home / "state/residue/inbox-blitz/2026-11-02").exists()


def test_hash_mismatch_keeps_file_and_refuses(tmp_path, monkeypatch):
    home, cold = tmp_path / "home", tmp_path / "cold"
    r = _w(home / "state/residue/inbox-blitz/a.json", "res")
    real = p._sha256
    monkeypatch.setattr(p, "_sha256", lambda path: "bad" if cold in path.parents else real(path))
    out = p.prune(home, cold)
    assert r.exists()
    assert out["removed"] == []
    assert out["refused"] == [{"path": "state/residue/inbox-blitz/a.json", "reason": "sha256-mismatch"}]


def test_no_cold_dir_deletes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "cold_dir", lambda: None)
    home = tmp_path / "home"
    r = _w(home / "state/residue/inbox-blitz/a.json")
    out = p.prune(home)
    assert r.exists() and out["removed"] == []
    assert out["refused"][0]["reason"] == "no-cold-dir"


def test_consumed_batons_archived_open_ones_stay(tmp_path):
    home = tmp_path / "home"
    done = _baton(home, "2026-10-23-a.md", "consumed")
    cont = _baton(home, "2026-10-24-b.md", "claimed", dep="continued")
    claimed = _baton(home, "2026-10-25-c.md", "claimed")
    opened = _baton(home, "2026-11-01-d.md", "open")
    out = p.prune(home, tmp_path / "cold")
    assert not done.exists() and not cont.exists()
    assert claimed.exists() and opened.exists()
    assert sorted(out["archived"]) == [
        "archive/handoffs/2026-10/2026-10-23-a.md",
        "archive/handoffs/2026-10/2026-10-24-b.md",
    ]


def test_archive_target_exists_is_refused_not_overwritten(tmp_path):
    home = tmp_path / "home"
    src = _baton(home, "2026-10-23-a.md", "consumed")
    _w(home / "archive/handoffs/2026-10/2026-10-23-a.md", "keep")
    out = p.prune(home, tmp_path / "cold")
    assert src.exists()
    assert out["refused"][0]["reason"] == "archive-target-exists"
    assert (home / "archive/handoffs/2026-10/2026-10-23-a.md").read_text() == "keep"


def test_cold_dir_reads_machine_local_key(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(p.machine_resolver, "registry_get", lambda k: seen.setdefault("k", k) and str(tmp_path))
    assert p.cold_dir() == tmp_path
    assert seen["k"] == "coordinator.group_em_home_cold_dir"


def test_clean_paths_dry_run_reuses_fix_concrete_path_citations(tmp_path):
    home = tmp_path / "home"
    f = _w(home / "state/lessons/l.md", "plain text, no paths\n")
    res = p.clean_paths(home)
    assert res == {"applied": False, "rc": 0, "files": 1}
    assert f.read_text() == "plain text, no paths\n"
