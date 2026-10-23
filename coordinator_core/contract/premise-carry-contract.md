# plan.premise_scope / plan.premise_record — carry contract

> **What this is.** The statement of the carry that lets plan-blitz's premise check, on a refire,
> re-resolve only the citations that owe a fresh check and carry every other verdict forward.
> Consumers: DoE `plan-blitz.mjs :: premiseChecker`. Producers: the engine ops `plan.premise_scope`
> and `plan.premise_record` (`coordinator_core/ops/plan_premise_carry.py`; pure core
> `coordinator_core/ops/_premise_carry.py`). The row shape is DoE's `PREMISE_SCHEMA`, extended by
> one optional integer `line`. The `declared_absent:` exemption lives in
> `readiness-verdict-producer-contract.md`.

## 1. Prior art and why the diff is set-based

The shape is the reground carry: `_research_grounding.line_hashes` records per-line hashes as a
basis, an unchanged unit carries its verdict, a changed unit is re-graded, and
`_research_grounding.carry_forward` keys a carried verdict to the grader version so a brief change
re-grades it.

Two differences:

- **Set-based, not positional.** The unit is a plan line matched by content hash as a set.
  `changed_digests` diffs by position, so one inserted line would mark every later line changed
  and a one-line edit would re-check the whole tail. It is never used here.
- **Opaque brief version.** DoE computes `brief_version` from the static premise-check brief text.
  The engine cannot see the brief, so it stores the value as an opaque string and compares it for
  equality. It is never hand-bumped, and a carried verdict is never keyed to HEAD or to the cited
  file's content.

## 2. Carry file

- One per plan, at
  `Path(session.machinery_paths.plan_sidecars_dir(str(main_worktree_root(repo_root)))) / f"{plan_stem}.premise-carry.json"`
  (`plan_sidecars_dir` takes and returns `str`). That is `.coordinator-local/plan-sidecars/`, the
  existing plan-derivable, gitignored sidecar home.
- Written atomically (temp file plus replace), built with pathlib; no absolute path is stored.
- JSON:
  `{"schema": "premise-carry/1", "plan": <repo-relative posix path>, "brief_version": <str>,
  "basis": [<sorted unique line hashes>], "rows": [<row> + {"line_hash": <str|null>}]}`.
- A file that is unreadable, has another `schema`, or has a different `plan` reads as **no
  record**.

## 3. Line hash

`_research_grounding.line_hashes`, reused and never redefined: sha256 of each `splitlines()` line
after `rstrip`, so CRLF equals LF.

## 4. Row keying (on record)

A row's `line_hash` is:

1. the hash of plan line `line` when the row carries an integer `line` in range (1-based);
2. otherwise the hash of the first plan line that contains the row's whitespace-normalised
   `citation`;
3. otherwise `null`. A `null` key never carries.

**Optional row key `anchor`.** A string naming a plan `anchor_premises[].id`. It passes through
merge and carry verbatim and is read by `plan.readiness_verdict`
(`readiness-verdict-producer-contract.md` § Anchor premises). An anchor row sets `line` to the
1-based plan line of that anchor's `command:`, so a folded multi-line `claim` never keys to `null`;
editing the check changes the hash and re-opens the anchor. A non-string `anchor` is refused by the
readiness op, not by the carry ops. The readiness op matches `anchor` stripped. A carried row whose
`verdict` is outside `RESOLVES | UNRESOLVED | CONTRADICTED | UNCHECKABLE` is read as no verdict.

## 5. Flagged

A row with verdict `UNRESOLVED` or `CONTRADICTED`. `UNCHECKABLE` and `RESOLVES` rows carry.

## 6. `plan.premise_scope` (COMPUTE_ONLY)

Params: `{plan | plan_path, brief_version}`. Any other key is refused.

Reply: `{plan, first_pass, reason, changed_lines: [{line, text}], recheck: [row], carried: [row]}`.

- **First pass.** With no record, or a record whose `brief_version` differs, the reply has
  `first_pass: true` and empty lists. **A `brief_version` mismatch is a first pass.** The checker
  then runs the full pass exactly as today.
- **Otherwise** `first_pass: false`, and:
  - `changed_lines` lists every current non-blank line whose hash is not in `basis` (1-based, with
    `text` capped at 300 characters).
  - `recheck` lists every prior flagged row, plus every prior row whose `line_hash` is `null` or
    absent from the current plan's hashes (a stale key).
  - `carried` lists every other prior row, verbatim (the `citation` is byte-for-byte unchanged, so
    the `blocking` keys a repeat-pull halt compares stay stable).
- **A refire owes zero fresh checks exactly when `changed_lines` and `recheck` are both empty.**

## 7. `plan.premise_record` (MUTATING)

Params: `{plan | plan_path, brief_version, rows, retired?, citations_skipped?}`.

- It computes the scope itself and does not trust a caller-supplied one.
- Merged rows are the scope's `carried`, minus any citation that appears in `rows` or in
  `retired`, followed by `rows` in the order given.
- It **refuses** (ValueError, nothing written) when a `recheck` row's citation appears in neither
  `rows` nor `retired`. A flag can be re-judged or retired, never dropped silently.
- The new basis is the hashes of every current line. When `citations_skipped > 0`, it is only the
  lines up to the largest `line` among `rows`, plus the prior basis hashes still present. Lines
  past the cap stay "changed", so the next refire checks them.
- Reply: `{plan, path, rows: <merged>, carried: <int>, fresh: <int>}`. On a first pass `carried`
  is 0 and the merged rows are `rows`.

## 8. Not in this contract

- `plan.readiness_verdict` gains no skip, force or override param and no write; the carry write
  lives only in `plan.premise_record`.
- The within-run `premiseRecheck` leg, `mergePremiseRows` and `premiseKey` are unchanged.
- First-pass behaviour is unchanged: the five classes, the cap in plan order and the contract text.
