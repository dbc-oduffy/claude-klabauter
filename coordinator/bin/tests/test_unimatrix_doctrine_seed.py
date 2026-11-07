"""Behavior pins for the doctrine and skills seeder: managed blocks, host caps, agy skills.json, Codex skills."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
REPO = BIN.parents[1]


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_doctrine_seed", BIN / "unimatrix-doctrine-seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ds = load()
B, E = ds.DOCTRINE_BEGIN, ds.DOCTRINE_END
OTHER_B, OTHER_E = "<!-- BEGIN other -->", "<!-- END other -->"


def test_real_doctrine_fits_agy_cap(tmp_path):
    # claude-klabauter carries no global-doctrine/ tree: its own CLAUDE.md stands in as the real doctrine text.
    (tmp_path / "global-doctrine" / "rules").mkdir(parents=True)
    (tmp_path / "global-doctrine" / "CLAUDE.md").write_text(
        (REPO / "CLAUDE.md").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "global-doctrine" / "rules" / "rules.md").write_text("- Context7 only when needed.\n", encoding="utf-8")
    block = ds.render_coordinator_doctrine(tmp_path, ds.AGY_CAP_BYTES)
    assert block.startswith(B + "\n") and block.endswith(E + "\n")
    assert len(block.encode("utf-8")) < 24000
    assert "North star" in block and "Context7" in block


def test_over_cap_raises(tmp_path):
    (tmp_path / "global-doctrine" / "rules").mkdir(parents=True)
    (tmp_path / "global-doctrine" / "CLAUDE.md").write_text("x" * 100)
    (tmp_path / "global-doctrine" / "rules" / "rules.md").write_text("y")
    assert ds.render_coordinator_doctrine(tmp_path, 1000)
    with pytest.raises(ValueError):
        ds.render_coordinator_doctrine(tmp_path, 100)


def test_two_marker_pairs_coexist_and_unseed_independently(tmp_path):
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


def test_seed_block_creates_and_unseed_deletes_empty_file(tmp_path):
    f = tmp_path / "sub" / "GEMINI.md"
    assert ds.seed_block(f, f"{B}\nA\n{E}\n", B, E) == "created"
    assert ds.unseed_block(f, B, E) == "removed"
    assert not f.exists()


def test_agy_skills_idempotent_and_unseed_only_ours(tmp_path):
    sj = tmp_path / "skills.json"
    sj.write_text(json.dumps({"entries": [{"path": "/foreign"}]}))
    root = tmp_path / "plugin" / "skills"
    assert ds.seed_agy_skills(sj, root) == "added"
    assert ds.seed_agy_skills(sj, root) == "present"
    entries = json.loads(sj.read_text())["entries"]
    assert entries == [{"path": "/foreign"}, {"path": str(root)}]
    assert ds.unseed_agy_skills(sj, root) == "removed"
    assert json.loads(sj.read_text())["entries"] == [{"path": "/foreign"}]
    assert ds.unseed_agy_skills(sj, root) == "absent"


def test_agy_skills_created_then_file_removed_when_empty(tmp_path):
    sj = tmp_path / "d" / "skills.json"
    assert ds.seed_agy_skills(sj, "/p/skills") == "created"
    assert json.loads(sj.read_text()) == {"entries": [{"path": "/p/skills"}]}
    assert ds.unseed_agy_skills(sj, "/p/skills") == "removed"
    assert not sj.exists()


@pytest.fixture
def skills_root(tmp_path):
    root = tmp_path / "plugin" / "skills"
    for name in ("alpha", "beta"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"# {name}\n")
    (root / "notaskill").mkdir()
    (root / "notaskill" / "README.md").write_text("x")
    (root / "loose.md").write_text("x")
    return root


def test_codex_skills_one_entry_per_skill_dir(tmp_path, skills_root):
    dest = tmp_path / "home" / ".agents" / "skills"
    assert ds.seed_codex_skills(dest, skills_root) == ["alpha", "beta"]
    assert (dest / "alpha" / "SKILL.md").read_text() == "# alpha\n"
    assert (dest / "beta" / "SKILL.md").is_file()
    assert not (dest / "notaskill").exists()
    assert ds.seed_codex_skills(dest, skills_root) == ["alpha", "beta"]


def test_codex_skills_copy_when_symlink_refused(tmp_path, skills_root, monkeypatch):
    def refuse(*a, **k):
        raise OSError("symlink refused")

    monkeypatch.setattr(os, "symlink", refuse)
    dest = tmp_path / "skills"
    assert ds.seed_codex_skills(dest, skills_root) == ["alpha", "beta"]
    assert not (dest / "alpha").is_symlink()
    assert (dest / "alpha" / "SKILL.md").read_text() == "# alpha\n"


def test_codex_unseed_removes_only_marker_carrying_entries(tmp_path, skills_root):
    dest = tmp_path / "skills"
    (dest / "mine").mkdir(parents=True)
    (dest / "mine" / "SKILL.md").write_text("user skill")
    (dest / "alpha").mkdir()
    (dest / "alpha" / "SKILL.md").write_text("foreign alpha")
    assert ds.seed_codex_skills(dest, skills_root) == ["beta"]
    assert (dest / "alpha" / "SKILL.md").read_text() == "foreign alpha"
    assert ds.unseed_codex_skills(dest) == ["beta"]
    assert not (dest / "beta").exists() and not (dest / "beta.coordinator-managed").exists()
    assert (dest / "mine" / "SKILL.md").read_text() == "user skill"
    assert (dest / "alpha" / "SKILL.md").read_text() == "foreign alpha"
    assert (skills_root / "beta" / "SKILL.md").is_file()


def test_codex_unseed_of_symlink_leaves_source(tmp_path, skills_root):
    dest = tmp_path / "skills"
    ds.seed_codex_skills(dest, skills_root)
    assert sorted(ds.unseed_codex_skills(dest)) == ["alpha", "beta"]
    assert (skills_root / "alpha" / "SKILL.md").is_file()
    assert list(dest.iterdir()) == []


def test_codex_reseed_prunes_removed_skill_but_keeps_foreign(tmp_path, skills_root):
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


def test_main_is_a_refusing_library_entrypoint(capsys):
    assert ds.main(["--any"]) == 2
    assert "library module" in capsys.readouterr().err
