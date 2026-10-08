"""Coverage for `coordinator-doc-new --type spinoff --from-sizing`
(docs/plans/2026-10-01-sizing-mints-baton.md § C2): idempotent baton mint with the
sizing's deliverable_id, a `sizing_object` citation, the reverse `baton:` edge,
and one refusal naming every failing field.

Loaded by file path: `coordinator-doc-new` is an extensionless polyglot entrypoint.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_from_sizing.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_CLI_PATH = Path(__file__).resolve().parent.parent / "coordinator-doc-new.py"
_NO_CONSOLE = no_console_creationflags()
_SIZING_REL = "state/sizings/2026-10-01-fixture.yaml"
_DLV = "dlv-fixture-abc123"


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("cdn_from_sizing_test", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader("cdn_from_sizing_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli()


def _sizing_text(tshirt: str | None = "M", intent: str | None = "Mint a baton from a sizing",
                 name: str | None = "baton from sizing", baton: str | None = None) -> str:
    lines = ["schema: sizing-object"]
    if name is not None:
        lines.append(f'name: "{name}"')
    if intent is not None:
        lines.append(f'intent: "{intent}"')
    lines.append("estimate:")
    if tshirt is not None:
        lines.append(f"  tshirt: {tshirt}")
    lines += [
        "  provisional: true",
        "route: plan",
        "detents: []",
        "fork: null",
        "xl_exit: null",
        "status: sized",
        "premise:",
        "  provenance: not-applicable",
        "  evidence: fixture",
        f'deliverable_id: "{_DLV}"',
    ]
    if baton is not None:
        lines.append(f'baton: "{baton}"')
    return "\n".join(lines) + "\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True, **_NO_CONSOLE)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    return tmp_path


def _put(repo: Path, text: str) -> Path:
    path = repo / _SIZING_REL
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _frontmatter(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])


def test_m_sizing_mints_with_inherited_id_citation_and_edge(repo):
    sizing = _put(repo, _sizing_text())
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))

    assert result["created"] is True
    assert result["title"] == "baton from sizing"
    baton = repo / result["path"]
    assert result["path"].startswith("state/handoffs/") and baton.is_file()
    fm = _frontmatter(baton)
    assert fm["deliverable_id"] == _DLV
    assert fm["sizing_object"] == _SIZING_REL
    assert fm["handoff_id"] == result["id"]
    assert fm["summary"] == "Mint a baton from a sizing"
    assert "## What this covers\n\nMint a baton from a sizing\n" in baton.read_text(encoding="utf-8")
    edge = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert edge["baton"] == result["path"]
    assert edge["status"] == "sized" and "plan" not in edge


def test_second_call_returns_same_baton_and_writes_nothing(repo):
    sizing = _put(repo, _sizing_text())
    first = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    before = sizing.read_bytes()
    second = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))

    assert second["created"] is False
    assert second["path"] == first["path"] and second["id"] == first["id"]
    assert sizing.read_bytes() == before
    assert len(list((repo / "state" / "handoffs").glob("*.md"))) == 1


@pytest.mark.parametrize("tshirt", ["XS", "S"])
def test_small_sizing_is_refused_naming_tshirt(repo, tshirt):
    _put(repo, _sizing_text(tshirt=tshirt))
    with pytest.raises(_cli.SizingMintRefused) as exc:
        _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    assert exc.value.fields == ["estimate.tshirt"]
    assert not (repo / "state" / "handoffs").exists()


@pytest.mark.parametrize(
    "sizing_rel",
    ["../outside.yaml", "state/sizings/../../outside.yaml", "state/handoffs/x.yaml"],
)
def test_sizing_path_outside_state_sizings_is_refused(repo, sizing_rel):
    _put(repo, _sizing_text())
    with pytest.raises(_cli.SizingMintRefused) as exc:
        _cli.mint_baton_from_sizing(sizing_rel, str(repo))
    assert exc.value.fields == ["sizing"]
    assert not (repo / "state" / "handoffs").exists()


def test_missing_tshirt_and_intent_named_in_one_refusal(repo):
    _put(repo, _sizing_text(tshirt=None, intent=None))
    with pytest.raises(_cli.SizingMintRefused) as exc:
        _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    assert exc.value.fields == ["estimate.tshirt", "intent"]
    assert "estimate.tshirt" in str(exc.value) and "intent" in str(exc.value)


def test_dangling_edge_refuses_and_names_the_path(repo):
    _put(repo, _sizing_text(baton="state/handoffs/gone.md"))
    with pytest.raises(_cli.SizingMintRefused) as exc:
        _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    assert exc.value.fields == ["baton"]
    assert "state/handoffs/gone.md" in str(exc.value)


def test_baton_write_failure_leaves_sizing_byte_identical(repo, monkeypatch):
    sizing = _put(repo, _sizing_text())
    before = sizing.read_bytes()

    def _boom(out_abs, content):
        raise OSError("disk full")

    monkeypatch.setattr(_cli, "_write_baton_file", _boom)
    with pytest.raises(OSError):
        _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    assert sizing.read_bytes() == before


def test_title_falls_back_to_intent_without_name(repo):
    _put(repo, _sizing_text(name=None))
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    assert result["title"] == "Mint a baton from a sizing"


def test_long_intent_summary_is_capped_but_body_is_verbatim(repo):
    intent = "word " * 60
    _put(repo, _sizing_text(intent=intent.strip()))
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    baton = repo / result["path"]
    assert len(_frontmatter(baton)["summary"]) <= 140
    assert intent.strip() in baton.read_text(encoding="utf-8")


def test_cli_from_sizing_with_title_refuses(repo):
    _put(repo, _sizing_text())
    proc = subprocess.run(
        [sys.executable, str(_CLI_PATH), "--type", "spinoff", "--from-sizing", _SIZING_REL,
         "--title", "x"],
        cwd=str(repo), capture_output=True, text=True, timeout=60, **_NO_CONSOLE,
    )
    assert proc.returncode != 0
    assert "--title" in proc.stderr
    assert not (repo / "state" / "handoffs").exists()


def test_cli_prints_baton_path_and_refuses_xs(repo, monkeypatch):
    sizing = _put(repo, _sizing_text())
    env_cmd = [sys.executable, str(_CLI_PATH), "--type", "spinoff", "--from-sizing", _SIZING_REL]
    ok = subprocess.run(env_cmd, cwd=str(repo), capture_output=True, text=True, timeout=60,
                        **_NO_CONSOLE)
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.strip() == yaml.safe_load(sizing.read_text(encoding="utf-8"))["baton"]

    _put(repo, _sizing_text(tshirt="XS", name="other"))
    bad = subprocess.run(env_cmd, cwd=str(repo), capture_output=True, text=True, timeout=60,
                         **_NO_CONSOLE)
    assert bad.returncode == 1 and "estimate.tshirt" in bad.stderr


def _baton_for(repo: Path, dlv: str | None = _DLV, handoff_id: str | None = "hnd-existing-1") -> str:
    rel = "state/handoffs/2026-10-01-existing.md"
    lines = ["---", 'title: "existing"']
    if handoff_id:
        lines.append(f"handoff_id: {handoff_id}")
    if dlv:
        lines.append(f"deliverable_id: {dlv}")
    lines += ["---", "", "# existing", ""]
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return rel


def test_baton_kwarg_joins_existing_baton_without_a_new_file(repo):
    sizing = _put(repo, _sizing_text())
    rel = _baton_for(repo)
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo), baton=rel)

    assert result["created"] is False
    assert result["id"] == "hnd-existing-1" and result["path"] == rel
    assert yaml.safe_load(sizing.read_text(encoding="utf-8"))["baton"] == rel
    assert [p.name for p in (repo / "state" / "handoffs").glob("*.md")] == ["2026-10-01-existing.md"]


def test_baton_kwarg_with_differing_deliverable_id_refuses_writing_nothing(repo):
    sizing = _put(repo, _sizing_text())
    rel = _baton_for(repo, dlv="dlv-other-999")
    before = sizing.read_bytes()
    with pytest.raises(_cli.SizingMintRefused) as exc:
        _cli.mint_baton_from_sizing(_SIZING_REL, str(repo), baton=rel)
    assert exc.value.fields == ["deliverable_id"]
    assert sizing.read_bytes() == before


def test_baton_kwarg_refuses_when_edge_names_another_baton(repo):
    sizing = _put(repo, _sizing_text(baton="state/handoffs/2026-10-01-other.md"))
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / "state" / "handoffs" / "2026-10-01-other.md").write_text("---\n---\n", encoding="utf-8")
    rel = _baton_for(repo)
    before = sizing.read_bytes()
    with pytest.raises(_cli.SizingMintRefused) as exc:
        _cli.mint_baton_from_sizing(_SIZING_REL, str(repo), baton=rel)
    assert exc.value.fields == ["baton"]
    assert sizing.read_bytes() == before


@pytest.mark.parametrize("missing", ["handoff_id", "deliverable_id"])
def test_baton_kwarg_refuses_a_baton_lacking_an_id(repo, missing):
    sizing = _put(repo, _sizing_text())
    rel = _baton_for(
        repo, dlv=None if missing == "deliverable_id" else _DLV,
        handoff_id=None if missing == "handoff_id" else "hnd-existing-1",
    )
    before = sizing.read_bytes()
    with pytest.raises(_cli.SizingMintRefused) as exc:
        _cli.mint_baton_from_sizing(_SIZING_REL, str(repo), baton=rel)
    assert exc.value.fields == ["baton"]
    assert sizing.read_bytes() == before


def test_deliverable_id_kwarg_is_carried_without_a_mint_call(repo, monkeypatch):
    text = _sizing_text().replace(f'deliverable_id: "{_DLV}"\n', "")
    _put(repo, text)

    def _no_mint(*_a, **_k):
        raise AssertionError("mint-deliverable-id must not run")

    monkeypatch.setattr(_cli, "_mint_deliverable_id", _no_mint)
    monkeypatch.setattr(_cli, "_mint_deliverable_id_from_title", _no_mint)
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo), deliverable_id="dlv-given-1")
    assert _frontmatter(repo / result["path"])["deliverable_id"] == "dlv-given-1"


def test_deliverable_id_kwarg_differing_from_edge_baton_refuses_without_writing(repo):
    text = _sizing_text(baton="state/handoffs/2026-10-01-existing.md").replace(
        f'deliverable_id: "{_DLV}"\n', ""
    )
    sizing = _put(repo, text)
    _baton_for(repo)
    before = sizing.read_bytes()
    with pytest.raises(_cli.SizingMintRefused) as exc:
        _cli.mint_baton_from_sizing(_SIZING_REL, str(repo), deliverable_id="dlv-given-1")
    assert exc.value.fields == ["deliverable_id"]
    assert sizing.read_bytes() == before


def test_deliverable_id_kwarg_differing_from_sizing_refuses_without_writing(repo):
    _put(repo, _sizing_text())
    with pytest.raises(_cli.SizingMintRefused) as exc:
        _cli.mint_baton_from_sizing(_SIZING_REL, str(repo), deliverable_id="dlv-given-1")
    assert exc.value.fields == ["deliverable_id"]
    assert not (repo / "state" / "handoffs").exists()



_SIBLING_REL = "state/sizings/2026-10-01-fixture-sibling.yaml"


def _put_sibling(repo: Path) -> None:
    (repo / _SIBLING_REL).write_text(_sizing_text(), encoding="utf-8", newline="\n")


def test_sibling_sizings_with_one_intent_mint_distinct_batons(repo):
    _put(repo, _sizing_text())
    _put_sibling(repo)

    first = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    second = _cli.mint_baton_from_sizing(_SIBLING_REL, str(repo))

    assert first["path"] == "state/handoffs/2026-10-01-fixture.md"
    assert second["path"] == "state/handoffs/2026-10-01-fixture-sibling.md"


def test_occupied_baton_path_refuses_leaving_sizing_byte_identical(repo, monkeypatch):
    sizing = _put(repo, _sizing_text())
    before = sizing.read_bytes()
    real_write = _cli._write_baton_file

    def _raced(out_abs, content):
        Path(out_abs).parent.mkdir(parents=True, exist_ok=True)
        Path(out_abs).write_text("a racing mint\n", encoding="utf-8")
        real_write(out_abs, content)

    monkeypatch.setattr(_cli, "_write_baton_file", _raced)
    with pytest.raises(_cli.SizingMintRefused) as exc:
        _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    assert exc.value.fields == ["baton"]
    assert sizing.read_bytes() == before


def test_stray_edge_to_another_sizings_baton_is_replaced(repo):
    _put_sibling(repo)
    other = _cli.mint_baton_from_sizing(_SIBLING_REL, str(repo))
    sizing = _put(repo, _sizing_text(baton=other["path"]))

    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))

    assert result["created"] is True and result["path"] != other["path"]
    assert yaml.safe_load(sizing.read_text(encoding="utf-8"))["baton"] == result["path"]
    assert _frontmatter(repo / other["path"])["sizing_object"] == _SIBLING_REL
