"""Unit tests for ``coordinator_core.install.substrate``'s settings-home
seed-wiki population step (``_load_seed_wiki_manifest`` /
``_install_seed_wikis``).

Never a directory glob: the manifest (``schemas/seed-wikis.json``) is the
single source of truth for which ``docs/wiki/`` pages are ratified for
cross-repo/cross-machine citation — a wholesale copy of every ``*.md`` under
``docs/wiki/`` would make non-seed pages resolve locally while still
404-ing for an OSS/sibling-repo reader, which is exactly the defect this
manifest exists to close. All fixtures below build a synthetic
``<tmp_path>/plugin_root`` and ``<tmp_path>/settings_home`` — never the real
settings home or ``Path.home()``.
"""

from __future__ import annotations

import json

from coordinator_core.install.substrate import (
    SubstrateFatalError,
    _install_seed_wikis,
    _load_seed_wiki_manifest,
)


def _write_manifest(plugin_root, pages) -> None:
    schemas = plugin_root / "schemas"
    schemas.mkdir(parents=True, exist_ok=True)
    (schemas / "seed-wikis.json").write_text(
        json.dumps({"schema_version": 1, "seed_wikis": pages}), encoding="utf-8"
    )


def _write_wiki_page(plugin_root, name, content="content\n") -> None:
    wiki = plugin_root / "docs" / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / name).write_text(content, encoding="utf-8")


# --- happy path --------------------------------------------------------


def test_happy_path_copies_exactly_the_manifest_pages(tmp_path):
    plugin_root = tmp_path / "plugin_root"
    settings_home = tmp_path / "settings_home"
    pages = ["daily-branch-discipline.md", "scoped-safety-commits.md"]
    _write_manifest(plugin_root, pages)
    for name in pages:
        _write_wiki_page(plugin_root, name, f"body of {name}\n")
    # A non-seed page must NOT be copied even though it sits right next to
    # the seed pages on disk — this is the glob-vs-manifest distinction.
    _write_wiki_page(plugin_root, "internal-only-page.md", "must not leak\n")

    _install_seed_wikis(plugin_root, settings_home, check_only=False)

    dst_wiki = settings_home / "coordinator-claude" / "docs" / "wiki"
    installed = sorted(p.name for p in dst_wiki.iterdir())
    assert installed == sorted(pages)
    for name in pages:
        assert (dst_wiki / name).read_text(encoding="utf-8") == f"body of {name}\n"


# --- page listed but missing on disk ------------------------------------


def test_page_listed_but_missing_on_disk_is_reported_and_others_still_install(tmp_path, capsys):
    plugin_root = tmp_path / "plugin_root"
    settings_home = tmp_path / "settings_home"
    _write_manifest(plugin_root, ["present.md", "ghost.md"])
    _write_wiki_page(plugin_root, "present.md", "here\n")
    # "ghost.md" is named in the manifest but never written to disk.

    _install_seed_wikis(plugin_root, settings_home, check_only=False)

    dst_wiki = settings_home / "coordinator-claude" / "docs" / "wiki"
    assert (dst_wiki / "present.md").read_text(encoding="utf-8") == "here\n"
    assert not (dst_wiki / "ghost.md").exists()
    err = capsys.readouterr().err
    assert "ghost.md" in err
    assert "absent" in err


# --- malformed / absent manifest ----------------------------------------


def test_absent_manifest_raises_fatal(tmp_path):
    plugin_root = tmp_path / "plugin_root"
    settings_home = tmp_path / "settings_home"
    plugin_root.mkdir()

    try:
        _install_seed_wikis(plugin_root, settings_home, check_only=False)
        assert False, "expected SubstrateFatalError"
    except SubstrateFatalError as exc:
        assert "seed-wikis.json not found" in str(exc)


def test_malformed_json_raises_fatal(tmp_path):
    plugin_root = tmp_path / "plugin_root"
    settings_home = tmp_path / "settings_home"
    schemas = plugin_root / "schemas"
    schemas.mkdir(parents=True)
    (schemas / "seed-wikis.json").write_text("{not valid json", encoding="utf-8")

    try:
        _load_seed_wiki_manifest(plugin_root)
        assert False, "expected SubstrateFatalError"
    except SubstrateFatalError as exc:
        assert "malformed" in str(exc)


def test_manifest_missing_seed_wikis_key_raises_fatal(tmp_path):
    plugin_root = tmp_path / "plugin_root"
    schemas = plugin_root / "schemas"
    schemas.mkdir(parents=True)
    (schemas / "seed-wikis.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    try:
        _load_seed_wiki_manifest(plugin_root)
        assert False, "expected SubstrateFatalError"
    except SubstrateFatalError as exc:
        assert "seed_wikis" in str(exc)


def test_manifest_empty_seed_wikis_list_raises_fatal(tmp_path):
    plugin_root = tmp_path / "plugin_root"
    _write_manifest(plugin_root, [])

    try:
        _load_seed_wiki_manifest(plugin_root)
        assert False, "expected SubstrateFatalError"
    except SubstrateFatalError:
        pass


# --- check_only writes nothing -------------------------------------------


def test_check_only_writes_nothing(tmp_path, capsys):
    plugin_root = tmp_path / "plugin_root"
    settings_home = tmp_path / "settings_home"
    _write_manifest(plugin_root, ["a.md"])
    _write_wiki_page(plugin_root, "a.md", "hello\n")

    _install_seed_wikis(plugin_root, settings_home, check_only=True)

    dst_wiki = settings_home / "coordinator-claude" / "docs" / "wiki"
    assert not dst_wiki.exists()
    out = capsys.readouterr().out
    assert "would:" in out
    assert "a.md" in out


def test_check_only_reports_missing_page_without_writing(tmp_path, capsys):
    plugin_root = tmp_path / "plugin_root"
    settings_home = tmp_path / "settings_home"
    _write_manifest(plugin_root, ["ghost.md"])

    _install_seed_wikis(plugin_root, settings_home, check_only=True)

    assert not settings_home.exists()
    err = capsys.readouterr().err
    assert "ghost.md" in err


# --- idempotent re-run ----------------------------------------------------


def test_idempotent_rerun_overwrites_stale_seed_copy(tmp_path):
    """Seed wiki copies are a derived cache, not operator-customized content
    (unlike coordinator-whoami) — a re-run must overwrite a stale
    destination rather than preserving it."""
    plugin_root = tmp_path / "plugin_root"
    settings_home = tmp_path / "settings_home"
    _write_manifest(plugin_root, ["a.md"])
    _write_wiki_page(plugin_root, "a.md", "version one\n")

    _install_seed_wikis(plugin_root, settings_home, check_only=False)
    dst = settings_home / "coordinator-claude" / "docs" / "wiki" / "a.md"
    assert dst.read_text(encoding="utf-8") == "version one\n"

    # Simulate operator/editor drift at the destination, then a doctrine
    # update to the source — re-run must win, not preserve the stale copy.
    dst.write_text("operator-modified stale copy\n", encoding="utf-8")
    _write_wiki_page(plugin_root, "a.md", "version two\n")

    _install_seed_wikis(plugin_root, settings_home, check_only=False)

    assert dst.read_text(encoding="utf-8") == "version two\n"


def test_rerun_is_noop_when_already_up_to_date(tmp_path):
    plugin_root = tmp_path / "plugin_root"
    settings_home = tmp_path / "settings_home"
    _write_manifest(plugin_root, ["a.md"])
    _write_wiki_page(plugin_root, "a.md", "stable\n")

    _install_seed_wikis(plugin_root, settings_home, check_only=False)
    _install_seed_wikis(plugin_root, settings_home, check_only=False)

    dst = settings_home / "coordinator-claude" / "docs" / "wiki" / "a.md"
    assert dst.read_text(encoding="utf-8") == "stable\n"
