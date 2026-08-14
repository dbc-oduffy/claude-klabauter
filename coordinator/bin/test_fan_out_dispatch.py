"""test_fan_out_dispatch.py — pytest suite for fan-out-dispatch.py.

Converted from a hand-rolled `.test.py` runner (print-based PASS/FAIL, its own
main()/sys.exit) into collectable top-level test_* functions; assertion intent
preserved 1:1. Each original lettered Test block becomes its own test_* function
(all use independent fixture subdirectories off a shared module-scoped `root`
tempdir), except Test H's three sub-cases (threshold resolution order), which stay
together in one test function since they share a single repo_h and are meaningfully
sequential checks of the same resolution ladder.

Port of: fan-out-dispatch.test.sh (65e5d199, 2026-07-19) — de-bash-coordinator
campaign, Wave 3. Validates the fan-out helper's overlap detection, malformed-row
rejection, clean-spec emission, no-commit-verb guarantee, large-wave NOTE boundary,
threshold-resolution order, non-git-repo hard-error, @file brief form, and the
pinned-interface (4th-column) observer. All tests use temp directories for fixtures;
cleanup is guaranteed by pytest's tmp_path_factory.

Spec backlink: docs/plans/2026-05-27-fan-out-default-doctrine.md §Chunk 1
Spec backlink (organic-ramp): docs/plans/2026-05-30-organic-ramp-concurrency-doctrine.md §C4
Spec backlink (invariant observers): docs/plans/2026-06-22-invariant-verification-observers.md §C2
Spec backlink (plan-doc OOS): docs/plans/2026-06-15-execute-plan-plan-doc-oos-injection.md §C4

Note on parity with the retired bash harness: the bash test carried 3 latent failures that
were harness bugs, not behaviour bugs — two large-wave assertions died to SIGPIPE (`set -o
pipefail` + `grep -q` short-circuit on large output) and the C9 count asserted 3 where the
real per-block figure is 2 headers × N blocks. This port asserts the CORRECT figures and is
green end-to-end.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest
from coordinator_core.win_portability import no_console_creationflags

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(SCRIPT_DIR, "fan-out-dispatch.py")
PYTHON = sys.executable



def make_git_repo(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        r = subprocess.run(cmd, cwd=path, capture_output=True, text=True, **no_console_creationflags())
        if r.returncode != 0:
            raise RuntimeError(f"git setup failed in {path}: {r.stderr}")


def run_helper(cwd, spec_file=None, stdin_text=None, extra_env=None, args=None):
    argv = [PYTHON, HELPER]
    if spec_file is not None:
        argv += ["--spec", spec_file]
    if args:
        argv += args
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(
        argv,
        cwd=cwd,
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        **no_console_creationflags(),
    )
    return r.returncode, r.stdout, r.stderr


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    return str(tmp_path_factory.mktemp("fan-out-dispatch-test"))


def test_file_overlap_detection(root):
    repo_a = os.path.join(root, "repo_a")
    make_git_repo(repo_a)
    spec_a = os.path.join(root, "spec_a.tsv")
    write(spec_a, "chunk-1\tFix auth module\tauth.py,shared.py\nchunk-2\tFix billing module\tbilling.py,shared.py\n")
    code, so, se = run_helper(repo_a, spec_file=spec_a)
    assert code != 0, "overlap must exit non-zero"
    assert "shared.py" in se
    assert "chunk-1" in se
    assert "chunk-2" in se
    assert "EXECUTOR DISPATCH BLOCK" not in so, "overlap must produce no partial output"


def test_malformed_row_detection(root):
    repo_b = os.path.join(root, "repo_b")
    make_git_repo(repo_b)

    spec_b1 = os.path.join(root, "spec_b1.tsv")
    write(spec_b1, "chunk-1\tFix something\n")  # only 2 fields
    code, so, se = run_helper(repo_b, spec_file=spec_b1)
    assert code != 0, "wrong-field-count must exit non-zero"
    assert "row 1" in se
    assert "EXECUTOR DISPATCH BLOCK" not in so

    spec_b2 = os.path.join(root, "spec_b2.tsv")
    write(spec_b2, "\tFix something\tfile.py\n")  # empty chunk-id (leading tab collapses -> 2 fields)
    code, so, se = run_helper(repo_b, spec_file=spec_b2)
    assert code != 0, "empty-chunk-id must exit non-zero"
    assert "row 1" in se
    assert "EXECUTOR DISPATCH BLOCK" not in so

    spec_b3 = os.path.join(root, "spec_b3.tsv")
    write(spec_b3, "chunk-1\tFix something\t\n")  # empty files
    code, so, se = run_helper(repo_b, spec_file=spec_b3)
    assert code != 0, "empty-files-field must exit non-zero"
    assert "chunk-1" in se
    assert "EXECUTOR DISPATCH BLOCK" not in so

    spec_b4 = os.path.join(root, "spec_b4.tsv")
    write(spec_b4, "chunk-1\tBrief\tfile.py\textra-field\n")  # 4th column: malformed pin, still valid
    code, so, se = run_helper(repo_b, spec_file=spec_b4)
    assert code == 0, "4-field row (optional pin column) must exit zero"
    assert "EXECUTOR DISPATCH BLOCK" in so

    spec_b5 = os.path.join(root, "spec_b5.tsv")
    write(spec_b5, "chunk\nid\tBrief here\tfile.py\n")  # embedded newline splits into non-3-field lines
    code, so, se = run_helper(repo_b, spec_file=spec_b5)
    assert code != 0, "embedded-newline must exit non-zero"
    assert "EXECUTOR DISPATCH BLOCK" not in so

    spec_b6 = os.path.join(root, "spec_b6.tsv")
    write(spec_b6, "chunk-x\tBrief\ta,b\n")  # comma-in-path documented limitation
    code, so, se = run_helper(repo_b, spec_file=spec_b6)
    assert code == 0, "comma-in-path is a known limitation, must exit zero"
    assert "- a" in so
    assert "- b" in so


@pytest.fixture(scope="module")
def clean_spec_output(root):
    """Test C's clean 3-chunk spec run, shared by test_clean_spec and test_no_commit_verb
    (originally Test D asserted against Test C's captured stdout/stderr in the same run)."""
    repo_c = os.path.join(root, "repo_c")
    make_git_repo(repo_c)
    spec_c = os.path.join(root, "spec_c.tsv")
    write(
        spec_c,
        "alpha\tImplement the alpha feature\talpha.py,alpha_test.py\n"
        "beta\tImplement the beta feature\tbeta.py,beta_test.py\n"
        "gamma\tImplement the gamma feature\tgamma.py,gamma_test.py\n",
    )
    code, so_c, se_c = run_helper(repo_c, spec_file=spec_c)
    return code, so_c, se_c


def test_clean_three_chunk_spec(clean_spec_output):
    code, so_c, se_c = clean_spec_output
    assert code == 0, "clean spec must exit zero"
    assert so_c.count("EXECUTOR DISPATCH BLOCK") == 3, "exactly 3 dispatch blocks"
    assert so_c.count("END BLOCK") == 3, "exactly 3 end-block markers"
    assert "EXECUTOR DISPATCH BLOCK: alpha" in so_c
    assert "EXECUTOR DISPATCH BLOCK: beta" in so_c
    assert "EXECUTOR DISPATCH BLOCK: gamma" in so_c
    assert "Implement the alpha feature" in so_c
    assert "Implement the beta feature" in so_c
    assert "Implement the gamma feature" in so_c
    assert "alpha.py" in so_c
    assert "beta.py" in so_c
    assert "gamma.py" in so_c
    assert so_c.count("beta (files:") == 2, "beta named as peer 2 times"
    assert so_c.count("gamma (files:") == 2, "gamma named as peer 2 times"
    assert so_c.count("alpha (files:") == 2, "alpha named as peer 2 times"
    assert so_c.count("Destructive-action prohibition") == 3, "destructive-action prohibition in all blocks"
    assert "Disk-first verification preamble" in so_c
    assert "TEXT ONLY" in so_c
    # C9 (corrected from the retired bash harness's stale count of 3): each block carries TWO
    # "Out-of-scope" headers (peer-work + plan-document), so 3 blocks -> 3 of each.
    assert so_c.count("Out-of-scope — peer work") == 3, "peer-work OOS header in all blocks"
    assert so_c.count("Out-of-scope — plan document") == 3, "plan-doc OOS header in all blocks"
    # C10 (M4b): expected_branch: is the SC-DR-008 commit-authorization token — the fan-out
    # contract is now brief -> executor edits -> EM-serial-commit, so it must NOT appear.
    assert "expected_branch:" not in so_c, "expected_branch absent from all blocks (M4b de-branch)"
    assert so_c.count("### In-scope") == 3, "in-scope section header in all blocks"


def test_no_commit_verb_in_blocks_reminders_on_stderr(clean_spec_output):
    code, so_c, se_c = clean_spec_output
    assert "git commit" not in so_c, "no 'git commit' instruction in blocks"
    assert "git add --" not in so_c, "no 'git add --' instruction in stdout"
    assert "pilot" in se_c
    assert "ramp" in se_c
    assert "6-8" not in se_c
    assert "large wave" not in so_c, "small wave -> no large-wave NOTE on stdout"
    assert "Executors do NOT commit" in se_c
    assert "git add --" in se_c, "EM commit instruction on stderr"


def test_large_wave_note_boundary(root):
    repo_e = os.path.join(root, "repo_e")
    make_git_repo(repo_e)
    spec_e_at = os.path.join(root, "spec_e_at.tsv")
    write(spec_e_at, "".join(f"chunk-{n}\tBrief for chunk {n}\tfile{n}.py\n" for n in range(1, 13)))
    code, so, se = run_helper(repo_e, spec_file=spec_e_at, extra_env={"LARGE_WAVE_THRESHOLD": "12"})
    assert code == 0, "12-chunk spec must exit zero"
    assert "large wave" in so, "12 chunks -> NOTE contains 'large wave'"
    assert "WARNING" not in so
    assert "PM call" not in so

    spec_e_below = os.path.join(root, "spec_e_below.tsv")
    write(spec_e_below, "".join(f"chunk-{n}\tBrief for chunk {n}\tfile{n}.py\n" for n in range(1, 12)))
    code, so, se = run_helper(repo_e, spec_file=spec_e_below, extra_env={"LARGE_WAVE_THRESHOLD": "12"})
    assert code == 0, "11-chunk spec must exit zero"
    assert "large wave" not in so, "11 chunks (N-1) -> no large-wave NOTE"
    assert "WARNING" not in so
    assert "PM call" not in so


def test_non_git_repo_clear_error(root):
    nonrepo = os.path.join(root, "not_a_repo")
    os.makedirs(nonrepo, exist_ok=True)
    spec_f = os.path.join(root, "spec_f.tsv")
    write(spec_f, "chunk-1\tSome brief\tsome_file.py\n")
    code, so, se = run_helper(nonrepo, spec_file=spec_f)
    assert code != 0, "non-git-repo must exit non-zero"
    assert "git repository" in se
    assert "Remediation" in se
    assert "EXECUTOR DISPATCH BLOCK" not in so


def test_at_file_brief_form(root):
    repo_g = os.path.join(root, "repo_g")
    make_git_repo(repo_g)
    brief_file = os.path.join(root, "brief_g.txt")
    write(brief_file, "This is a longer brief read from a file\n")
    spec_g = os.path.join(root, "spec_g.tsv")
    write(spec_g, f"chunk-g\t@{brief_file}\tsome_file.py\n")
    code, so, se = run_helper(repo_g, spec_file=spec_g)
    assert code == 0, "@file brief must exit zero"
    assert "longer brief read from a file" in so


def test_threshold_resolution_order(root):
    repo_h = os.path.join(root, "repo_h")
    make_git_repo(repo_h)

    def n_chunk_spec(n, out):
        write(out, "".join(f"chunk-{i}\tbrief {i}\tfile_{i}.py\n" for i in range(1, n + 1)))

    specs = {}
    for n in (16, 15, 8, 5, 4, 3, 2):
        specs[n] = os.path.join(root, f"spec_h{n}.tsv")
        n_chunk_spec(n, specs[n])
    NOTE = "large wave"

    def clean_env(overrides):
        env = {"LARGE_WAVE_THRESHOLD": "", "MACHINE_LOCAL_FAN_OUT_LARGE_WAVE_THRESHOLD": ""}
        env.update(overrides)
        return env

    # H1: no env, empty registry -> fallback 16 (floor is 16, not 8)
    reg_h1 = os.path.join(root, "reg_h1")
    os.makedirs(reg_h1, exist_ok=True)
    _, out16, _ = run_helper(repo_h, spec_file=specs[16], extra_env=clean_env({"MACHINE_LOCAL_REGISTRY_DIR": reg_h1}))
    _, out15, _ = run_helper(repo_h, spec_file=specs[15], extra_env=clean_env({"MACHINE_LOCAL_REGISTRY_DIR": reg_h1}))
    _, out8, _ = run_helper(repo_h, spec_file=specs[8], extra_env=clean_env({"MACHINE_LOCAL_REGISTRY_DIR": reg_h1}))
    assert NOTE in out16, "fallback 16 -> NOTE at 16 chunks"
    assert NOTE not in out15, "fallback 16 -> no NOTE at 15 chunks"
    assert NOTE not in out8, "fallback floor is 16 NOT 8 -> no NOTE at 8 chunks"

    # H2: machine-local registry value (5) wins over fallback
    reg_h2 = os.path.join(root, "reg_h2")
    os.makedirs(reg_h2, exist_ok=True)
    ml_exe = shutil.which("machine-local")
    assert ml_exe, "machine-local not on PATH"
    ml = subprocess.run(
        [ml_exe, "set", "fan_out.large_wave_threshold", "5"],
        env={**os.environ, "MACHINE_LOCAL_REGISTRY_DIR": reg_h2},
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert ml.returncode == 0, ml.stderr
    _, out5, _ = run_helper(repo_h, spec_file=specs[5], extra_env=clean_env({"MACHINE_LOCAL_REGISTRY_DIR": reg_h2}))
    _, out4, _ = run_helper(repo_h, spec_file=specs[4], extra_env=clean_env({"MACHINE_LOCAL_REGISTRY_DIR": reg_h2}))
    assert NOTE in out5, "machine-local=5 -> NOTE at 5 chunks"
    assert NOTE not in out4, "machine-local=5 -> no NOTE at 4 chunks"

    # H3: short env var wins over registry value
    _, out3, _ = run_helper(repo_h, spec_file=specs[3], extra_env={
        "MACHINE_LOCAL_FAN_OUT_LARGE_WAVE_THRESHOLD": "",
        "LARGE_WAVE_THRESHOLD": "3",
        "MACHINE_LOCAL_REGISTRY_DIR": reg_h2,
    })
    _, out2, _ = run_helper(repo_h, spec_file=specs[2], extra_env={
        "MACHINE_LOCAL_FAN_OUT_LARGE_WAVE_THRESHOLD": "",
        "LARGE_WAVE_THRESHOLD": "3",
        "MACHINE_LOCAL_REGISTRY_DIR": reg_h2,
    })
    assert NOTE in out3, "env=3 beats registry=5 -> NOTE at 3 chunks"
    assert NOTE not in out2, "env=3 -> no NOTE at 2 chunks"


def test_pinned_interface_symbol_present_silent(root):
    repo_i = os.path.join(root, "repo_i")
    make_git_repo(repo_i)
    producer_i = os.path.join(root, "producer_i.py")
    write(producer_i, "def my_pinned_function(x):\n    return x\n")
    spec_i = os.path.join(root, "spec_i.tsv")
    write(spec_i, f"chunk-producer\tAuthor the producer\t{producer_i}\nchunk-consumer\tAuthor the consumer\tconsumer.py\tmy_pinned_function@{producer_i}\n")
    code, so, se = run_helper(repo_i, spec_file=spec_i)
    assert code == 0, "symbol present -> exit 0"
    assert "EXECUTOR DISPATCH BLOCK: chunk-producer" in so
    assert "EXECUTOR DISPATCH BLOCK: chunk-consumer" in so
    assert "NOT found" not in se
    assert "does not exist" not in se


def test_pinned_interface_symbol_absent_offers_note(root):
    repo_j = os.path.join(root, "repo_j")
    make_git_repo(repo_j)
    producer_j = os.path.join(root, "producer_j.py")
    write(producer_j, "def some_other_function():\n    pass\n")
    spec_j = os.path.join(root, "spec_j.tsv")
    write(spec_j, f"chunk-producer\tAuthor the producer\t{producer_j}\nchunk-consumer\tAuthor the consumer\tconsumer.py\tmissing_symbol@{producer_j}\n")
    code, so, se = run_helper(repo_j, spec_file=spec_j)
    assert code == 0, "symbol absent -> exit 0 (offer, not gate)"
    assert "NOTE:" in se
    assert "missing_symbol" in se
    assert "serial" in se
    assert "EXECUTOR DISPATCH BLOCK: chunk-producer" in so
    assert "EXECUTOR DISPATCH BLOCK: chunk-consumer" in so
    assert "serial predecessor" not in so, "NOTE goes to stderr, not stdout"


def test_pinned_interface_producer_file_absent_offers_note(root):
    repo_j2 = os.path.join(root, "repo_j2")
    make_git_repo(repo_j2)
    producer_j2 = os.path.join(root, "nonexistent_producer.py")
    spec_j2 = os.path.join(root, "spec_j2.tsv")
    write(spec_j2, f"chunk-producer\tAuthor the producer\t{producer_j2}\nchunk-consumer\tAuthor the consumer\tconsumer.py\tsome_symbol@{producer_j2}\n")
    code, so, se = run_helper(repo_j2, spec_file=spec_j2)
    assert code == 0, "file absent -> exit 0 (offer, not gate)"
    assert "NOTE:" in se
    assert "does not exist" in se
    assert "serial" in se
    assert "EXECUTOR DISPATCH BLOCK: chunk-producer" in so
    assert "EXECUTOR DISPATCH BLOCK: chunk-consumer" in so


def test_legacy_three_field_row_backward_compat(root):
    repo_k = os.path.join(root, "repo_k")
    make_git_repo(repo_k)
    spec_k = os.path.join(root, "spec_k.tsv")
    write(spec_k, "legacy-chunk-1\tFix the legacy module\tlegacy.py,legacy_test.py\nlegacy-chunk-2\tFix another legacy module\tother.py\n")
    code, so, se = run_helper(repo_k, spec_file=spec_k)
    assert code == 0, "3-field rows must exit 0"
    assert "EXECUTOR DISPATCH BLOCK: legacy-chunk-1" in so
    assert "EXECUTOR DISPATCH BLOCK: legacy-chunk-2" in so
    assert "pinned interface" not in se


def test_plan_doc_oos_block_injection_ordering(root):
    repo_l = os.path.join(root, "repo_l")
    make_git_repo(repo_l)
    spec_l = os.path.join(root, "spec_l.tsv")
    write(spec_l, "C1\tedit foo\tfoo.py\nC2\tedit bar\tbar.py\n")
    code, so_l, se_l = run_helper(repo_l, spec_file=spec_l)
    assert code == 0, "2-chunk spec must exit zero"
    assert so_l.count("Out-of-scope — plan document, do NOT touch") == 2, "plan-doc OOS heading per block"
    assert so_l.count("block_subagent_plan_body_write") == 2, "enforcement hook named per block"
    plan_idx = so_l.find("Out-of-scope — plan document")
    disk_idx = so_l.find("Disk-first verification preamble")
    assert plan_idx != -1 and disk_idx != -1 and plan_idx < disk_idx, \
        f"plan-doc OOS must appear BEFORE Disk-first preamble: plan_idx={plan_idx} disk_idx={disk_idx}"


def test_stdin_spec_path(root):
    repo_m = os.path.join(root, "repo_m")
    make_git_repo(repo_m)
    code, so, se = run_helper(repo_m, stdin_text="solo\tDo the thing\tsolo.py\n")
    assert code == 0, "stdin spec must exit zero"
    assert "EXECUTOR DISPATCH BLOCK: solo" in so


# ---------------------------------------------------------------------------
# candidate_restatements (5th field: change_kind) — push-not-pull hook onto
# coordinator_core.learn_lessons_assemble.generate_candidates. Spec backlink:
# DoE-claude coordinator/agents/executor.md § Candidate-Restatement Disposition.
# ---------------------------------------------------------------------------

# CLAUDE_KLABAUTER_ROOT for these tests is this very checkout — coordinator_core lives
# at its top level, a sibling of coordinator/bin/ (where this test file and
# fan-out-dispatch.py both live).
_REAL_CLAUDE_KLABAUTER_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))


def test_wiki_append_row_gets_candidate_restatements_populated(root):
    repo_n = os.path.join(root, "repo_n")
    make_git_repo(repo_n)
    target_wiki = os.path.join(repo_n, "target-wiki.md")
    write(
        target_wiki,
        "# Some Doctrine\n"
        "The quick brown fox jumps over the lazy dog in the doctrine text.\n",
    )
    spec_n = os.path.join(root, "spec_n.tsv")
    write(
        spec_n,
        "wiki-chunk\tThe quick brown fox jumps over something else entirely\t"
        "target-wiki.md\t-\twiki-append\n",
    )
    code, so, se = run_helper(
        repo_n, spec_file=spec_n, extra_env={"CLAUDE_KLABAUTER_ROOT": _REAL_CLAUDE_KLABAUTER_ROOT}
    )
    assert code == 0, f"wiki-append row must exit zero: {se}"
    assert "candidate_restatements:" in so
    assert '"line"' in so and '"excerpt"' in so, "populated candidates carry line+excerpt"
    assert "quick brown fox" in so, "the overlapping target passage is surfaced as an excerpt"


def test_non_wiki_row_gets_no_candidate_restatements_field(root):
    repo_o = os.path.join(root, "repo_o")
    make_git_repo(repo_o)
    write(os.path.join(repo_o, "code.py"), "def f():\n    pass\n")
    spec_o = os.path.join(root, "spec_o.tsv")
    write(spec_o, "code-chunk\tRefactor the helper\tcode.py\t-\tcode-edit\n")
    code, so, se = run_helper(
        repo_o, spec_file=spec_o, extra_env={"CLAUDE_KLABAUTER_ROOT": _REAL_CLAUDE_KLABAUTER_ROOT}
    )
    assert code == 0
    assert "candidate_restatements" not in so, "non-wiki change_kind must not get the field"


def test_three_and_four_field_rows_get_no_candidate_restatements_field(root):
    repo_p = os.path.join(root, "repo_p")
    make_git_repo(repo_p)
    spec_p = os.path.join(root, "spec_p.tsv")
    write(spec_p, "legacy-chunk\tFix the thing\tsome_file.py\n")
    code, so, se = run_helper(
        repo_p, spec_file=spec_p, extra_env={"CLAUDE_KLABAUTER_ROOT": _REAL_CLAUDE_KLABAUTER_ROOT}
    )
    assert code == 0
    assert "candidate_restatements" not in so, "absent change_kind (3-field row) gets no field"


def test_empty_pin_field_before_change_kind_fails_loud(root):
    # Review: code-reviewer — Finding 1/2. An empty 4th field with a populated 5th field
    # must fail loud (exit 1, no output) rather than silently misparsing change_kind into
    # pin_raw and dropping it — the pre-fix behaviour emitted a spurious "malformed 4th-
    # column pin" NOTE instead of a clean, correctly-diagnosed error.
    repo_r = os.path.join(root, "repo_r")
    make_git_repo(repo_r)
    write(os.path.join(repo_r, "target-wiki.md"), "# Doctrine\nSome text.\n")
    spec_r = os.path.join(root, "spec_r.tsv")
    write(spec_r, "chunk-r\tSome brief\ttarget-wiki.md\t\twiki-append\n")
    code, so, se = run_helper(
        repo_r, spec_file=spec_r, extra_env={"CLAUDE_KLABAUTER_ROOT": _REAL_CLAUDE_KLABAUTER_ROOT}
    )
    assert code == 1, "empty 4th field + populated 5th field must be a hard parse error"
    assert so == "", "no output emitted on parse error"
    assert "malformed row" in se
    assert "4th (pin) field is empty" in se
    assert "malformed 4th-column pin" not in se, (
        "must not surface the old misleading pin-format diagnostic for this shape"
    )


def test_generator_failure_degrades_to_empty_list_not_a_raise(root):
    repo_q = os.path.join(root, "repo_q")
    make_git_repo(repo_q)
    write(os.path.join(repo_q, "target-wiki.md"), "# Doctrine\nSome text.\n")
    spec_q = os.path.join(root, "spec_q.tsv")
    write(spec_q, "wiki-chunk\tSome incoming prose\ttarget-wiki.md\t-\twiki-new\n")
    bogus_claude_klabauter_root = os.path.join(root, "does-not-exist-claude-klabauter-root")
    code, so, se = run_helper(
        repo_q, spec_file=spec_q, extra_env={"CLAUDE_KLABAUTER_ROOT": bogus_claude_klabauter_root}
    )
    assert code == 0, "an unresolvable claude-klabauter root must not block the dispatch"
    assert "EXECUTOR DISPATCH BLOCK: wiki-chunk" in so, "the block is still emitted"
    assert "candidate_restatements: []" in so, "degrades to an explicit empty list"
    assert "NOTE" in se, "the degrade is surfaced as a NOTE, not swallowed silently"
