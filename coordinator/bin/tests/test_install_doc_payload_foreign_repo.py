
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_gate_module():
    spec = importlib.util.spec_from_file_location(
        "check_install_doc_payload_under_test", _BIN_DIR / "check-install-doc-payload.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate_module()


def _write_doc(root: Path, name: str, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(body, encoding="utf-8")
    return path


class TestParseForeignRepoRef:
    def test_well_formed_marker_parses(self):
        parsed = gate.parse_foreign_repo_ref("foreign-repo:claude-klabauter/scripts/setup.py")
        assert parsed == ("claude-klabauter", "scripts/setup.py")

    def test_non_marker_token_returns_none(self):
        assert gate.parse_foreign_repo_ref("scripts/setup.py") is None

    def test_empty_repo_name_is_malformed(self):
        repo_name, _ = gate.parse_foreign_repo_ref("foreign-repo:/scripts/setup.py")
        assert repo_name == ""

    def test_dotdot_traversal_is_malformed(self):
        repo_name, _ = gate.parse_foreign_repo_ref("foreign-repo:sibling/../../etc/setup.py")
        assert repo_name == ""

    def test_absolute_path_is_malformed(self):
        repo_name, _ = gate.parse_foreign_repo_ref("foreign-repo:sibling//abs/setup.py")
        assert repo_name == ""

    def test_windows_drive_rooted_path_is_malformed(self):
        repo_name, _ = gate.parse_foreign_repo_ref("foreign-repo:sibling/C:/scripts/setup.py")
        assert repo_name == ""

    def test_unc_path_is_malformed(self):
        repo_name, _ = gate.parse_foreign_repo_ref(r"foreign-repo:sibling/\\evilhost\share\setup.py")
        assert repo_name == ""

    def test_windows_drive_relative_path_is_malformed(self):
        repo_name, _ = gate.parse_foreign_repo_ref("foreign-repo:sibling/C:setup.py")
        assert repo_name == ""

    def test_placeholder_angle_bracket_is_malformed(self):
        repo_name, _ = gate.parse_foreign_repo_ref("foreign-repo:sibling/<name>/setup.py")
        assert repo_name == ""

    def test_untracked_extension_is_malformed(self):
        repo_name, _ = gate.parse_foreign_repo_ref("foreign-repo:sibling/README.md")
        assert repo_name == ""


class TestCheckTreeAcceptsWellFormedForeignRepoMarker:
    """Direction 1: a marked foreign-repo path is ACCEPTED (no Finding),
    without being resolved anywhere in the tree under test."""

    def test_marked_path_absent_from_tree_produces_no_finding(self, tmp_path):
        _write_doc(
            tmp_path,
            "README.md",
            "# Install\n\n"
            "Clone the engine repo, then:\n\n"
            "```\npython3 foreign-repo:claude-klabauter/scripts/setup.py --i-am-agent\n```\n",
        )
        findings = gate.check_tree(tmp_path)
        assert findings == []

    def test_marked_path_alongside_real_local_reference(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "setup.py").write_text("# setup\n", encoding="utf-8")
        _write_doc(
            tmp_path,
            "AGENTS.md",
            "```\npython3 scripts/setup.py\n```\n"
            "```\npython3 foreign-repo:sibling-repo/tools/install.py\n```\n",
        )
        findings = gate.check_tree(tmp_path)
        assert findings == []


class TestCheckTreeStillRejectsUnmarkedBogusPath:
    """Direction 2: an UNMARKED unresolvable path is STILL REJECTED -- the
    marker must not have widened the gate's default behaviour."""

    def test_unmarked_missing_local_path_still_fails(self, tmp_path):
        _write_doc(
            tmp_path,
            "INSTALL.md",
            "```\npython3 scripts/setup.py\n```\n",
        )
        findings = gate.check_tree(tmp_path)
        assert len(findings) == 1
        assert findings[0].missing_path == "scripts/setup.py"
        assert findings[0].kind == "command"

    def test_malformed_foreign_marker_is_a_finding(self, tmp_path):
        _write_doc(
            tmp_path,
            "INSTALL.md",
            "```\npython3 foreign-repo:sibling/../escape/setup.py\n```\n",
        )
        findings = gate.check_tree(tmp_path)
        assert len(findings) == 1
        assert findings[0].kind == "foreign-repo"
        assert findings[0].missing_path == "foreign-repo:sibling/../escape/setup.py"
        rendered = findings[0].render(tmp_path)
        assert "not a well-formed foreign-repo marker" in rendered

    def test_bare_foreign_repo_prefix_with_no_slash_is_a_finding(self, tmp_path):
        _write_doc(
            tmp_path,
            "INSTALL.md",
            "```\npython3 foreign-repo:bogustoken.py\n```\n",
        )
        findings = gate.check_tree(tmp_path)
        assert len(findings) == 1
        assert findings[0].kind == "foreign-repo"


class TestExistingKlabauterPlaceholderUnaffected:

    def test_angle_bracket_placeholder_produces_no_finding(self, tmp_path):
        _write_doc(
            tmp_path,
            "README.md",
            "```\npython3 <klabauter-clone>/scripts/setup.py --i-am-agent\n```\n",
        )
        findings = gate.check_tree(tmp_path)
        assert findings == []
