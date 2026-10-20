"""Tests for the repo.provision_local contract module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops import sibling_repo_provision_contract as c

GOOD = """\
schema: sibling-repo-manifest
name: demo-vault-guide
files:
  - path: README.md
    source: templates/README.md
  - path: tests/__init__.py
    content: ""
commit_message: "chore: provision"
"""


def _parse(text: str) -> c.Manifest:
    return c.parse_manifest(text, manifest_rel="m.yaml")


def test_parse_good():
    m = _parse(GOOD)
    assert m.name == "demo-vault-guide"
    assert [f.path for f in m.files] == ["README.md", "tests/__init__.py"]
    assert m.files[0].source == "templates/README.md" and m.files[0].content is None
    assert m.files[1].content == "" and m.files[1].source is None
    assert m.commit_message == "chore: provision"


def _with(files: str = "", name: str = "x", schema: str = "sibling-repo-manifest") -> str:
    return f"schema: {schema}\nname: {name}\nfiles:\n{files}"


@pytest.mark.parametrize(
    "text,field",
    [
        (_with("  - {path: a, content: ''}\n", schema="other"), "schema"),
        (_with("  - {path: a, content: ''}\n", name="a/b"), "name"),
        (_with("  - {path: a, content: ''}\n", name="..") , "name"),
        (_with("  - {path: a, content: ''}\n", name=".git"), "name"),
        (_with("  - {path: /abs, content: ''}\n"), "path"),
        (_with("  - {path: '" + "C" + ":/x', content: ''}\n"), "path"),
        (_with("  - {path: a/../b, content: ''}\n"), "path"),
        (_with("  - {path: .git/hooks/x, content: ''}\n"), "path"),
        (_with("  - {path: .GIT/config, content: ''}\n"), "path"),
        (_with("  - {path: a, source: s, content: ''}\n"), "exactly one"),
        (_with("  - {path: a}\n"), "exactly one"),
        (_with("  - {path: a, content: ''}\n  - {path: a, content: 'z'}\n"), "duplicate"),
        (_with(""), "files"),
        ("- not a mapping\n", "mapping"),
    ],
)
def test_parse_refusals(text, field):
    with pytest.raises(c.ManifestError, match=field):
        _parse(text)


def test_sibling_root(tmp_path):
    assert c.sibling_root(tmp_path / "home", "n") == tmp_path / "n"


def _marked(root: Path, remote: bool = False) -> Path:
    (root / ".git").mkdir(parents=True)
    (root / ".git" / c.MARKER_NAME).write_text(json.dumps({"name": root.name}), encoding="utf-8")
    cfg = '[core]\n\tbare = false\n'
    if remote:
        cfg += '[remote "origin"]\n\turl = x\n'
    (root / ".git" / "config").write_text(cfg, encoding="utf-8")
    return root


def test_marker_absent_and_present(tmp_path):
    assert c.read_marker(tmp_path / "none") is None
    (tmp_path / "bare" / ".git").mkdir(parents=True)
    assert c.read_marker(tmp_path / "bare") is None
    assert c.read_marker(_marked(tmp_path / "m")) == {"name": "m"}


def test_has_remote(tmp_path):
    assert c.has_remote(tmp_path / "missing") is False
    assert c.has_remote(_marked(tmp_path / "a")) is False
    assert c.has_remote(_marked(tmp_path / "b", remote=True)) is True


def test_provisioned_local_root(tmp_path, monkeypatch):
    local = _marked(tmp_path / "loc")
    remote = _marked(tmp_path / "rem", remote=True)
    unmarked = tmp_path / "um"
    (unmarked / ".git").mkdir(parents=True)
    gitfile = tmp_path / "wt"
    gitfile.mkdir()
    (gitfile / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    table = {"loc": local, "rem": remote, "um": unmarked, "wt": gitfile}
    monkeypatch.setattr(c, "receiver_em_to_repo_key", lambda owner: "repos." + owner)
    monkeypatch.setattr(
        c, "registry_get", lambda key: str(table[key[6:]]) if key[6:] in table else None
    )
    assert c.provisioned_local_root("loc") == local
    assert c.provisioned_local_root("rem") is None
    assert c.provisioned_local_root("um") is None
    assert c.provisioned_local_root("wt") is None
    assert c.provisioned_local_root("unregistered") is None


def test_constants():
    assert c.OP_KEY == "repo.provision_local"
    assert c.MARKER_NAME == "coordinator-provision.json"
    assert "repo.provision_local" in c.ROUTE_REMEDY and "\n" not in c.ROUTE_REMEDY


def test_adopt_parse_and_refusal():
    base = "  - {path: a, content: ''}\n"
    assert _parse(_with(base)).adopt is False
    assert _parse(_with(base) + "adopt: true\n").adopt is True
    with pytest.raises(c.ManifestError, match="adopt"):
        _parse(_with(base) + "adopt: yes-please\n")
    with pytest.raises(c.ManifestError, match="adopt"):
        _parse(_with(base) + "adopt: 1\n")


def _home_with_sizing(tmp_path: Path, source_text: str = "make repo demo-vault-guide") -> Path:
    home = tmp_path / "home"
    (home / "state" / "sizings").mkdir(parents=True)
    import yaml

    doc = {"requirement_register": {"rows": [{"id": "R1", "source_text": source_text}]}}
    (home / "state" / "sizings" / "s.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return home


def test_check_assent_passes_when_row_names_target(tmp_path):
    home = _home_with_sizing(tmp_path)
    assert c.check_assent(home, "state/sizings/s.yaml#R1", "demo-vault-guide") == (
        "state/sizings/s.yaml#R1"
    )


@pytest.mark.parametrize(
    "assent",
    ["state/sizings/missing.yaml#R1", "state/sizings/s.yaml#R9", "state/sizings/s.yaml", "", None],
)
def test_check_assent_refuses(tmp_path, assent):
    home = _home_with_sizing(tmp_path)
    with pytest.raises(ValueError):
        c.check_assent(home, assent, "demo-vault-guide")


def test_check_assent_refuses_row_not_naming_target(tmp_path):
    home = _home_with_sizing(tmp_path, source_text="make some other repo")
    with pytest.raises(ValueError, match="does not name"):
        c.check_assent(home, "state/sizings/s.yaml#R1", "demo-vault-guide")


def test_target_resolvers(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    marked = _marked(tmp_path / "marked")
    unmarked = tmp_path / "unmarked"
    (unmarked / ".git").mkdir(parents=True)
    absent = tmp_path / "absent"

    assert c.provisioned_target_of(marked / "a" / "b.txt", home) == marked
    assert c.provisioned_target_of(unmarked / "x", home) is None
    assert c.provisioned_target_of(absent / "x", home) is None
    assert c.provisioned_target_of(home / "x", home) is None
    assert c.provisioned_target_of(tmp_path.parent / "x", home) is None

    assert c.remedy_applies(marked / "a", home) is True
    assert c.remedy_applies(absent / "a" / "b", home) is True
    assert c.remedy_applies(unmarked / "a", home) is False
    assert c.remedy_applies(home / "a", home) is False
    assert c.remedy_applies(tmp_path.parent / "x", home) is False
