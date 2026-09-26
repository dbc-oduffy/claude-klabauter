
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
