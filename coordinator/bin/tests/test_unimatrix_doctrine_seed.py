"""Behavior pins for the doctrine and skills seeder: managed blocks, host caps, agy skills.json, Codex skills."""
import importlib.util
import json
import os
from pathlib import Path

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
REPO = BIN.parents[1]


def load() -> "ModuleType":
    spec = importlib.util.spec_from_file_location("unimatrix_doctrine_seed", BIN / "unimatrix-doctrine-seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ds = load()
B, E = ds.DOCTRINE_BEGIN, ds.DOCTRINE_END
OTHER_B, OTHER_E = "<!-- BEGIN other -->", "<!-- END other -->"


def test_real_doctrine_fits_agy_cap(tmp_path: Path) -> None:
    # claude-klabauter carries no global-doctrine/ tree: its own CLAUDE.md stands in as the real doctrine text.
    (tmp_path / "global-doctrine" / "rules").mkdir(parents=True)
    (tmp_path / "global-doctrine" / "CLAUDE.md").write_text(
        (REPO / "CLAUDE.md").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "global-doctrine" / "rules" / "rules.md").write_text("- Context7 only when needed.\n", encoding="utf-8")
    block = ds.render_coordinator_doctrine(tmp_path, ds.AGY_CAP_BYTES)
    assert block.startswith(B + "\n") and block.endswith(E + "\n")
    assert len(block.encode("utf-8")) < 24000
    assert "North star" in block and "Context7" in block


def test_over_cap_raises(tmp_path: Path) -> None:
    (tmp_path / "global-doctrine" / "rules").mkdir(parents=True)
    (tmp_path / "global-doctrine" / "CLAUDE.md").write_text("x" * 100)
    (tmp_path / "global-doctrine" / "rules" / "rules.md").write_text("y")
    assert ds.render_coordinator_doctrine(tmp_path, 1000)
    with pytest.raises(ValueError):
        ds.render_coordinator_doctrine(tmp_path, 100)


def test_two_marker_pairs_coexist_and_unseed_independently(tmp_path: Path) -> None:
    f = tmp_path / "AGENTS.md"
    f.write_text("user text\n")
    assert ds.seed_block(f, f"{B}\nA\n{E}\n", B, E) == "added"
    assert ds.seed_block(f, f"{OTHER_B}\nO\n{OTHER_E}\n", OTHER_B, OTHER_E) == "added"
    assert ds.seed_block(f, f"{B}\nA\n{E}\n", B, E) == "present"
    assert ds.seed_block(f, f"{B}\nA2\n{E}\n", B, E) == "updated"
    assert ds.unseed_block(f, B, E) == "removed"
    text = f.read_text()
    assert "A2" not in text and "O" in text and "user text" in text
    assert ds.unseed_block(f, OTHER_B, OTHER_E) == "removed"
    assert f.read_text().strip() == "user text"
    assert ds.unseed_block(f, B, E) == "absent"


def test_seed_block_creates_and_unseed_deletes_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "sub" / "GEMINI.md"
    assert ds.seed_block(f, f"{B}\nA\n{E}\n", B, E) == "created"
    assert ds.unseed_block(f, B, E) == "removed"
    assert not f.exists()


_REAL_IS_SCRATCH_ROOT = ds.is_scratch_root


@pytest.fixture(autouse=True)
def _pytest_tmp_roots_are_not_scratch(monkeypatch: pytest.MonkeyPatch) -> None:
    # pytest's tmp_path sits under the system temp dir, which the scratch predicate refuses.
    monkeypatch.setattr(ds, "is_scratch_root", lambda path: False)


@pytest.mark.parametrize("path", [
    "/private/tmp/claude-501/scratchpad/skills",
    "/tmp/x/skills",
    "/var/folders/ab/cd/T/skills",
    "C:\\Users\\u\\AppData\\Local\\Temp\\p\\skills",  # abs-path-ok: fixture string, never touched
    "/repo/scratch/skills",
])
def test_scratch_predicate_flags_tmp_and_scratch_shapes(path: str) -> None:
    # Source: observed ~/.gemini/config/skills.json holding a /private/tmp scratchpad root (run ask-cb5854 R1 item 5).
    assert _REAL_IS_SCRATCH_ROOT(path)


@pytest.mark.parametrize("path", ["/home/u/tmp/proj/skills", "/Users/u/tmp/skills", "/Users/u/.claude/plugins/coordinator/skills", "C:\\Users\\u\\.claude\\skills", "/p/skills", "/Users/u/scratchpad/proj/skills", "/Users/u/scratch", "/Users/u/proj/scratchpad"])  # abs-path-ok: fixture string; scratchpad outside a temp root and a bare trailing scratch are sanctioned, not scratch
def test_scratch_predicate_passes_sanctioned_roots(path: str) -> None:
    assert not _REAL_IS_SCRATCH_ROOT(path)


def test_agy_skills_never_creates_the_file_and_prunes_scratch_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "is_scratch_root", lambda p: str(p).startswith("/private/tmp"))
    sj = tmp_path / "skills.json"
    keep = tmp_path / "keep"
    keep.mkdir()
    assert ds.seed_agy_skills(sj, "/tmp/x/skills") == "present"
    assert not sj.exists()
    sj.write_text(json.dumps({"entries": [{"path": "/private/tmp/s/skills"}, {"path": str(keep)}]}))
    assert ds.seed_agy_skills(sj, "/tmp/x/skills") == "pruned"
    assert json.loads(sj.read_text())["entries"] == [{"path": str(keep)}]
    assert ds.seed_agy_skills(sj, "/tmp/x/skills") == "present"


def test_agy_skills_drops_every_coordinator_skills_source_and_keeps_foreign_live_entries(tmp_path: Path) -> None:
    root = tmp_path / "plugin" / "skills"
    cache = tmp_path / "cache" / "coordinator" / "4.6.21" / "skills"
    klab = tmp_path / "klab" / "coordinator" / "skills"
    foreign = tmp_path / "foreign" / "skills"
    for d in (root, cache, klab, foreign):
        d.mkdir(parents=True)
    sj = tmp_path / "skills.json"
    sj.write_text(json.dumps({"entries": [{"path": str(p)} for p in (root, cache, klab, foreign)]}))
    assert ds.seed_agy_skills(sj, root) == "pruned"
    assert json.loads(sj.read_text())["entries"] == [{"path": str(foreign)}]
    assert ds.seed_agy_skills(sj, root) == "present"


def test_agy_skills_drops_absent_and_other_profile_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    users = tmp_path / "Users"
    home, other = users / "me", users / "me_old"
    (home / "skills").mkdir(parents=True)
    (other / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    sj = tmp_path / "skills.json"
    sj.write_text(json.dumps({"entries": [
        {"path": str(other / "skills")}, {"path": str(tmp_path / "gone")}, {"path": str(home / "skills")}]}))
    assert ds.seed_agy_skills(sj, "/p/skills") == "pruned"
    assert json.loads(sj.read_text())["entries"] == [{"path": str(home / "skills")}]


def test_other_profile_needs_a_shared_profiles_root(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "me"
    assert ds.is_other_profile_path(tmp_path / "Users" / "me_old" / "x", home)
    assert not ds.is_other_profile_path(home / "x", home)
    assert not ds.is_other_profile_path(tmp_path / "elsewhere" / "x", home)
    assert not ds.is_other_profile_path(tmp_path / "sib" / "x", tmp_path / "proj" / "me")


@pytest.mark.skipif(os.name != "nt", reason="drive-letter paths are win32-shaped")
def test_other_profile_detects_win32_drive_paths() -> None:
    home = Path("C:/Users/me")
    assert ds.is_other_profile_path(r"C:\Users\other\.agy\skills", home)
    assert ds.is_other_profile_path("c:/users/Other/x", home)
    assert not ds.is_other_profile_path(r"C:\Users\me\.agy\skills", home)
    assert not ds.is_other_profile_path(r"C:\Program Files\x", home)


def test_unseed_agy_skills_removes_only_the_legacy_root_entry(tmp_path: Path) -> None:
    sj = tmp_path / "skills.json"
    root = tmp_path / "plugin" / "skills"
    sj.write_text(json.dumps({"entries": [{"path": "/foreign"}, {"path": str(root)}]}))
    assert ds.unseed_agy_skills(sj, root) == "removed"
    assert json.loads(sj.read_text())["entries"] == [{"path": "/foreign"}]
    assert ds.unseed_agy_skills(sj, root) == "absent"
    sj.write_text(json.dumps({"entries": [{"path": str(root)}]}))
    assert ds.unseed_agy_skills(sj, root) == "removed"
    assert not sj.exists()


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    root = tmp_path / "plugin" / "skills"
    for name in ("alpha", "beta"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"# {name}\n")
    (root / "notaskill").mkdir()
    (root / "notaskill" / "README.md").write_text("x")
    (root / "loose.md").write_text("x")
    return root


def test_codex_skills_one_entry_per_skill_dir(tmp_path: Path, skills_root) -> None:
    dest = tmp_path / "home" / ".agents" / "skills"
    assert ds.seed_codex_skills(dest, skills_root) == ["alpha", "beta"]
    assert (dest / "alpha" / "SKILL.md").read_text() == "# alpha\n"
    assert (dest / "beta" / "SKILL.md").is_file()
    assert not (dest / "notaskill").exists()
    assert ds.seed_codex_skills(dest, skills_root) == ["alpha", "beta"]


def test_codex_refresh_keeps_current_links_and_repairs_drift(tmp_path, skills_root, monkeypatch):
    dest = tmp_path / "skills"
    names = ds.seed_codex_skills(dest, skills_root)
    alpha, beta = (dest / name for name in names)
    if not alpha.is_symlink() or not beta.is_symlink():
        pytest.skip("symlink creation unavailable")
    beta.unlink()
    beta.symlink_to(skills_root / "alpha", target_is_directory=True)
    ds._marker(beta).write_bytes(b"stale")
    original_remove, original_write = ds._remove_entry, Path.write_text

    def remove(path):
        assert path != alpha
        return original_remove(path)

    def write(path, *args, **kwargs):
        assert path != ds._marker(alpha)
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(ds, "_remove_entry", remove)
    monkeypatch.setattr(Path, "write_text", write)
    assert ds.seed_codex_skills(dest, skills_root) == names
    assert beta.samefile(skills_root / "beta")
    assert ds._marker(beta).read_text() == "managed by unimatrix-install\n"


def test_codex_skills_copy_when_symlink_refused(tmp_path: Path, skills_root, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a, **k):
        raise OSError("symlink refused")

    monkeypatch.setattr(os, "symlink", refuse)
    dest = tmp_path / "skills"
    assert ds.seed_codex_skills(dest, skills_root) == ["alpha", "beta"]
    assert not (dest / "alpha").is_symlink()
    assert (dest / "alpha" / "SKILL.md").read_text() == "# alpha\n"


def test_codex_unseed_removes_only_marker_carrying_entries(tmp_path: Path, skills_root) -> None:
    dest = tmp_path / "skills"
    (dest / "mine").mkdir(parents=True)
    (dest / "mine" / "SKILL.md").write_text("user skill")
    (dest / "alpha").mkdir()
    (dest / "alpha" / "SKILL.md").write_text("foreign alpha")
    with pytest.raises(ValueError, match="alpha"):
        ds.seed_codex_skills(dest, skills_root)
    assert (dest / "alpha" / "SKILL.md").read_text() == "foreign alpha"
    assert ds.unseed_codex_skills(dest) == []
    assert not (dest / "beta").exists() and not (dest / "beta.coordinator-managed").exists()
    assert (dest / "mine" / "SKILL.md").read_text() == "user skill"
    assert (dest / "alpha" / "SKILL.md").read_text() == "foreign alpha"
    assert (skills_root / "beta" / "SKILL.md").is_file()


def test_codex_unseed_of_symlink_leaves_source(tmp_path: Path, skills_root) -> None:
    dest = tmp_path / "skills"
    ds.seed_codex_skills(dest, skills_root)
    assert sorted(ds.unseed_codex_skills(dest)) == ["alpha", "beta"]
    assert (skills_root / "alpha" / "SKILL.md").is_file()
    assert list(dest.iterdir()) == []


@pytest.mark.parametrize("invalid", ["missing", "empty", "unreadable", "broken", "missing-entry"])
def test_invalid_skill_inventory_preserves_managed_entries(tmp_path, skills_root, monkeypatch, invalid):
    dest = tmp_path / "dest"
    ds.seed_codex_skills(dest, skills_root)
    before = {p.name: p.read_bytes() for p in dest.glob("*coordinator-managed")}
    source = tmp_path / invalid
    if invalid == "empty":
        source.mkdir()
    elif invalid in ("unreadable", "broken", "missing-entry"):
        source = skills_root
        (source / "alpha" / "SKILL.md").unlink()
        if invalid == "broken":
            try:
                (source / "alpha" / "SKILL.md").symlink_to(source / "absent")
            except OSError:  # Windows without SeCreateSymbolicLinkPrivilege (WinError 1314)
                pytest.skip("symlink creation unavailable")
        elif invalid == "unreadable":
            (source / "alpha" / "SKILL.md").write_text("# alpha")
            original = Path.read_bytes

            def read(path):
                if path == source / "alpha" / "SKILL.md":
                    raise PermissionError("unreadable required source")
                return original(path)

            monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises((OSError, ValueError)):
        ds.seed_codex_skills(dest, source)
    assert {p.name: p.read_bytes() for p in dest.glob("*coordinator-managed")} == before
    assert (dest / "alpha").is_symlink() or (dest / "alpha").is_dir()
    assert (dest / "beta" / "SKILL.md").read_text() == "# beta\n"


def test_conflicting_skill_can_be_retried_without_deleting_foreign_content(tmp_path, skills_root):
    dest = tmp_path / "dest"
    (dest / "alpha").mkdir(parents=True)
    (dest / "alpha" / "SKILL.md").write_text("foreign")
    with pytest.raises(ValueError, match="alpha"):
        ds.seed_codex_skills(dest, skills_root)
    assert (dest / "alpha" / "SKILL.md").read_text() == "foreign"
    assert not (dest / "beta").exists()
    (dest / "alpha").rename(dest / "operator-alpha")
    assert ds.seed_codex_skills(dest, skills_root) == ["alpha", "beta"]
    assert ds.seed_codex_skills(dest, skills_root) == ["alpha", "beta"]
    assert (dest / "operator-alpha" / "SKILL.md").read_text() == "foreign"


def test_partial_skill_copy_failure_is_reported_and_retryable(tmp_path, skills_root, monkeypatch):
    dest = tmp_path / "dest"
    original = ds.shutil.copytree

    def refuse_symlink(*args, **kwargs):
        raise OSError("symlink unavailable")

    def copy(source, target, *args, **kwargs):
        if source.name == "beta":
            raise OSError("destination unavailable")
        return original(source, target, *args, **kwargs)

    monkeypatch.setattr(ds.os, "symlink", refuse_symlink)
    monkeypatch.setattr(ds.shutil, "copytree", copy)
    with pytest.raises(OSError, match="destination unavailable"):
        ds.seed_codex_skills(dest, skills_root)
    assert (dest / "alpha" / "SKILL.md").read_text() == "# alpha\n"
    monkeypatch.setattr(ds.shutil, "copytree", original)
    assert ds.seed_codex_skills(dest, skills_root) == ["alpha", "beta"]


def test_foreign_skill_arriving_during_seed_is_incomplete(tmp_path, skills_root, monkeypatch):
    dest = tmp_path / "dest"

    def link(source, target, **kwargs):
        if source.name == "alpha":
            (dest / "beta").mkdir()
            (dest / "beta" / "SKILL.md").write_text("concurrent foreign skill")
        raise OSError("symlink unavailable")

    monkeypatch.setattr(ds.os, "symlink", link)
    with pytest.raises(ValueError, match="drift.*beta"):
        ds.seed_codex_skills(dest, skills_root)
    assert (dest / "beta" / "SKILL.md").read_text() == "concurrent foreign skill"
    assert not (dest / "beta.coordinator-managed").exists()


def test_codex_reseed_prunes_removed_skill_but_keeps_foreign(tmp_path: Path, skills_root) -> None:
    dest = tmp_path / "skills"
    (dest / "mine").mkdir(parents=True)
    (dest / "mine" / "SKILL.md").write_text("user skill")
    ds.seed_codex_skills(dest, skills_root)
    import shutil
    shutil.rmtree(skills_root / "beta")
    assert ds.seed_codex_skills(dest, skills_root) == ["alpha"]
    assert not (dest / "beta").exists() and not (dest / "beta.coordinator-managed").exists()
    assert (dest / "alpha" / "SKILL.md").is_file()
    assert (dest / "mine" / "SKILL.md").read_text() == "user skill"


@pytest.fixture
def klabauter_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> "tuple[ModuleType, Path]":
    """The installer copied beside its lib/ and seeder under tmp_path (outside every repo), with no global-doctrine sibling."""
    import shutil
    bin_dir = tmp_path / "mirror" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("unimatrix-install.py", "unimatrix-doctrine-seed.py"):
        shutil.copy2(BIN / name, bin_dir / name)
    shutil.copytree(BIN / "lib", bin_dir / "lib")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    spec = importlib.util.spec_from_file_location("unimatrix_install_mirror", bin_dir / "unimatrix-install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (mod.SKILLS_ROOT / "required").mkdir(parents=True)
    (mod.SKILLS_ROOT / "required" / "SKILL.md").write_text("# required\n", encoding="utf-8")
    return mod, home


def _seed_agy(mod, tmp_path):
    import io
    out = io.StringIO()
    rp = tmp_path / "agy" / "GEMINI.md"
    missing = mod._seed_standing_doctrine(out, False, True, None, rp, None, tmp_path / "agy" / "skills.json")
    return missing, out.getvalue(), rp


def _content_root(tmp_path):
    doe = tmp_path / "doe"
    (doe / "global-doctrine" / "rules").mkdir(parents=True)
    (doe / "global-doctrine" / "CLAUDE.md").write_text("# Doctrine from the DoE root\n", encoding="utf-8")
    (doe / "global-doctrine" / "rules" / "rules.md").write_text("- a rule\n", encoding="utf-8")
    return doe


def test_doctrine_installs_from_content_root_pointer_and_names_rung(tmp_path: Path, klabauter_mirror) -> None:
    mod, home = klabauter_mirror
    doe = _content_root(tmp_path)
    (home / ".claude" / ".coordinator-content-root").write_text(str(doe) + "\n", encoding="utf-8")
    missing, text, rp = _seed_agy(mod, tmp_path)
    assert missing == []
    assert "DoE root pointer" in text and str(doe) in text
    assert "Doctrine from the DoE root" in rp.read_text(encoding="utf-8")


def _seed_codex(mod, tmp_path, home):
    import io
    (home / ".claude" / ".coordinator-content-root").write_text(str(_content_root(tmp_path)) + "\n", encoding="utf-8")
    out = io.StringIO()
    rp = tmp_path / "codex" / "AGENTS.md"
    return out, rp, lambda: mod._seed_standing_doctrine(out, True, False, rp, None, tmp_path / "codex" / "skills", None)


def test_unmanaged_text_is_reported_untouched_and_block_carries_steer(tmp_path: Path, klabauter_mirror) -> None:
    mod, home = klabauter_mirror
    out, rp, run = _seed_codex(mod, tmp_path, home)
    rp.parent.mkdir(parents=True)
    outside = "## Unreal Engine\n\n- hand-written UE rule\n"
    rp.write_text(outside, encoding="utf-8")
    assert run() == []
    text = out.getvalue()
    assert "WARN" in text and str(rp) in text and "2 non-blank line(s)" in text
    seeded = rp.read_text(encoding="utf-8")
    assert seeded.startswith(outside)
    assert ds.PROJECT_STEER in seeded and ds.PER_PROJECT_STEER in seeded
    assert ds.split_block(seeded, B, E)[0] == outside + "\n"


def test_block_tracks_the_live_source_on_every_run(tmp_path: Path, klabauter_mirror) -> None:
    mod, home = klabauter_mirror
    out, rp, run = _seed_codex(mod, tmp_path, home)
    assert run() == []
    src = tmp_path / "doe" / "global-doctrine" / "rules" / "rules.md"
    src.write_text(src.read_text(encoding="utf-8") + "\n- a fresh live rule\n", encoding="utf-8")
    assert run() == []
    seeded = rp.read_text(encoding="utf-8")
    assert "a fresh live rule" in seeded
    src.write_text("- only rule\n", encoding="utf-8")
    assert run() == []
    seeded = rp.read_text(encoding="utf-8")
    assert "a fresh live rule" not in seeded and "- only rule" in seeded


def test_clean_file_prints_no_unmanaged_advisory(tmp_path: Path, klabauter_mirror) -> None:
    mod, home = klabauter_mirror
    out, rp, run = _seed_codex(mod, tmp_path, home)
    assert run() == []
    assert "outside the coordinator-doctrine block" not in out.getvalue()
    out.truncate(0)
    out.seek(0)
    assert run() == []
    assert "outside the coordinator-doctrine block" not in out.getvalue()


def test_doctrine_miss_names_the_pointer_files_read(tmp_path: Path, klabauter_mirror) -> None:
    mod, home = klabauter_mirror
    missing, text, rp = _seed_agy(mod, tmp_path)
    assert missing == ["agy coordinator doctrine"]
    assert "beside this install" not in text
    assert str(home / ".claude" / ".coordinator-content-root") in text
    assert str(home / ".claude" / ".coordinator-content-root") in text
    assert not rp.exists()


def test_doctrine_miss_names_a_pointer_value_without_doctrine(tmp_path: Path, klabauter_mirror) -> None:
    mod, home = klabauter_mirror
    empty = tmp_path / "empty-doe"
    empty.mkdir()
    (home / ".claude" / ".coordinator-content-root").write_text(str(empty), encoding="utf-8")
    missing, text, _ = _seed_agy(mod, tmp_path)
    assert missing == ["agy coordinator doctrine"]
    assert str(empty) in text and "no global-doctrine/CLAUDE.md" in text


def test_beside_install_rung_still_resolves_without_pointer(tmp_path: Path, klabauter_mirror) -> None:
    mod, _ = klabauter_mirror
    plugin = tmp_path / "plug"
    (plugin / "bin").mkdir(parents=True)
    (plugin / "global-doctrine").mkdir()
    (plugin / "global-doctrine" / "CLAUDE.md").write_text("x", encoding="utf-8")
    root, rung, _ = mod.resolve_doctrine(plugin)
    assert root == plugin.parent / "plug" and rung == "--plugin-root"


def test_main_is_a_refusing_library_entrypoint(capsys: pytest.CaptureFixture[str]) -> None:
    assert ds.main(["--any"]) == 2
    assert "library module" in capsys.readouterr().err
