"""`landing_render.render_for_landing` re-renders the op catalog from the landed op source by AST,
reading the root's HEAD catalog and never importing the op modules."""

from __future__ import annotations

import time

import pytest

from coordinator_core.contract import landing_render
from coordinator_core.ops.dispatch_emit.tests.test_land_run_renders_op_catalog import (
    CATALOG,
    GENERATED,
    NEW_DOC,
    OLD_DOC,
    OP,
    OP_DOTTED,
    OP_MODULE,
    REGISTRY_MAP,
    contract,
    contracts_for,
    op_source,
    render,
    seed_op_repo,
    write,
)

from coordinator_core.ops.dispatch_emit.tests.test_landing_parity import _git

pytestmark = pytest.mark.spawns_process


@pytest.fixture
def op_repo(tmp_path, monkeypatch):
    return seed_op_repo(tmp_path, monkeypatch)


def _expected(description: str) -> dict:
    return render([contract(OP, OP_DOTTED, description)])


def test_a_docstring_edit_renders_the_three_files_equal_to_a_render_of_the_edited_source(op_repo):
    write(op_repo, OP_MODULE, op_source(NEW_DOC))

    out = landing_render.render_for_landing(op_repo, [OP_MODULE])

    assert out == render(contracts_for(NEW_DOC))
    assert set(out) == set(GENERATED)


def test_a_path_that_is_no_op_module_renders_nothing(op_repo):
    assert landing_render.render_for_landing(op_repo, ["notes/a.py", "docs/x.md"]) is None


def test_a_one_line_handler_doc_gets_the_module_doc_appended(op_repo):
    src = (
        '"""Module doc line."""\n'
        "from coordinator_core.ipc import register_op\n\n\n"
        f'@register_op("{OP}")\n'
        "def _echo(params):\n"
        '    """One liner."""\n'
        "    return params\n"
    )
    write(op_repo, OP_MODULE, src)

    out = landing_render.render_for_landing(op_repo, [OP_MODULE])

    assert out == _expected("One liner.\n\nModule doc line.")


def test_a_handler_with_no_doc_takes_the_module_doc(op_repo):
    src = (
        '"""Module doc line."""\n'
        "from coordinator_core.ipc import register_op\n\n\n"
        f'@register_op("{OP}")\n'
        "def _echo(params):\n"
        "    return params\n"
    )
    write(op_repo, OP_MODULE, src)

    out = landing_render.render_for_landing(op_repo, [OP_MODULE])

    assert out == _expected("Module doc line.")


def test_a_handler_doc_naming_module_docstring_gets_the_module_doc_appended(op_repo):
    src = (
        '"""Module doc line."""\n'
        "from coordinator_core.ipc import register_op\n\n\n"
        f'@register_op("{OP}")\n'
        "def _echo(params):\n"
        '    """See the module docstring.\n\n    More.\n    """\n'
        "    return params\n"
    )
    write(op_repo, OP_MODULE, src)

    out = landing_render.render_for_landing(op_repo, [OP_MODULE])

    assert out == _expected("See the module docstring.\n\nMore.\n\nModule doc line.")


def test_a_register_op_call_form_handler_defined_in_the_same_file_is_located(op_repo):
    src = (
        "from coordinator_core.ipc import register_op\n\n\n"
        "def _echo(params):\n"
        '    """Call form.\n\n    Two lines.\n    """\n'
        "    return params\n\n\n"
        f'register_op("{OP}", _echo)\n'
    )
    write(op_repo, OP_MODULE, src)

    out = landing_render.render_for_landing(op_repo, [OP_MODULE])

    assert out == _expected("Call form.\n\nTwo lines.")


def test_a_call_form_handler_imported_from_another_module_refuses(op_repo):
    src = (
        "from coordinator_core.ipc import register_op\n"
        "from coordinator_core.elsewhere import _echo\n\n"
        f'register_op("{OP}", _echo)\n'
    )
    write(op_repo, OP_MODULE, src)

    with pytest.raises(landing_render.RenderRefused) as exc:
        landing_render.render_for_landing(op_repo, [OP_MODULE])

    assert "demo_echo.py" in str(exc.value)


def test_an_owned_op_with_no_registration_in_the_file_refuses(op_repo):
    write(op_repo, OP_MODULE, '"""Module doc."""\nx = 1\n')

    with pytest.raises(landing_render.RenderRefused) as exc:
        landing_render.render_for_landing(op_repo, [OP_MODULE])

    assert "demo_echo.py" in str(exc.value)


def test_an_unparsable_op_module_refuses_naming_the_file(op_repo):
    write(op_repo, OP_MODULE, "def broken(:\n")

    with pytest.raises(landing_render.RenderRefused) as exc:
        landing_render.render_for_landing(op_repo, [OP_MODULE])

    assert "demo_echo.py" in str(exc.value)


@pytest.mark.parametrize("path", [
    REGISTRY_MAP,
    "coordinator_core/ipc.py",
    "coordinator_core/authz/classification.py",
    "coordinator_core/contract/emit_op_catalog.py",
    "coordinator_core/contract/op_contract.py",
])
def test_a_landing_of_a_source_beyond_a_docstring_patch_refuses_naming_the_write_command(op_repo, path):
    with pytest.raises(landing_render.RenderRefused) as exc:
        landing_render.render_for_landing(op_repo, [path])

    assert "emit_op_catalog --write" in str(exc.value)


def test_a_package_op_module_is_triggered_by_its_init_file(tmp_path, monkeypatch):
    pkg_init = "coordinator_core/ops/demo_pkg/__init__.py"
    root = seed_op_repo(tmp_path, monkeypatch)
    write(root, REGISTRY_MAP, f'OP_MODULE_MAP = {{\n    "{OP}": "coordinator_core.ops.demo_pkg",\n}}\n')
    write(root, pkg_init, op_source(NEW_DOC))
    for rel, text in render([contract(OP, "coordinator_core.ops.demo_pkg", NEW_DOC)]).items():
        write(root, rel, text)
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "package op"], root)
    write(root, pkg_init, op_source(NEW_DOC + "\n\nMore."))

    out = landing_render.render_for_landing(root, [pkg_init])

    assert out is not None and "More." in out[CATALOG]


def test_the_catalog_patched_is_the_one_at_head_not_a_peers_worktree_edit(op_repo):
    write(op_repo, CATALOG, '{"generated_by": "peer scribble", "ops": []}\n')
    write(op_repo, OP_MODULE, op_source(NEW_DOC))

    out = landing_render.render_for_landing(op_repo, [OP_MODULE])

    assert out == render(contracts_for(NEW_DOC))


def test_an_op_named_by_a_module_constant_is_located(op_repo):
    src = (
        '"""Module doc."""\n'
        "from coordinator_core.ipc import register_op\n\n"
        f'OP_NAME = "{OP}"\n\n\n'
        "@register_op(OP_NAME)\n"
        "def _echo(params):\n"
        '    """Const named.\n\n    Two lines.\n    """\n'
        "    return params\n"
    )
    write(op_repo, OP_MODULE, src)

    out = landing_render.render_for_landing(op_repo, [OP_MODULE])

    assert out == _expected("Const named.\n\nTwo lines.")


def test_an_op_named_by_a_non_literal_expression_still_refuses(op_repo):
    src = (
        "from coordinator_core.ipc import register_op\n\n"
        '@register_op("demo." + "echo")\n'
        "def _echo(params):\n"
        '    """Dynamic.\n\n    Two.\n    """\n'
        "    return params\n"
    )
    write(op_repo, OP_MODULE, src)

    with pytest.raises(landing_render.RenderRefused):
        landing_render.render_for_landing(op_repo, [OP_MODULE])


def test_the_base_catalog_is_head_in_a_linked_worktree_too(op_repo, tmp_path):
    linked = tmp_path / "linked"
    _git(["worktree", "add", "-q", "--detach", str(linked)], op_repo)
    write(linked, CATALOG, '{"generated_by": "peer scribble", "ops": []}\n')
    write(linked, OP_MODULE, op_source(NEW_DOC))

    out = landing_render.render_for_landing(linked, [OP_MODULE])

    assert out == render(contracts_for(NEW_DOC))


def test_the_render_step_costs_under_50ms_process_time_over_a_569_op_catalog(tmp_path, monkeypatch):
    root = seed_op_repo(tmp_path, monkeypatch, extra_ops=568)
    write(root, OP_MODULE, op_source(NEW_DOC))

    started = time.process_time()
    out = landing_render.render_for_landing(root, [OP_MODULE])
    elapsed = time.process_time() - started

    assert out == render(contracts_for(NEW_DOC, extra_ops=568))
    assert OLD_DOC not in out[CATALOG]
    assert elapsed < 0.05, f"render_for_landing took {elapsed * 1000:.1f}ms process time"
