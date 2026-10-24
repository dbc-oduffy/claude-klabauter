# coordinator → touch-record.jsonl v1 producer contract (STABLE CONSUMER SURFACE)

> **What this is.** The producer-side contract of the per-claimant touched-files record
> (`touch-record.jsonl`) that claude-klabauter's `coordinator_core.session.touch_record` writes. External
> consumers (example-cockpit-repo's per-EM activity view, DoE tooling) may read these files directly
> from disk; this file declares which parts of the line shape they may rely on.
>
> **Writer of record.** `touch_record.encode_line` (key set), `touch_record.sink_path` (filename),
> `touch_record.SCHEMA_VERSION` (`v`). A key not listed below is not part of the contract.
>
> **Pinned by.** `coordinator_core/session/tests/test_touch_record_v1_keys_pinned.py` — it fails
> naming this file when the writer's emitted key set drifts.

## 1. Line shape

One JSON object per line, UTF-8, `\n`-terminated, compact separators (`,` `:`). Lines are
append-only; a trailing newline-less fragment is a writer caught mid-append and is ignored by
readers.

| Key | Type | Presence | Meaning |
|---|---|---|---|
| `v` | int | always | Schema version. `1`. |
| `verb` | string | always | `"T"` touch (claim) or `"R"` release. |
| `ts` | float | always | Writer wall-clock epoch seconds. Orders events only across sinks; within one file, line order is append order. |
| `sid` | string | always | Session id of the claim's owner. |
| `agent` | string or null | always | Agent id, or explicit `null` when the writer is the session itself. |
| `path` | string | always | Repo-relative, POSIX-separated, normalized at write time. Absolute paths are rejected, never stored. |
| `hash` | string | only when the writer supplied one | Whole-file CRLF-folded `sha256` hex of the file at touch time. Omitted, never `null`. |
| `name` | string | only when the writer's registry name resolved for this `sid` | Human-facing session name. Omitted, never `null`. |
| `kind` | string | only when the channel knows | `"w"` write or `"r"` read. Absent means unknown, not write. Omitted, never `null`. |

No PID or other liveness token is carried. Liveness of `sid` is a separate question answered by
the engine, not by this record.

## 2. Layout

- Filename: `touch-record.jsonl` (`touch_record.RECORD_FILENAME`).
- Session-keyed sink: `<git-dir>/coordinator-sessions/<sid>/touch-record.jsonl`.
- Agent-keyed sink: `<git-dir>/coordinator-sessions/.agents/<agentId>/touch-record.jsonl`. Its
  lines carry `sid` (the owning session) and `agent` (this `<agentId>`), so a line is attributable
  without consulting its directory.
- Rotation: past a size bound the live file is renamed to
  `touch-record.jsonl.rotated-<ts>-<pid>[-<seq>].jsonl` beside it; a fresh live file follows. A
  claimant's record is the live file plus zero or more rotated siblings, oldest generation first,
  live file newest. Readers must read the family.

## 3. Folding rule

Last verb wins per `(claimant, path)`: a later `T` after an `R` re-claims, a later `R` releases.

## 4. Version policy

- **Additive keys keep `v=1`.** A new optional key (omitted when absent, as `hash`, `name` and
  `kind` were added) does not bump the version. Consumers MUST ignore keys they do not know.
- **Rename, removal, retyping, or a change to a listed key's meaning bumps `v`** and is announced
  to consumers by cross-repo memo before the writer flips. Consumers MUST treat an unknown `v` as
  unreadable, not as v1.
- The pin test is updated in the same change as any key-set change, so drift is never silent.
