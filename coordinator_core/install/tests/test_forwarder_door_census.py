"""Tests for `coordinator_core.install.forwarder_door_census`.

Covers: the four-bucket classification logic against synthetic fixture
scripts (never the real 968-entry `coordinator/bin/`, which drifts), the
`(a)`/`(b)` axis evidence recorded per row, JSON/table rendering, and the
allowlist-population union behaviour (`_write_allowlist` never drops a
pre-existing entry, e.g. C1's seeded `cross-repo-memo`).
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.install import forwarder_door_census as fdc


def _write(tmp_path, name: str, body: str):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


class TestAxisA:
    def test_no_client_side_work_passes(self, tmp_path):
        _write(
            tmp_path,
            "clean.py",
            'def main(argv=None):\n    return 0\n\nif __name__ == "__main__":\n    import sys\n    sys.exit(main())\n',
        )
        v = fdc.classify_one("clean", "clean.py", bin_dir=tmp_path)
        assert v.op_equivalent is True
        assert v.op_equivalent_evidence == ()

    def test_stat_call_fails_a(self, tmp_path):
        _write(
            tmp_path,
            "statter.py",
            "import os\n\ndef main(argv=None):\n    st = os.stat('x')\n    return 0\n",
        )
        v = fdc.classify_one("statter", "statter.py", bin_dir=tmp_path)
        assert v.op_equivalent is False
        assert any("os.stat" in e for e in v.op_equivalent_evidence)

    def test_subprocess_call_fails_a(self, tmp_path):
        _write(
            tmp_path,
            "shelling.py",
            "import subprocess\n\ndef main(argv=None):\n    subprocess.run(['git', 'status'])\n    return 0\n",
        )
        v = fdc.classify_one("shelling", "shelling.py", bin_dir=tmp_path)
        assert v.op_equivalent is False
        assert any("subprocess.run" in e for e in v.op_equivalent_evidence)

    def test_glob_and_rglob_fail_a(self, tmp_path):
        _write(
            tmp_path,
            "globber.py",
            "from pathlib import Path\n\ndef main(argv=None):\n    list(Path('.').rglob('*.py'))\n    return 0\n",
        )
        v = fdc.classify_one("globber", "globber.py", bin_dir=tmp_path)
        assert v.op_equivalent is False
        assert any(".rglob" in e for e in v.op_equivalent_evidence)


class TestAxisB:
    def test_clean_module_passes_b(self, tmp_path):
        _write(
            tmp_path,
            "clean.py",
            'def main(argv=None):\n    return 0\n\nif __name__ == "__main__":\n    import sys\n    sys.exit(main())\n',
        )
        v = fdc.classify_one("clean", "clean.py", bin_dir=tmp_path)
        assert v.warm_loadable is True
        assert v.warm_loadable_evidence == ()

    def test_top_level_sys_path_insert_fails_b(self, tmp_path):
        _write(
            tmp_path,
            "pathmut.py",
            "import sys\nsys.path.insert(0, 'x')\n\ndef main(argv=None):\n    return 0\n",
        )
        v = fdc.classify_one("pathmut", "pathmut.py", bin_dir=tmp_path)
        assert v.warm_loadable is False
        assert any("sys.path.insert" in e for e in v.warm_loadable_evidence)

    def test_top_level_hard_sys_exit_fails_b(self, tmp_path):
        _write(
            tmp_path,
            "hardexit.py",
            "import sys\n\nif True:\n    sys.exit(1)\n\ndef main(argv=None):\n    return 0\n",
        )
        v = fdc.classify_one("hardexit", "hardexit.py", bin_dir=tmp_path)
        assert v.warm_loadable is False
        assert any("sys.exit" in e for e in v.warm_loadable_evidence)

    def test_main_guard_body_never_flagged(self, tmp_path):
        """`sys.exit(main())` inside the `if __name__ == "__main__":` guard
        never executes under `exec_module` -- must not be flagged."""
        _write(
            tmp_path,
            "guarded.py",
            'import sys\n\ndef main(argv=None):\n    return 0\n\nif __name__ == "__main__":\n    sys.exit(main())\n',
        )
        v = fdc.classify_one("guarded", "guarded.py", bin_dir=tmp_path)
        assert v.warm_loadable is True

    def test_module_level_call_expr_fails_b(self, tmp_path):
        _write(
            tmp_path,
            "sideeffect.py",
            "import logging\nlogging.basicConfig()\n\ndef main(argv=None):\n    return 0\n",
        )
        v = fdc.classify_one("sideeffect", "sideeffect.py", bin_dir=tmp_path)
        assert v.warm_loadable is False

    def test_global_at_top_level_fails_b(self, tmp_path):
        _write(
            tmp_path,
            "globaluse.py",
            "global _X\n_X = 1\n\ndef main(argv=None):\n    return 0\n",
        )
        v = fdc.classify_one("globaluse", "globaluse.py", bin_dir=tmp_path)
        assert v.warm_loadable is False
        assert any("interpreter-global mutation" in e for e in v.warm_loadable_evidence)


class TestBucketing:
    def test_both_pass_is_door_eligible(self, tmp_path):
        _write(tmp_path, "clean.py", "def main(argv=None):\n    return 0\n")
        v = fdc.classify_one("clean", "clean.py", bin_dir=tmp_path)
        assert v.bucket == "door-eligible"

    def test_a_fails_only_is_needs_op_extension(self, tmp_path):
        _write(
            tmp_path,
            "statter.py",
            "import os\n\ndef main(argv=None):\n    os.stat('x')\n    return 0\n",
        )
        v = fdc.classify_one("statter", "statter.py", bin_dir=tmp_path)
        assert v.bucket == "needs-op-extension"

    def test_b_fails_only_is_needs_warm_safety(self, tmp_path):
        _write(
            tmp_path,
            "pathmut.py",
            "import sys\nsys.path.insert(0, 'x')\n\ndef main(argv=None):\n    return 0\n",
        )
        v = fdc.classify_one("pathmut", "pathmut.py", bin_dir=tmp_path)
        assert v.bucket == "needs-warm-safety"

    def test_both_fail_is_engine_unreachable(self, tmp_path):
        _write(
            tmp_path,
            "bad.py",
            "import sys, os\nsys.path.insert(0, 'x')\n\ndef main(argv=None):\n    os.stat('x')\n    return 0\n",
        )
        v = fdc.classify_one("bad", "bad.py", bin_dir=tmp_path)
        assert v.bucket == "engine-unreachable"

    def test_syntax_error_is_engine_unreachable(self, tmp_path):
        _write(tmp_path, "broken.py", "def main(:\n    pass\n")
        v = fdc.classify_one("broken", "broken.py", bin_dir=tmp_path)
        assert v.bucket == "engine-unreachable"
        assert v.scan_error is not None

    def test_missing_script_is_engine_unreachable(self, tmp_path):
        v = fdc.classify_one("ghost", "ghost.py", bin_dir=tmp_path)
        assert v.bucket == "engine-unreachable"
        assert v.scan_error is not None


class TestCrossRepoMemoCanonicalExample:
    """DR-365's canonical (a)-failure: `cross-repo-memo list`'s own mtime
    pass over candidates the op returns unsorted (see that module's own
    docstring: "Op returns candidates sorted by FILENAME with no mtime/age/
    stale -- the CLI reproduces the historical mtime-based UX with a
    minimal stat pass"). Exercised against the REAL file since it is the
    concrete example the dispatch brief cites -- if this file is ever
    rewritten to no longer do that stat pass, this test should be revisited
    alongside it, not silently left green on stale reasoning."""

    def test_real_cross_repo_memo_fails_axis_a(self):
        from pathlib import Path

        bin_dir = fdc._BIN_DIR
        script = bin_dir / "cross-repo-memo.py"
        if not script.is_file():
            pytest.skip("coordinator/bin/cross-repo-memo.py not present in this checkout")
        v = fdc.classify_one("cross-repo-memo", "cross-repo-memo.py", bin_dir=bin_dir)
        assert v.op_equivalent is False
        assert any("os.stat" in e for e in v.op_equivalent_evidence)


class TestRunCensus:
    def test_run_census_classifies_every_derived_name(self, tmp_path):
        _write(tmp_path, "one.py", "def main(argv=None):\n    return 0\n")
        _write(tmp_path, "two.py", "import os\n\ndef main(argv=None):\n    os.stat('x')\n    return 0\n")
        verdicts = fdc.run_census(bin_dir=tmp_path)
        names = {v.name for v in verdicts}
        assert names == {"one", "two"}

    def test_bucket_counts_sum_to_total(self, tmp_path):
        _write(tmp_path, "one.py", "def main(argv=None):\n    return 0\n")
        _write(tmp_path, "two.py", "import os\n\ndef main(argv=None):\n    os.stat('x')\n    return 0\n")
        verdicts = fdc.run_census(bin_dir=tmp_path)
        counts = fdc.bucket_counts(verdicts)
        assert sum(counts.values()) == len(verdicts) == 2

    def test_door_eligible_names_sorted(self, tmp_path):
        _write(tmp_path, "b.py", "def main(argv=None):\n    return 0\n")
        _write(tmp_path, "a.py", "def main(argv=None):\n    return 0\n")
        verdicts = fdc.run_census(bin_dir=tmp_path)
        assert fdc.door_eligible_names(verdicts) == ("a", "b")


class TestRendering:
    def test_to_json_round_trips_and_carries_counts(self, tmp_path):
        _write(tmp_path, "one.py", "def main(argv=None):\n    return 0\n")
        verdicts = fdc.run_census(bin_dir=tmp_path)
        payload = json.loads(fdc.to_json(verdicts))
        assert payload["counts"]["door-eligible"] == 1
        assert payload["rows"][0]["name"] == "one"

    def test_render_table_states_performance_axis_disclaimer(self, tmp_path):
        _write(tmp_path, "one.py", "def main(argv=None):\n    return 0\n")
        verdicts = fdc.run_census(bin_dir=tmp_path)
        table = fdc.render_table(verdicts)
        assert "PERFORMANCE AXIS, NOT A COVERAGE AXIS" in table
        assert "one" in table


class TestAllowlistPopulation:
    def test_write_allowlist_unions_with_existing_seed(self, tmp_path):
        allowlist_path = tmp_path / "warm_entrypoint_allowlist.json"
        allowlist_path.write_text(
            json.dumps({"entrypoints": ["cross-repo-memo"]}), encoding="utf-8"
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write(bin_dir, "eligible-one.py", "def main(argv=None):\n    return 0\n")
        _write(
            bin_dir,
            "ineligible-one.py",
            "import os\n\ndef main(argv=None):\n    os.stat('x')\n    return 0\n",
        )
        verdicts = fdc.run_census(bin_dir=bin_dir)

        merged = fdc._write_allowlist(verdicts, allowlist_path=allowlist_path)

        assert "cross-repo-memo" in merged
        assert "eligible-one" in merged
        assert "ineligible-one" not in merged

        on_disk = json.loads(allowlist_path.read_text(encoding="utf-8"))
        assert set(on_disk["entrypoints"]) == set(merged)

    def test_write_allowlist_creates_when_absent(self, tmp_path):
        allowlist_path = tmp_path / "fresh_allowlist.json"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write(bin_dir, "eligible-one.py", "def main(argv=None):\n    return 0\n")
        verdicts = fdc.run_census(bin_dir=bin_dir)

        merged = fdc._write_allowlist(verdicts, allowlist_path=allowlist_path)

        assert merged == ("eligible-one",)
        assert allowlist_path.is_file()


class TestNegativeSpecNeverCountsInstalledFiles:
    def test_run_census_never_touches_settings_home(self, tmp_path, monkeypatch):
        """Classification must derive from `bin_dir` (generator state) only
        -- pointing HOME at a directory with no settings-home bin/ must not
        raise or otherwise attempt to read an installed-files listing."""
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "nonexistent-settings-home"))
        _write(tmp_path, "one.py", "def main(argv=None):\n    return 0\n")
        verdicts = fdc.run_census(bin_dir=tmp_path)
        assert len(verdicts) == 1
