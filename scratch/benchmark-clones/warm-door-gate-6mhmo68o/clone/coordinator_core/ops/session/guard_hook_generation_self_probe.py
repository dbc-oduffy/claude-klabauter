"""
coordinator_core.ops.session.guard_hook_generation_self_probe — SessionStart
self-probe for the settings.json hooks-generation kill switch.

Purpose: every OTHER guard in the 2026-07-28 incident cluster is reactive —
`gen_settings_hooks.py` checks its own markers only when it happens to run
(installer re-run, `/coordinator:setup`), and the post-merge/post-checkout
gates (see `coordinator/bin/coordinator-postsync-marker-resync-check`,
Claude-klabauter) only fire on a git sync event. Nothing previously ran on
EVERY session boot to ask "does this machine's own hook-delivery config
still resolve?" — a machine could sit for days with a broken/foreign
`COORDINATOR_CONTENT_ROOT` before some other trigger noticed. This probe
closes that gap: it fires on `SessionStart`, records what THIS session
resolved as its content root to a per-machine sentinel, and — if that
resolution comes back empty or points at a directory that doesn't exist on
this machine — RE-ARMS the negative kill switch
(`gen_settings_hooks._kill_switch_marker`) so the FIRST bad session disables
generation, instead of the fourth bricked one teaching us.

Cheap by construction (fires every session; Windows process-spawn tax is a
standing P0 — see `coordinator.local.md`'s bash-kill campaign ruling): this
probe does a single `os.environ.get` read, at most two `Path.is_file`/
`Path.is_dir` stats, and a best-effort small text write. No subprocess, no
registry read, no `resolve_coordinator_root()` call (that function's own
fallback chain can shell out to `machine-local get` — deliberately NOT used
here; this probe only asks "does the value THIS session's settings.json
`env` block already carried actually resolve," not "let's go re-derive it
from scratch").

Never blocks SessionStart: every code path is wrapped so an unexpected
failure degrades to silence (empty additionalContext, no sentinel write, no
kill-switch mutation) rather than raising — mirrors the fail-open posture of
the sibling `guard_settings_integrity`/`guard_foreign_platform_paths`
SessionStart guards. This hook must never itself exit non-zero; a
SessionStart hook that denies is a brick (see leg0's exit-code-hygiene
rationale in `coordinator_core.install._shared`, which this probe's own
try/except mirrors at the Python-function level rather than the shell-guard
level).

The two carve-outs below are one rule applied twice, not two independent
patches: a maintainer-only signal (`.doe-root`, the harness's own
installed-plugins registry) may be used to CLASSIFY which install shape a
machine has, but its ABSENCE must never be read as evidence the install is
UNHEALTHY — polarity, not provenance. This module is DR-117's worked
example: `d6fa361d` below is exactly the defect DR-117 names (a DoE-only
signal's absence treated as a diagnosis), and `d19dbe78` is the fix DR-117
generalizes from (a disjoint, harness-native classify-only branch added
alongside the first, not a replacement for it — see DR-117's anti-scope).
See `docs/decisions/DR-117-maintainer-signals-may-classify-never-diagnose.md`
(DoE-claude repo; tripwire `MAINTAINER-SIGNAL-DIAGNOSIS` in
`coordinator/docs/wiki/coordinator-tripwires.md`) for the rule in full — a
blunter "no DoE-specific signal in shipped code" framing was considered and
rejected there (it would misflag `resolve_coordinator_clone._resolve_source_mode`,
a legitimate classify-only use of `.coordinator-dev-repo`).

Inline-install carve-out (added 2026-07-31): an empty/unresolvable
`COORDINATOR_CONTENT_ROOT` is NOT itself proof of breakage — a machine whose
hook delivery is already live via the plugin path (e.g. a DoE inline
`--plugin-dir` dev install) legitimately never needs this env var at all
(`gen_settings_hooks.generate()` returns "skipped (plugin delivery already
live)" before ever writing it). `run_self_probe` now checks
`guard_settings_integrity.is_inline_install` — the SAME live-existence
carve-out that module's own clobber/reconciliation lenses already apply for
`--plugin-dir` machines — before re-arming, and only for that one narrow
shape; see the inline comment at the check site for why the two OTHER
in-tree candidates (`gen_settings_hooks.positive_marker_path()`,
`guard_settings_integrity._plugin_side_reachable()`/
`detect_hook_delivery_duplication()`) were rejected. This carve-out covers
ONLY the DoE-maintainer `--plugin-dir` dev-install shape (a `.doe-root`
pointer).

Marketplace/OSS-install carve-out (added 2026-07-31, closing the gap the
paragraph above left open): `.doe-root` is a DoE-maintainer-specific signal
— the coordinator plugin ships to many users through the marketplace/OSS
path who have no `.doe-root` at all, so for that MAJORITY install shape the
probe still false-positived and armed a kill switch that then required a
hand-delete (the arm was self-masking: `generate()` checks the marker AHEAD
of its plugin-delivery check, so the banner's own "re-run the installer"
remedy was a no-op). `run_self_probe` now ALSO checks
`_is_marketplace_install_live` — an additional OR-branch alongside
`is_inline_install`, not a replacement for it; the two cover DISJOINT
install shapes (`.doe-root` dev installs vs. marketplace/OSS installs) —
before re-arming. The signal: the harness's OWN installed-plugins registry
(`<claude_home>/plugins/installed_plugins.json`, read via
`guard_settings_integrity.read_installed_plugin_records` — reused, not
reopened a second way), matched on the `coordinator@` NAME PREFIX (not the
exact `coordinator@coordinator-claude` string, so a fork/renamed
marketplace still matches), cross-checked against `settings.json`'s
`enabledPlugins` (must be `True` for that key, so a deliberately-disabled
install does not read as live), and — this is the load-bearing negative
spec — the recorded `installPath` (and `installPath/hooks/hooks.json`) is
STATTED, never trusted bare: the JSON record persists on disk happily after
the coordinator tree it names has been destroyed, so the record ALONE is
the exact useless signal that cannot separate "healthy plugin install" from
"destroyed tree" (same failure mode `is_inline_install`'s own docstring
already rejects for a stale `.doe-root` pointer). A destroyed marketplace
cache dir still fails the stat and still falls through to the arm path, so
the true-positive detection this probe exists to preserve is unchanged. See
`_is_marketplace_install_live`'s own docstring for the full requirement
list and the three rejected alternatives (`CLAUDE_PLUGIN_ROOT` env read —
rests on an unconfirmed harness behavior; marketplace cache-dir stat —
hardcodes the marketplace name, fails closed on a fork; `enabledPlugins`
alone / `known_marketplaces.json` / `positive_marker_path()` /
`COORDINATOR_SOURCE_MODE` / `__file__` self-location — all stay TRUE (or
otherwise fail to discriminate) when the tree is destroyed, which is the
whole failure mode being guarded against).

Known non-obvious property (pre-existing, not introduced by this dispatch):
this probe hook is ITSELF registered plugin-side. On a machine whose ONLY
hook delivery is plugin-side and whose tree has been destroyed, the probe
never runs at all — the seam wrapper reports the script unreachable and
exits, so there is no SessionStart invocation to arm anything. The true
positive this probe can actually catch is therefore narrower than the
"detects a broken hook-delivery config" framing above implies: in practice
it is "settings.json-baked (or marketplace-registry) delivery is live
enough to invoke this script, but `COORDINATOR_CONTENT_ROOT` is empty" —
not "any broken hook delivery, however it broke."

Marker schema: the kill-switch marker this probe writes now carries real
`Since:`/`Expires:` lines in the exact format
`guard_settings_integrity._read_kill_switch_marker` requires — a marker this
probe writes must never land that sibling parser's MALFORMED branch (a
bare `#`-comment marker previously did, on every boot).

Spec backlink: coordinator_core.install.gen_settings_hooks (kill-switch /
    positive-marker polarity inversion, same 2026-07-28 dispatch).
Spec backlink: DR-117 (DoE-claude, maintainer signals may classify, never
    diagnose) — the general rule both carve-outs in this module instantiate;
    see the paragraph above `_is_marketplace_install_live`'s discussion for
    how this module is DR-117's worked example.
Sibling: coordinator/bin/coordinator-postsync-marker-resync-check
    (claude-klabauter) — the git-sync-triggered leg of the same defense; this
    module is the boot-triggered leg. Both re-arm the SAME negative marker
    via the SAME `gen_settings_hooks.kill_switch_marker_path` helper, so
    "kill switch armed" always means the same file regardless of which
    trigger armed it.
"""


from __future__ import annotations

GENERATES = []  # writes only a per-machine sentinel and kill-switch marker under settings-home, outside claude-klabauter's own tracked tree

import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from coordinator_core.install._shared import COORDINATOR_CONTENT_ROOT_ENV_KEY
from coordinator_core.install.gen_settings_hooks import (
    kill_switch_marker_path,
    resolve_settings_out_path,
)
from coordinator_core.ops.session.guard_settings_integrity import (
    is_inline_install,
    read_installed_plugin_records,
)

# Match on the `coordinator@` NAME PREFIX, not the full
# `coordinator@coordinator-claude` key -- a fork or renamed marketplace
# (e.g. `coordinator@my-fork`) must still be recognized as a live
# marketplace/OSS install of this same plugin.
_COORDINATOR_PLUGIN_PREFIX = "coordinator@"

# Marker re-arm window: the marker this probe writes (see `_render...` below,
# schema shared with `guard_settings_integrity._read_kill_switch_marker`)
# expires quickly on purpose -- this marker records a DETECTED BREAKAGE, not
# an operator's deliberate long-lived opt-out, so it should escalate back to
# the loud MALFORMED-adjacent "EXPIRED" banner soon if nobody has looked at
# it, rather than sitting silent behind the one-line not-expired router for
# months the way an operator-armed marker legitimately can.
_REARM_EXPIRY_DAYS = 7

_SENTINEL_NAME = ".coordinator-content-root-last-seen"


def _resolve_config_dir(config_dir: Optional[Path]) -> Path:
    if config_dir is not None:
        return config_dir
    raw = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return Path(raw)


def _atomic_write_text(path: Path, content: str) -> bool:
    """Best-effort atomic write; never raises. Returns True on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            os.replace(tmp_name, path)
        except OSError:
            try:
                os.remove(tmp_name)
            except OSError:
                pass
            return False
        return True
    except OSError:
        return False


def _is_marketplace_install_live(config_dir: Path) -> bool:
    """Marketplace/OSS-install carve-out (added 2026-07-31, closing the gap
    the inline-install carve-out above left open): is a `coordinator@`-
    prefixed plugin actually loading via the harness's own
    installed-plugins registry, independent of `.doe-root`?

    True iff ALL of:
      - `<config_dir>/plugins/installed_plugins.json` names a
        `coordinator@`-PREFIXED key (matches a fork/renamed marketplace
        too, e.g. `coordinator@my-fork` — never the exact
        `coordinator@coordinator-claude` string only) with a non-empty
        record list;
      - that same key is `True` in `settings.json`'s `enabledPlugins` (a
        deliberately-disabled install must NOT read as live even though
        its registry record still exists);
      - the record's `installPath` STATS as a real, existing directory on
        THIS machine RIGHT NOW, AND `installPath/hooks/hooks.json` also
        stats as a real file.

    Negative spec — the stat is load-bearing, never skipped: the registry
    JSON record persists on disk happily after the actual plugin tree
    (`installPath`) has been destroyed — a record's mere PRESENCE is
    exactly the same useless signal `is_inline_install`'s own docstring
    rejects for `.doe-root` (a pointer file surviving a destroyed clone).
    Statting `installPath` (and its `hooks/hooks.json`) is what makes this
    check discriminate "healthy marketplace install" from "destroyed
    tree" — a destroyed tree fails the stat and this function correctly
    returns False, so the true-positive re-arm path below still fires. Do
    NOT "simplify" this by trusting the record alone; that would silently
    reintroduce the false negative this carve-out exists to close.

    Fail-open at every uncertain step (missing/malformed registry,
    missing/malformed settings.json, no matching key): returns False,
    which falls through to the existing arm path — never a crash, never a
    false "safe to suppress".
    """
    plugins = read_installed_plugin_records(config_dir)
    if not plugins:
        return False

    matching_keys = sorted(
        key
        for key, records in plugins.items()
        if key.startswith(_COORDINATOR_PLUGIN_PREFIX)
        and isinstance(records, list)
        and len(records) > 0
    )
    if not matching_keys:
        return False

    settings_path = config_dir / "settings.json"
    try:
        with settings_path.open("r", encoding="utf-8") as fh:
            settings_data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(settings_data, dict):
        return False
    enabled = settings_data.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return False

    for key in matching_keys:
        if enabled.get(key) is not True:
            continue
        # Review: code-reviewer (Finding 1) -- installed_plugins.json stores
        # a LIST of records per key (user-scope + project-scope installs can
        # both exist under the same key), and a stale/destroyed first record
        # must not shadow a live, healthy later one. Iterate every record
        # under this key and return True on the first that passes the stat
        # check -- "ANY of" semantics, matching the non-empty-record-list
        # filter `matching_keys` already applies.
        for record in plugins[key]:
            if not isinstance(record, dict):
                continue
            install_path = record.get("installPath")
            if not isinstance(install_path, str) or not install_path:
                continue
            install_dir = Path(install_path)
            if not install_dir.is_dir():
                continue
            if (install_dir / "hooks" / "hooks.json").is_file():
                return True
    return False


_BANNER_REARMED = """
╔══════════════════════════════════════════════════════════════════╗
║  ⚠  coordinator hook generation SELF-PROBE found an empty/unresolvable
║     COORDINATOR_CONTENT_ROOT this boot — the kill switch has been
║     RE-ARMED ({marker}) so generation stays off
║     until this is fixed, rather than bricking hooks again next run.
║
║  Action: re-run the coordinator installer (or `/coordinator:setup`) once
║  this machine's DoE-claude clone location is confirmed, then delete
║  {marker} to re-enable.
╚══════════════════════════════════════════════════════════════════╝

"""


def run_self_probe(config_dir: Optional[Path] = None) -> str:
    """Run the self-probe against `config_dir` (defaults to
    `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`); return additionalContext text
    (empty string == silent, the healthy/no-op case).

    Never raises — see module docstring's fail-open posture.
    """
    try:
        resolved_config_dir = _resolve_config_dir(config_dir)
        # os.environ is sound here (not a stale daemon's boot-time env):
        # DR-215/C5 removed the UDS daemon transport, and coordinator_core.ipc's
        # own module docstring confirms the engine is now in-process/command-
        # type — `dispatch_message` is called directly by each caller, no
        # socket, no persistent service loop. So every hook/op invocation
        # (including this SessionStart probe) runs in a fresh process whose
        # os.environ the harness populates from THIS session's settings.json
        # `env` block, not a resurfaced value from an earlier boot.
        content_root = os.environ.get(COORDINATOR_CONTENT_ROOT_ENV_KEY, "")
        is_empty = not content_root or not os.path.isdir(content_root)

        # Classify BEFORE writing the sentinel, not after. `resolved_ok=false`
        # is the EXPECTED steady state on a `--plugin-dir` or marketplace-live
        # machine (see the two carve-outs below), so a sentinel carrying only
        # that line reads as a fault on a perfectly healthy box — it has
        # already cost one investigation that got as far as auditing a
        # stood-down kill-switch marker before the carve-outs explained it.
        # The sentinel is a human-facing breadcrumb; recording the RESOLUTION
        # without the CLASSIFICATION is what made it misleading.
        #
        # Both discriminators are the same cheap probes the carve-outs run
        # (`is_inline_install`: a `.doe-root` read plus one live `isdir`;
        # `_is_marketplace_install_live`: the harness's own installed-plugins
        # registry plus a stat) — computed once here and REUSED below, never
        # called twice, so this preserves the module's no-subprocess
        # "cheap by construction" contract. Neither runs at all when the
        # content root resolved, which is the majority path.
        inline_install = is_inline_install(resolved_config_dir) if is_empty else False
        marketplace_live = (
            _is_marketplace_install_live(resolved_config_dir)
            if is_empty and not inline_install
            else False
        )
        if not is_empty:
            verdict = "resolved"
        elif inline_install:
            verdict = "expected-inline-plugin-dir"
        elif marketplace_live:
            verdict = "expected-marketplace-live"
        else:
            verdict = "unresolved-and-unexplained"

        sentinel = resolved_config_dir / _SENTINEL_NAME
        sentinel_body = (
            f"{COORDINATOR_CONTENT_ROOT_ENV_KEY}={content_root}\n"
            f"resolved_ok={'false' if is_empty else 'true'}\n"
            f"verdict={verdict}\n"
        )
        _atomic_write_text(sentinel, sentinel_body)  # best-effort; failure is silent

        if not is_empty:
            return ""

        # Discriminator: an inline (`--plugin-dir`) dev install serves hook
        # delivery live from its clone (`${CLAUDE_PLUGIN_ROOT}`/CLAUDE_PLUGIN_ROOT
        # resolution) and never touches `gen_settings_hooks`/
        # COORDINATOR_CONTENT_ROOT at all — an empty/unresolvable content root
        # on such a machine is the EXPECTED healthy shape (see
        # `gen_settings_hooks.generate()`'s own "skipped (plugin delivery
        # already live)" early-return), not a broken one. Reuses
        # `guard_settings_integrity.is_inline_install` verbatim — the SAME
        # carve-out that module's own clobber lens and reconciliation lens
        # already apply twice for `--plugin-dir` machines, not a third
        # independently-derived check.
        #
        # NARROW carve-out, not a general disarm: `is_inline_install` is a
        # LIVE existence probe — it re-verifies `<doe-root>/coordinator`
        # exists on disk RIGHT NOW, not merely that a `.doe-root` file was
        # once written. A machine whose actual coordinator clone has since
        # been destroyed (the true-positive shape this probe exists to
        # catch — see this dispatch's report for the confirmed 2026-07-31
        # incident) still fails this check and falls through to the arm
        # path below; a stale `.doe-root` pointer to a now-missing directory
        # does NOT get read as "healthy".
        #
        # Deliberately NOT `gen_settings_hooks.positive_marker_path()`
        # (`.coordinator-hooks-enabled`): that marker's PRESENCE means the
        # OPPOSITE of what's needed here — it records that a machine has
        # opted INTO settings.json-baked hook generation, i.e. that it DOES
        # depend on COORDINATOR_CONTENT_ROOT resolving. Treating its
        # presence as "safe to suppress" would misclassify exactly the
        # true-positive shape (a previously-generating machine whose content
        # root just broke).
        #
        # Deliberately NOT `guard_settings_integrity._plugin_side_reachable`/
        # `detect_hook_delivery_duplication`: both call
        # `resolve_content_root()`, whose dev/passthrough registry rungs can
        # shell out to `machine-local get` on a cache miss — the exact
        # subprocess-per-SessionStart cost this module's own docstring rules
        # out (see "Cheap by construction" above).
        #
        # This check covers the inline `--plugin-dir` shape ONLY. A pure
        # OSS/marketplace install carries no `.doe-root` at all and is
        # covered by `_is_marketplace_install_live` immediately below —
        # the two branches are disjoint by construction, and the OSS one
        # is the majority shape, so neither may be dropped as redundant.
        if inline_install:  # computed once above; same probe, same semantics
            return ""

        # Marketplace/OSS carve-out (added 2026-07-31): the ONLY thing
        # `is_inline_install` above covers is a DoE `--plugin-dir` dev
        # install (`.doe-root` present) -- the majority of coordinator
        # users install via the marketplace/OSS path and have no
        # `.doe-root` at all, so without this second OR-branch this probe
        # still false-positives and arms the kill switch for them. See
        # `_is_marketplace_install_live`'s own docstring for the exact
        # discriminator (harness's own installed-plugins registry,
        # validated by a stat -- never trusted bare).
        if marketplace_live:  # computed once above; same probe, same semantics
            return ""

        settings_out = resolve_settings_out_path(str(resolved_config_dir / "settings.json"))
        marker = kill_switch_marker_path(settings_out)
        if marker.is_file():
            # Already armed (by this probe on a prior boot, an operator, or
            # the post-sync gate) — nothing new to report.
            return ""

        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            since = date.today()
            expires = since + timedelta(days=_REARM_EXPIRY_DAYS)
            marker.write_text(
                # Schema-valid for `guard_settings_integrity._read_kill_switch_marker`
                # (requires a parseable `Expires: YYYY-MM-DD` line) — a marker
                # this probe writes must never land that sibling parser's
                # MALFORMED branch (see this dispatch's report; a `#`-comment-
                # only marker previously did exactly that on every boot).
                f"Since: {since.isoformat()}\n"
                f"Expires: {expires.isoformat()}\n"
                "Reason: guard_hook_generation_self_probe.py detected an "
                f"empty/unresolvable {COORDINATOR_CONTENT_ROOT_ENV_KEY} at "
                "SessionStart.\n"
                "Disarm condition: confirm COORDINATOR_CONTENT_ROOT resolves "
                "to a real, existing coordinator content root on this "
                "machine (re-run the installer or /coordinator:setup), then "
                "delete this marker.\n",
                encoding="utf-8", newline="\n",
            )
        except OSError:
            print(
                f"guard_hook_generation_self_probe: could not write kill-switch marker {marker}",
                file=sys.stderr,
            )
            return ""

        return _BANNER_REARMED.format(marker=marker)
    except Exception as exc:  # noqa: BLE001 — SessionStart must never raise/block
        print(f"guard_hook_generation_self_probe: unexpected failure (ignored): {exc}", file=sys.stderr)
        return ""


async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Entry function for this probe, kept as the test seam
    `test_guard_hook_generation_self_probe.py` exercises directly.

    NOT a registered op. This carried `@register_op("session.self_probe_hook_generation")`
    from its original authoring until 2026-07-29, but the registration quad was never
    completed — no entry ever existed in `_registry_map.py`, `ops/__init__.py`, or
    `op_scopes.py` — so it was never actually dispatchable via JSON-RPC. The decorator
    was dropped as vestigial, which also cleared two fast-tier guards it was holding
    red (`test_unclassified_baseline_never_grows`,
    `test_op_key_scope_table_covers_all_registered_ops`): both keyed off the decorator
    alone. The probe still runs the way it always has — SessionStart calls
    `run_self_probe()` directly, not through op dispatch.

    Parameters (params dict):
        config_dir (str, optional) — overrides CLAUDE_CONFIG_DIR/$HOME/.claude
            resolution. Primarily for test-harness use.

    Returns:
        {"text": str}  — additionalContext text (empty string == silent).
    """
    config_dir_param = params.get("config_dir")
    config_dir = Path(config_dir_param) if config_dir_param else None
    text = run_self_probe(config_dir)
    return {"text": text}
