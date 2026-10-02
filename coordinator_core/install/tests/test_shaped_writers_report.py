"""Every writer with a runtime-shaped clause journals what it resolved.

A shaped-clause writer that never calls `record_resolution` reads as
"did not report" in the install receipt on every run.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.install.write_surface import ShapedClause
from coordinator_core.install.write_surface_discovery import discover_declarations

_REPO = Path(__file__).resolve().parents[3]

#: writer_id -> why it is not an install-run writer.
_NOT_AN_INSTALL_RUN_WRITER = {
    "forwarder-self-heal": "runs at session start, never inside an install run; journaling there would append to an install journal outside any install",
}


def _module_source(source_module: str) -> str:
    rel = Path(*source_module.split("."))
    for candidate in (_REPO / rel.with_suffix(".py"), _REPO / rel):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return ""


def test_every_shaped_writer_journals_a_resolution():
    declarations, failures = discover_declarations(_REPO)
    assert failures == []
    silent = sorted(
        writer_id
        for writer_id, decl in declarations.items()
        if any(isinstance(c, ShapedClause) for c in decl.clauses)
        and writer_id not in _NOT_AN_INSTALL_RUN_WRITER
        and "record_resolution" not in _module_source(decl.source_module)
    )
    assert silent == [], f"shaped-clause writers that never call record_resolution: {silent}"


def test_the_allowlist_names_only_writers_that_still_exist():
    declarations, _ = discover_declarations(_REPO)
    assert set(_NOT_AN_INSTALL_RUN_WRITER) <= set(declarations)


def test_live_plugin_registration_journals_even_when_it_wrote_nothing(tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as rj
    from coordinator_core.install.live_plugin_registration import assert_live_plugin_registration

    sh = tmp_path / "sh"
    sh.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(sh))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.delenv(rj.RESOLUTION_JOURNAL_ENV_VAR, raising=False)

    report = assert_live_plugin_registration(tmp_path / "claude", tmp_path / "live")

    assert report["status"] == "absent"
    assert rj.read_journal()["live-plugin-registration"][0].entries == ()


def test_a_journal_row_every_shaped_template_admits_builds_a_receipt():
    """A template whose literal prefix rejects the writer's own concrete paths
    or keys makes `build_receipt` raise, and the whole receipt is lost."""
    from coordinator_core.install.receipt import _template_prefix

    declarations, _ = discover_declarations(_REPO)
    for writer_id, decl in declarations.items():
        for index, clause in enumerate(decl.clauses):
            if not isinstance(clause, ShapedClause):
                continue
            template = clause.entry_template
            for field_name, prefix in (("path", _template_prefix(template.path)), ("key", _template_prefix(template.key))):
                value = getattr(template, field_name)
                assert not (prefix and prefix == value), (
                    f"{writer_id} clause {index}: {field_name} {value!r} has no <placeholder>, "
                    "so only that exact literal passes the resolution check"
                )
                assert not (prefix and prefix.startswith("$")), (
                    f"{writer_id} clause {index}: {field_name} prefix {prefix!r} is not a concrete path"
                )
