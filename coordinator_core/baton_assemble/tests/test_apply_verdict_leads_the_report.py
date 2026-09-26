
from __future__ import annotations

import json

import coordinator_core.baton_assemble.apply as ba_apply


class TestVerdictLeadsTheJsonReport:
    def test_status_and_verdict_are_the_first_two_keys(self):
        _, report = ba_apply._finalize_report(
            ba_apply.APPLY_EXIT_OK,
            {"results": [1, 2, 3], "landed": ["d1", "d2"], "commit_sha": "e88954833ad8"},
        )

        assert list(report)[:2] == ["status", "verdict"]

    def test_the_head_of_the_printed_json_carries_the_outcome(self):
        _, report = ba_apply._finalize_report(
            ba_apply.APPLY_EXIT_OK,
            {"results": [1, 2, 3], "landed": ["d1", "d2", "d3"], "commit_sha": "e88954833ad8"},
        )

        head = json.dumps(report, indent=2).splitlines()[:3]

        assert any('"status": "ok"' in line for line in head)
        assert any("landed d1, d2, d3" in line for line in head)

    def test_existing_keys_are_preserved_unchanged(self):
        source = {"results": ["r"], "landed": ["d1"], "gates": {"repo_identity": "MATCH"}}

        _, report = ba_apply._finalize_report(ba_apply.APPLY_EXIT_OK, source)

        for key, value in source.items():
            assert report[key] == value

    def test_a_stale_status_or_verdict_on_the_report_cannot_win(self):
        _, report = ba_apply._finalize_report(
            ba_apply.APPLY_EXIT_OK,
            {"status": "stale-nonsense", "verdict": "stale verdict", "landed": ["d1"]},
        )

        assert report["status"] == "ok"
        assert report["verdict"] == "ok — landed d1"
        assert list(report)[:2] == ["status", "verdict"]


class TestReplayIsNotReportedAsWork:
    def test_replayed_directives_are_named_as_not_re_run(self):
        line = ba_apply._verdict_line(
            "ok",
            {
                "landed": ["d1", "d2"],
                "replayed": [{"directive_id": "d1", "reason": "already on disk"}],
            },
        )

        assert line == "ok — landed d1, d2 (d1 replayed, not re-run)"

    def test_a_clean_run_says_nothing_about_replay(self):
        line = ba_apply._verdict_line("ok", {"landed": ["d1"], "replayed": []})

        assert line == "ok — landed d1"


class TestVerdictReachesStderr:
    def test_the_verdict_is_printed_to_stderr(self, capsys):
        ba_apply._finalize_report(
            ba_apply.APPLY_EXIT_OK, {"landed": ["d1"], "commit_sha": "abcdef123456"}
        )

        captured = capsys.readouterr()
        assert "baton-assemble apply: ok — landed d1; committed abcdef123456" in captured.err
        assert captured.out == ""


class TestVerdictLineContent:
    def test_a_landed_run_names_its_directives_and_short_sha(self):
        line = ba_apply._verdict_line("ok", {"landed": ["d1", "d2"], "commit_sha": "0123456789abcdef"})

        assert line == "ok — landed d1, d2; committed 0123456789ab"

    def test_a_run_that_landed_nothing_says_so(self):
        line = ba_apply._verdict_line("transport_fail", {"landed": [], "error": "no repo_root"})

        assert line == "transport_fail — nothing landed; no repo_root"

    def test_a_multi_line_error_is_flattened_and_truncated(self):
        line = ba_apply._verdict_line(
            "partial", {"landed": ["d1"], "error": "first line\n" + "x" * 400}
        )

        assert "\n" not in line
        assert line.endswith("...")
        assert len(line) < 220

    def test_budget_breach_keeps_its_own_label(self):
        _, report = ba_apply._finalize_report(
            ba_apply.apply_base.APPLY_EXIT_CLAIM_DENIED, {"budget_breach": {"cap": 30}}
        )

        assert report["status"] == ba_apply._BUDGET_BREACH_STATUS
        assert report["verdict"].startswith(f"{ba_apply._BUDGET_BREACH_STATUS} — ")
