"""Plain (no cadence/pending_fix marker) gate: the committed door.exe must
never sit behind the door sources this tree ships. Runs on EVERY platform --
POSIX carries no prebuilt to compile at all, but the SOURCE fingerprint
`committed_prebuilt_source_drift` reads is plain text and readable anywhere,
so this is answerable without a compiler on any box.

Incident this pins: the committed Windows door.exe sat 11 days behind
door.c/door_core.c/door_core.h/door_env_set.h, changed by POSIX/cloud commits
that cannot compile the binary that would have caught it. A POSIX author
touching a door source must see this test go red the same day.
"""

from __future__ import annotations

from coordinator_core.install import door_install


def test_committed_door_exe_is_not_behind_its_sources():
    drifted = door_install.committed_prebuilt_source_drift()
    assert drifted == [], (
        f"coordinator_core/warm/door/door.exe is behind its sources: {', '.join(drifted)}. "
        "Rebuild it: `python -m coordinator_core.warm.door.build <engine_root> "
        "--output coordinator_core/warm/door/door.exe` on a Windows box with clang or "
        "MSVC, then commit the rebuilt door.exe together with its "
        "door.exe.provenance.json sidecar."
    )
