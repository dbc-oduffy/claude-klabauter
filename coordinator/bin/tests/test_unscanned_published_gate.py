"""test_unscanned_published_gate — tests for
`dispatch_end_of_run_unscanned_published_check`, the third end-of-run leg
(task: close Group A of state/audits/2026-08-05-first-full-payload-identity-
findings.md and the structural gap underneath it -- "the set of files
published equals the set of files the transform sweep actually visited"
has never been asserted anywhere; the publish allowlist and the transform
surface are two independently-maintained tables and nothing compares them).

Drives the REAL `coordinator_core.percolate.surface.iter_surface_files` (no
fake/stub for it -- the whole point of this leg is catching real
`file_surface` gaps, so faking the surface walk would defeat the test).

ADMISSION-INVERTED 2026-08-05 (surface.py module docstring, team-lead ruling): a
published file no longer needs a matching extension or shebang to be in-surface --
admission is default-open, narrowed only by exclusion (symlink/exclude_prefixes/
exclude_basenames/exclude_globs) or content-detected binary. `include_extensions`/
`shebang_prefixes` remain available as OPT-IN narrowing via a row's
`narrow_to_include_extensions: true` (no real store row sets this key today).

Covers:
  * the exact Group A shape (extensionless, no `#!` first line) is now admitted
    by default and passes, not flagged
    (`test_extensionless_no_shebang_file_is_admitted_by_default_and_passes`);
  * a row that opts INTO narrowing still gets the pre-inversion, extension-gated
    behavior (`test_narrowed_row_extensionless_no_shebang_file_still_fails`);
  * content-detected binary is unscanned and fails even under the new default
    (`test_binary_content_is_unscanned_and_fails_even_by_default`);
  * a published file excluded by `exclude_globs` is a hard failure with
    that reason named (`test_exclude_glob_excluded_file_fails_with_reason`);
  * a clean tree (every published file is in-surface by default) passes
    (`test_every_published_file_scanned_passes`);
  * `.git/*` internals are excluded structurally, never flagged
    (`test_git_internals_are_structurally_excluded_not_flagged`);
  * a file whose relative path is in the exceptions list is visible (a
    NOTE, not silence) but does not fail
    (`test_ratified_exception_is_visible_but_not_a_failure`);
  * a malformed exceptions entry (missing `reason`) is a fail-closed load
    error, not a silent skip (`test_malformed_exception_entry_fails_closed`);
  * `--target` filtering does NOT make this leg advisory, unlike the other
    two end-of-run legs (`test_target_filtered_run_still_hard_fails`).

Run: python -m pytest coordinator/bin/tests/test_unscanned_published_gate.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_unscanned_published_gate_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _target(tmp_path: Path, name="t") -> "publish.ResolvedTarget":
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / ".git").mkdir()
    return publish.ResolvedTarget(name=name, mode="mirror", source_dir=src, dest_dir=dst)


class TestUnscannedPublishedCheckDirect:
    def test_extensionless_no_shebang_file_is_admitted_by_default_and_passes(self, tmp_path):
        """ADMISSION-INVERTED 2026-08-05: this is the exact Group A shape
        (state/audits/2026-08-05-first-full-payload-identity-findings.md) --
        an extensionless file with a plain comment, not a `#!` first line.
        `include_extensions` no longer gates admission, so this is now
        in-surface by default and the check must NOT flag it. Was the
        pre-inversion `test_extensionless_no_shebang_file_is_unscanned_and_fails`;
        renamed to assert the fixed behavior rather than the closed bug."""
        target = _target(tmp_path)
        (target.dest_dir / "some-cli-trampoline").write_text(
            "# a comment, not a shebang\nprint('raw claude-klabauter')\n", encoding="utf-8"
        )
        section = {"file_surface": {"include_extensions": ["*.py"]}}

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)], target_filtered=False
        )
        assert ok is True

    def test_narrowed_row_extensionless_no_shebang_file_still_fails(self, tmp_path, capsys):
        """A row that explicitly opts INTO the pre-inversion narrowing
        (`narrow_to_include_extensions: true`) still gets the old,
        extension-gated behavior -- the narrowing opt-out this leg's
        classifier (`_classify_unscanned_reason`) documents as dormant-not-
        dead. No real store row sets this key today; this proves the path
        stays correct for the day one does."""
        target = _target(tmp_path)
        (target.dest_dir / "some-cli-trampoline").write_text(
            "# a comment, not a shebang\nprint('raw claude-klabauter')\n", encoding="utf-8"
        )
        section = {
            "file_surface": {
                "include_extensions": ["*.py"],
                "narrow_to_include_extensions": True,
            }
        }

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)], target_filtered=False
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "some-cli-trampoline" in captured.err
        assert "unscanned-published check FAILED" in captured.err
        assert "narrow_to_include_extensions is set" in captured.err

    def test_binary_content_is_unscanned_and_fails_even_by_default(self, tmp_path, capsys):
        """The one exclusion that survives the inversion unconditionally:
        content-detected binary. No `narrow_to_include_extensions` needed."""
        target = _target(tmp_path)
        (target.dest_dir / "asset.bin").write_bytes(b"\x00\x01binary")
        section = {"file_surface": {}}

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)], target_filtered=False
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "asset.bin" in captured.err
        assert "content-detected binary" in captured.err

    def test_exclude_glob_excluded_file_fails_with_reason(self, tmp_path, capsys):
        target = _target(tmp_path)
        (target.dest_dir / "depersonalize-identity.example.yaml").write_text(
            "# claude-klabauter\n", encoding="utf-8"
        )
        section = {
            "file_surface": {
                "include_extensions": ["*.yaml"],
                "exclude_globs": ["*depersonalize*"],
            }
        }

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)], target_filtered=False
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "depersonalize-identity.example.yaml" in captured.err
        assert "exclude_basenames/exclude_globs" in captured.err

    def test_git_internals_are_structurally_excluded_not_flagged(self, tmp_path):
        """`.git/*` is destination-repo plumbing no row's sync ever touches
        -- excluded at the mechanism level (_STRUCTURAL_NEVER_PUBLISHED_PREFIX),
        not something that should ever need a ratified exception entry.
        First found live on this leg's real-payload run (~20 `.git/*`
        false positives) before this exclusion existed."""
        target = _target(tmp_path)
        (target.dest_dir / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (target.dest_dir / ".git" / "hooks").mkdir()
        (target.dest_dir / ".git" / "hooks" / "pre-commit.sample").write_text(
            "#!/bin/sh\n", encoding="utf-8"
        )
        section = {"file_surface": {"include_extensions": ["*.py"]}}

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)], target_filtered=False
        )
        assert ok is True

    def test_pycache_pyc_is_structurally_excluded_not_flagged(self, tmp_path):
        """`__pycache__/*.pyc` is a locally-generated Python build artifact,
        never copied into a destination clone by any row's sync (source-side
        `.percolate-ignore` already excludes it) -- anything that
        subsequently RUNS Python inside the destination (a pytest run, a
        stray `python -m`) recreates it there with no row ever having
        "published" it. Measured live: a real
        claude-klabauter-publish-repo-toplevel run reported 499 such
        findings, all `__pycache__/*.pyc`, drowning any real finding.
        Structural exclusion (`_is_structurally_never_published`), same
        mechanism as `.git/*`, not a per-file ratified exception."""
        target = _target(tmp_path)
        pycache = target.dest_dir / "coordinator_core" / "__pycache__"
        pycache.mkdir(parents=True)
        (pycache / "__init__.cpython-313.pyc").write_bytes(b"\x00fake bytecode")
        (target.dest_dir / "stray.pyo").write_bytes(b"\x00also bytecode")
        section = {"file_surface": {"include_extensions": ["*.py"]}}

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)], target_filtered=False
        )
        assert ok is True

    def test_unscanned_source_file_still_fails_alongside_pycache_noise(self, tmp_path, capsys):
        """The direction that proves the pycache fix is a narrow noise
        exclusion, not a hole: a real unscanned SOURCE file (`.md`/`.py`)
        sitting right next to `__pycache__` noise in the same tree must
        still fail the gate, loudly, and the pycache file must not appear
        in the failure output."""
        target = _target(tmp_path)
        pycache = target.dest_dir / "coordinator_core" / "__pycache__"
        pycache.mkdir(parents=True)
        (pycache / "__init__.cpython-313.pyc").write_bytes(b"\x00fake bytecode")
        section = {"file_surface": {}}
        (target.dest_dir / "unscanned_leak.py").write_text(
            "print('this should have been scanned')\n", encoding="utf-8"
        )

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)],
            target_filtered=False,
            visited_files_by_repo_root={publish._dest_repo_root(target.dest_dir) or target.dest_dir: set()},
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "unscanned_leak.py" in captured.err
        assert "unscanned-published check FAILED" in captured.err
        assert "__pycache__" not in captured.err
        assert ".pyc" not in captured.err

    def test_every_published_file_scanned_passes(self, tmp_path):
        target = _target(tmp_path)
        (target.dest_dir / "a.py").write_text("print('clean')\n", encoding="utf-8")
        (target.dest_dir / "README.md").write_text("# clean\n", encoding="utf-8")
        section = {"file_surface": {"include_extensions": ["*.py", "*.md"]}}

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)], target_filtered=False
        )
        assert ok is True

    def test_ratified_exception_is_visible_but_not_a_failure(self, tmp_path, monkeypatch, capsys):
        target = _target(tmp_path)
        (target.dest_dir / "depersonalize-identity.example.yaml").write_text(
            "# claude-klabauter\n", encoding="utf-8"
        )
        section = {
            "file_surface": {
                "include_extensions": ["*.yaml"],
                "exclude_globs": ["*depersonalize*"],
            }
        }
        monkeypatch.setattr(
            publish,
            "_load_unscanned_exceptions",
            lambda: {
                "depersonalize-identity.example.yaml": "Example config file, intentionally raw -- ratified 2026-08-05."
            },
        )

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)], target_filtered=False
        )
        assert ok is True
        captured = capsys.readouterr()
        assert "NOTE" in captured.err
        assert "DELIBERATE exclusion" in captured.err
        assert "ratified 2026-08-05" in captured.err

    def test_malformed_exception_entry_fails_closed(self, tmp_path, monkeypatch, capsys):
        target = _target(tmp_path)
        (target.dest_dir / "a.txt").write_text("x\n", encoding="utf-8")
        section = {"file_surface": {"include_extensions": ["*.py"]}}

        def _raise():
            raise ValueError("entry missing 'reason'")

        monkeypatch.setattr(publish, "_load_unscanned_exceptions", _raise)

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)], target_filtered=False
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "unscanned-published check unavailable" in captured.err

    def test_target_filtered_run_still_hard_fails(self, tmp_path, capsys):
        """Unlike the identity and install-doc-payload legs, --target
        filtering does not make this leg advisory -- each finding is a
        property of one row's own dest tree, with no cross-row dependency
        to legitimately excuse it. Uses binary content (the one exclusion
        that survives the admission inversion unconditionally) rather than
        an extensionless-no-shebang fixture, since that shape is now
        correctly admitted by default (§ test_extensionless_no_shebang_file_
        is_admitted_by_default_and_passes) and would not exercise a failure
        at all."""
        target = _target(tmp_path)
        (target.dest_dir / "asset.bin").write_bytes(b"\x00binary")
        section = {"file_surface": {}}

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)], target_filtered=True
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "FAILED" in captured.err
        assert "advisory" not in captured.err

    def test_visited_files_by_repo_root_catches_file_never_actually_visited(self, tmp_path):
        """The unscanned-published guard-hole fix (cross-repo/inbox "the store is
        ours and inject bypasses the sweep" follow-on): when the caller supplies
        `visited_files_by_repo_root`, a published file that is ELIGIBLE under the
        row's file_surface params but was never actually recorded as visited (e.g.
        an injected file that landed after the real sweep already ran) is a hard
        failure -- the exact gap the pre-fix re-derivation could not see, because
        re-deriving eligibility at check time cannot tell "visited" from "merely
        matches the same params right now" apart."""
        target = _target(tmp_path)
        (target.dest_dir / "scrubbed.py").write_text("print('clean')\n", encoding="utf-8")
        (target.dest_dir / "injected-but-never-scrubbed.py").write_text(
            "print('raw')\n", encoding="utf-8"
        )
        section = {"file_surface": {"include_extensions": ["*.py"]}}
        repo_root = publish._dest_repo_root(target.dest_dir) or target.dest_dir

        # Only "scrubbed.py" was actually visited this run -- the other file is
        # published (on disk) and ELIGIBLE (matches *.py) but was never recorded.
        visited_files_by_repo_root = {repo_root: {target.dest_dir / "scrubbed.py"}}

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)],
            target_filtered=False,
            visited_files_by_repo_root=visited_files_by_repo_root,
        )
        assert ok is False

    def test_visited_files_by_repo_root_passes_when_every_published_file_recorded(self, tmp_path):
        target = _target(tmp_path)
        (target.dest_dir / "scrubbed.py").write_text("print('clean')\n", encoding="utf-8")
        section = {"file_surface": {"include_extensions": ["*.py"]}}
        repo_root = publish._dest_repo_root(target.dest_dir) or target.dest_dir

        visited_files_by_repo_root = {repo_root: {target.dest_dir / "scrubbed.py"}}

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)],
            target_filtered=False,
            visited_files_by_repo_root=visited_files_by_repo_root,
        )
        assert ok is True

    def test_visited_files_by_repo_root_never_merged_with_eligibility_rederivation(self, tmp_path):
        """A file matching the row's file_surface params but ABSENT from the
        supplied visited set must fail even though the pre-fix `iter_surface_files`
        re-derivation would happily call it eligible -- proves the fix does not
        fall back to (or merge with) eligibility re-derivation once a real visited
        set is supplied for this repo root."""
        target = _target(tmp_path)
        (target.dest_dir / "eligible_but_unvisited.py").write_text("print('x')\n", encoding="utf-8")
        section = {"file_surface": {"include_extensions": ["*.py"]}}
        repo_root = publish._dest_repo_root(target.dest_dir) or target.dest_dir

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(target, section)],
            target_filtered=False,
            visited_files_by_repo_root={repo_root: set()},
        )
        assert ok is False

    def test_multiple_rows_same_repo_root_union_scanned_sets(self, tmp_path):
        """Two rows sharing a repo root (a subdir row + a toplevel row) with
        DIFFERENT file_surface params -- a file only one row's params would
        catch must still count as scanned (union, not per-row exclusivity)."""
        src = tmp_path / "src"
        src.mkdir()
        repo_root = tmp_path / "dst"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        sub_dir = repo_root / "sub"
        sub_dir.mkdir()
        (sub_dir / "only_yaml_row_catches.yaml").write_text("x: 1\n", encoding="utf-8")

        row_a = publish.ResolvedTarget(name="a", mode="mirror", source_dir=src, dest_dir=sub_dir)
        section_a = {"file_surface": {"include_extensions": ["*.py"]}}  # would NOT catch .yaml
        row_b = publish.ResolvedTarget(name="b", mode="flat-mirror", source_dir=src, dest_dir=repo_root)
        section_b = {"file_surface": {"include_extensions": ["*.yaml"]}}  # catches it

        ok = publish.dispatch_end_of_run_unscanned_published_check(
            [(row_a, section_a), (row_b, section_b)], target_filtered=False
        )
        assert ok is True


class TestLoadUnscannedExceptions:
    def test_absent_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(publish, "_UNSCANNED_EXCEPTIONS_PATH", tmp_path / "absent.yaml")
        assert publish._load_unscanned_exceptions() == {}

    def test_well_formed_entry_loads(self, tmp_path, monkeypatch):
        p = tmp_path / "exceptions.yaml"
        p.write_text(
            "exceptions:\n  - path: \"a/b.txt\"\n    reason: \"ratified example\"\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(publish, "_UNSCANNED_EXCEPTIONS_PATH", p)
        assert publish._load_unscanned_exceptions() == {"a/b.txt": "ratified example"}

    def test_missing_reason_raises(self, tmp_path, monkeypatch):
        p = tmp_path / "exceptions.yaml"
        p.write_text("exceptions:\n  - path: \"a/b.txt\"\n", encoding="utf-8")
        monkeypatch.setattr(publish, "_UNSCANNED_EXCEPTIONS_PATH", p)
        with pytest.raises(ValueError):
            publish._load_unscanned_exceptions()

    def test_missing_path_raises(self, tmp_path, monkeypatch):
        p = tmp_path / "exceptions.yaml"
        p.write_text("exceptions:\n  - reason: \"no path given\"\n", encoding="utf-8")
        monkeypatch.setattr(publish, "_UNSCANNED_EXCEPTIONS_PATH", p)
        with pytest.raises(ValueError):
            publish._load_unscanned_exceptions()

    def test_real_repo_exceptions_file_has_only_ratified_entries(self):
        """Sanity check against the REAL, checked-in exceptions file (not a
        fixture): every entry present is one a human has actually ratified, and
        no other.

        Entry 1, `.github/scripts/check-persona-names.py`, ADR'd as AC4/AC5 of
        docs/plans/2026-08-03-mirror-native-content-homed-and-injected.md (the
        checker must never scrub itself).

        Entry 2, `bin/depersonalize-identity.example.yaml`, was Group B of
        state/audits/2026-08-05-first-full-payload-identity-findings.md and
        stayed deliberately UNRATIFIED until example-doctrine-repo's EM and PM confirmed
        the exclusion (cross-repo/inbox/2026-08-05-example-doctrine-repo-em-three-publish-
        gates-block-the-4-0-0-percolate.md): the file is the per-operator
        identity-rewrite data-file TEMPLATE, whose body is worked substitution
        examples, so the depersonalize transform would rewrite its own
        illustrative values. Note the path form -- destination-repo-root-
        relative (`bin/...`, not `coordinator/bin/...`), which is what
        `dispatch_end_of_run_unscanned_published_check` compares against; the
        `coordinator/`-prefixed form would load fine and match nothing."""
        loaded = publish._load_unscanned_exceptions()
        assert set(loaded) == {
            ".github/scripts/check-persona-names.py",
            "bin/depersonalize-identity.example.yaml",
        }
        assert "AC4" in loaded[".github/scripts/check-persona-names.py"]
        assert "AC5" in loaded[".github/scripts/check-persona-names.py"]
