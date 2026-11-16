"""`run_landing.land_run` carries the regenerated op catalog with a landing that edits an op docstring,
or refuses naming the render failure. Seeds a tmp repo whose catalog is rendered for one op module."""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from coordinator_core.contract import emit_op_catalog
from coordinator_core.ops.dispatch_emit.tests.test_landing_parity import _fire, _git, _head, _landed_names, _repo
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CATALOG = "coordinator_core/contract/op-catalog.json"
MCP_TOOLS = "coordinator_core/contract/mcp-tools.json"
CATALOG_MD = "docs/reference/op-catalog.md"
GENERATED = (CATALOG, MCP_TOOLS, CATALOG_MD)
_KEYS = dict(zip(("op-catalog.json", "mcp-tools.json", "op-catalog.md"), GENERATED))

REGISTRY_MAP = "coordinator_core/ops/_registry_map.py"
OP_MODULE = "coordinator_core/ops/demo_echo.py"
OP = "demo.echo"
OP_DOTTED = "coordinator_core.ops.demo_echo"

OLD_DOC = "Echo the params back.\n\nReturns them unchanged."
NEW_DOC = "Echo the params back, edited.\n\nReturns them unchanged, edited."


def op_source(doc: str) -> str:
    doc = doc.replace("\n\n", "\n\n    ")
    return f'''"""Demo echo op."""
from coordinator_core.ipc import register_op


@register_op("{OP}")
def _echo(params):
    """{doc}
    """
    return params
'''


def contract(op: str, module: str, doc: str) -> dict:
    return {
        "description": doc,
        "module": module,
        "name": op,
        "op_class": "read",
        "params_schema": {"type": "object"},
        "summary": next((ln.strip() for ln in doc.splitlines() if ln.strip()), ""),
        "timeout_s": 30.0,
    }


def render(contracts: list) -> dict:
    """The three generated texts keyed by repo-relative path, rendered by the production generator."""
    with mock.patch.object(emit_op_catalog, "_contracts", lambda: contracts):
        return {_KEYS[k]: text for k, text in emit_op_catalog.render_all().items()}


def contracts_for(doc: str, extra_ops: int = 0) -> list:
    out = [contract(OP, OP_DOTTED, doc)]
    out += [contract(f"demo.extra{i:04d}", "coordinator_core.ops.demo_extra", f"Extra op {i}.") for i in range(extra_ops)]
    return sorted(out, key=lambda c: c["name"])


def registry_map_source(extra_ops: int = 0) -> str:
    rows = [f'    "{OP}": "{OP_DOTTED}",'] + [
        f'    "demo.extra{i:04d}": "coordinator_core.ops.demo_extra",' for i in range(extra_ops)
    ]
    return "OP_MODULE_MAP = {\n" + "\n".join(rows) + "\n}\n"


def write(root, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def seed_op_repo(tmp_path, monkeypatch, extra_ops: int = 0, op_text: str | None = None) -> Path:
    """A git repo at HEAD holding the registry map, one op module and the catalog rendered for them."""
    root = _repo(tmp_path, monkeypatch)
    write(root, REGISTRY_MAP, registry_map_source(extra_ops))
    write(root, OP_MODULE, op_text if op_text is not None else op_source(OLD_DOC))
    for rel, text in render(contracts_for(OLD_DOC, extra_ops)).items():
        write(root, rel, text)
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed op catalog"], root)
    return root


def blob(root, rel: str) -> str:
    out = subprocess.run(["git", "cat-file", "blob", f"HEAD:{rel}"], cwd=str(root), capture_output=True, check=True)
    return out.stdout.decode("utf-8")


@pytest.fixture
def op_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return seed_op_repo(tmp_path, monkeypatch)


def test_a_docstring_edit_lands_the_three_regenerated_files_in_the_same_commit(op_repo) -> None:
    write(op_repo, OP_MODULE, op_source(NEW_DOC))

    out = _fire("new", op_repo, {"C1": [OP_MODULE]})

    assert out["committed"] is True, out
    assert _landed_names(op_repo) == {OP_MODULE, *GENERATED}
    for rel, text in render(contracts_for(NEW_DOC)).items():
        assert blob(op_repo, rel) == text, rel
    assert "edited" in blob(op_repo, CATALOG)


def test_a_docstring_edit_lands_the_files_through_the_commit_paths_fallback_too(op_repo) -> None:
    write(op_repo, OP_MODULE, op_source(NEW_DOC))

    with mock.patch("coordinator_core.git.commit._gpgsign_enabled", return_value=True):
        out = _fire("new", op_repo, {"C1": [OP_MODULE]})

    assert out["committed"] is True, out
    assert _landed_names(op_repo) == {OP_MODULE, *GENERATED}
    for rel, text in render(contracts_for(NEW_DOC)).items():
        assert blob(op_repo, rel) == text, rel


def test_an_unparsable_op_module_refuses_naming_the_file_and_commits_nothing(op_repo) -> None:
    write(op_repo, OP_MODULE, "def broken(:\n")
    head = _head(op_repo)

    out = _fire("new", op_repo, {"C1": [OP_MODULE]})

    assert out["committed"] is False
    assert out["refused"] == "op-catalog-render-failed", out
    assert "demo_echo.py" in out["error"], out
    assert _head(op_repo) == head


def test_a_registry_map_edit_without_the_generated_files_refuses_naming_the_write_command(op_repo) -> None:
    write(op_repo, REGISTRY_MAP, registry_map_source() + '# a new op would be added here\n')
    head = _head(op_repo)

    out = _fire("new", op_repo, {"C1": [REGISTRY_MAP]})

    assert out["committed"] is False
    assert out["refused"] == "op-catalog-render-failed", out
    assert "emit_op_catalog --write" in out["error"], out
    assert _head(op_repo) == head


def test_a_registry_map_edit_lands_when_the_run_regenerated_all_three_files(op_repo) -> None:
    write(op_repo, REGISTRY_MAP, registry_map_source() + '# annotated\n')
    write(op_repo, OP_MODULE, op_source(NEW_DOC))
    regenerated = render(contracts_for(NEW_DOC))
    for rel, text in regenerated.items():
        write(op_repo, rel, text)

    out = _fire("new", op_repo, {"C1": [REGISTRY_MAP, OP_MODULE, *GENERATED]})

    assert out["committed"] is True, out
    assert "refused" not in out
    for rel, text in regenerated.items():
        assert blob(op_repo, rel) == text, rel


def test_a_landing_with_no_op_module_path_adds_no_generated_file(op_repo) -> None:
    write(op_repo, "notes/a.py", "a = 1\n")

    out = _fire("new", op_repo, {"C1": ["notes/a.py"]})

    assert out["committed"] is True, out
    assert _landed_names(op_repo) == {"notes/a.py"}
    for rel, text in render(contracts_for(OLD_DOC)).items():
        assert blob(op_repo, rel) == text, rel


def test_a_landing_that_does_not_commit_puts_the_generated_files_back(op_repo) -> None:
    write(op_repo, OP_MODULE, op_source(NEW_DOC))
    before = {rel: (op_repo / rel).read_bytes() for rel in GENERATED}
    head = _head(op_repo)
    refusal = {"committed": False, "error": "commit refused", "refused": "CommitRefused"}

    with mock.patch("coordinator_core.ops.dispatch_emit.run_landing._commit", return_value=refusal):
        out = _fire("new", op_repo, {"C1": [OP_MODULE]})

    assert out["committed"] is False, out
    assert _head(op_repo) == head
    assert {rel: (op_repo / rel).read_bytes() for rel in GENERATED} == before


def test_a_landing_that_commits_leaves_the_generated_files_rendered_on_disk(op_repo) -> None:
    write(op_repo, OP_MODULE, op_source(NEW_DOC))

    out = _fire("new", op_repo, {"C1": [OP_MODULE]})

    assert out["committed"] is True, out
    for rel, text in render(contracts_for(NEW_DOC)).items():
        assert (op_repo / rel).read_bytes() == text.encode("utf-8"), rel
