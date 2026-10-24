# Group EM records producer contract

> **What this is.** The on-disk and `--json` shapes a cockpit consumer (cockpit, DoE) may read
> about the Group EM: the nomination, the box holder, the box hold, the suite grants, the
> `who` / `standing` output, and where the watch heartbeat is pinned. Key sets below are pinned
> against each writer by `coordinator_core/group_em/tests/test_consumer_surface_keys_pinned.py`.
> Adding a key is compatible; removing or renaming one is a breaking change that must go through
> this doc and that test.
>
> **Writers.** `coordinator_core/group_em/nomination.py`, `box_hold.py`, `suite_grant.py`,
> `standing.py`. `<settings-home>` is `settings_home()`; every record is machine-global, in no
> repo's tree.

## 1. Nomination — `<settings-home>/state/group-em/<repo-key>.json`

`<repo-key>` is `nomination.repo_key(repo_root)`. Keys: `version`, `repo_root`, `session_id`,
`peer_name`, `nominated_at`, `nominated_by`, `note`, `entered_via`, `entry_evidence`.

- `entry_evidence` is `{status, prompt_id, claimed_at}` for a typed claim, `{kind, claimed_at}` for a
  home-start; verified at read time, never at write time.
- A claim that takes over from another session adds `displaced_holder`, `displaced_holder_live`;
  a positively dead incumbent also adds `replaced_holder_session_id`, `replaced_holder_name`,
  `replaced_nominated_at`, `replaced_live_reason`.
- The record never carries a `pid`; liveness is a registry join on `session_id`.

## 2. Box holder — `<settings-home>/state/group-em/box/holder.json`

The same record, same key set, as the nomination of the repo that last claimed. Standing is
box-wide; a per-repo record naming another session is stale.

## 3. `who --json` / `standing --json`

`who` is the nomination record plus `live`, `live_reason`, `live_state`, `entry_status`.
`standing` is `who` plus `standing` (`live` | `not_live` | `no_match`). Both return nothing when no
record is on file. Producer: `coordinator_core/group_em/standing.py`.

## 4. Box hold — `<settings-home>/state/group-em/box/hold.json`

Keys: `set_by_session`, `reason`, `set_at`, `expires_at` (epoch seconds). The reader treats
absent, expired or malformed as no hold; the effective deadline is
`min(expires_at, set_at + 1800)`.

## 5. Suite grants — `<settings-home>/state/group-em/suite-grants/<issuer>/<grantee>.json`

Keys: `schema_version`, `kind` (`group-em-suite-grant`), `issuer_session_id`, `issuer_name`,
`grantee_session_id`, `grantee_name`, `scope_root`, `issued_at`, `expires_at`, `note`.
The record authorizes the Tier-U/F suite gate only.

## 6. Watch heartbeat — `<repo>/state/group-em-watch.json`

Pinned against DoE's reader by `coordinator_core/group_em/tests/test_watch_heartbeat.py`
(`_READER_KEYS`). Cited, not restated here.

## 7. What does not exist

- **No GPU/CPU slot ledger.** Nothing on the box grants those slots, so there is nothing to record;
  the only box-wide resource records are the hold (section 4) and the suite grants (section 5).
- **No watch daemon.** `coordinator_core/group_em/watch.py` is a Monitor-armed persistent
  runnable, not an OS service; its liveness is the heartbeat's freshness (section 6).
