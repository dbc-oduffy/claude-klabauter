"""coordinator/bin/tests/test_git_hook_rung_order.py — pins the shell
fallback-chain RUNG ORDER emitted by `_shim_body` / `_append_block` in
`coordinator/bin/lib/git_hook_install.py`.

Spec: C7 of docs/plans/2026-08-16-one-engine-for-the-whole-box.md. The
template used to try the baked absolute-path SCRIPT literal FIRST, with the
settings-home forwarder rung (`${COORDINATOR_SETTINGS_HOME:-...}/bin/<name>`)
only consulted several rungs later. On a box where the baked literal
(generated at a prior install time, on this machine's own checkout path)
still happens to exist, every rung after it is dead code — including the
settings-home rung, which is the only one that stays correct once a
checkout moves. The PM caught this asking why a baked absolute path is
machine-portable: it is not.

This test asserts the settings-home rung's SCRIPT/‌`_T` assignment appears
strictly BEFORE the baked coord_bin absolute-path assignment in the emitted
body text, for both the whole-file shim (`_shim_body`) and the append-form
block (`_append_block`) — so a future rung reshuffle that regresses the
order is caught here rather than rediscovered by a machine that moved.

Negative spec: this test does NOT assert anything about `_resolve_coord_bin`
(the Python-side rung order for BAKING the coord_bin candidate) — that is a
distinct, unrelated rung ladder untouched by this chunk.
"""
from __future__ import annotations

import sys
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from git_hook_install import _append_block, _shim_body  # noqa: E402

_COORD_BIN = "/fake/coord/bin"
_SCRIPT_NAME = "coordinator-auto-push"
_SETTINGS_HOME_NEEDLE = "${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/"
_BAKED_ABS_NEEDLE = f'"{_COORD_BIN}/{_SCRIPT_NAME}"'


def test_shim_body_settings_home_rung_precedes_baked_absolute_path():
    body = _shim_body(_COORD_BIN, _SCRIPT_NAME, 'exec "$_PY" "$SCRIPT" "$@"')

    settings_home_idx = body.find(_SETTINGS_HOME_NEEDLE)
    baked_idx = body.find(_BAKED_ABS_NEEDLE)

    assert settings_home_idx != -1, "settings-home rung missing from _shim_body output"
    assert baked_idx != -1, "baked absolute-path rung missing from _shim_body output"
    assert settings_home_idx < baked_idx, (
        "settings-home rung must resolve BEFORE the baked absolute path — "
        f"settings-home at {settings_home_idx}, baked absolute path at {baked_idx}"
    )


def test_append_block_settings_home_rung_precedes_baked_absolute_path():
    block = _append_block(
        _COORD_BIN,
        _SCRIPT_NAME,
        "coordinator auto-push (crash insurance)",
        '"$_PY" "$_T" "$@"',
    )

    settings_home_idx = block.find(_SETTINGS_HOME_NEEDLE)
    baked_idx = block.find(_BAKED_ABS_NEEDLE)

    assert settings_home_idx != -1, "settings-home rung missing from _append_block output"
    assert baked_idx != -1, "baked absolute-path rung missing from _append_block output"
    assert settings_home_idx < baked_idx, (
        "settings-home rung must resolve BEFORE the baked absolute path — "
        f"settings-home at {settings_home_idx}, baked absolute path at {baked_idx}"
    )
