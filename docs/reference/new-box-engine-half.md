# New box: the claude-klabauter engine half

What the DoE box-onboarding playbook can cite for a third box (Mac Mini, 64GB, example-retrieval-repo + ue-addon host, no direct ethernet yet). Shipped rows name command, op and file; GUARDED-STOP rows name the row they wait on. Functions are cited by name.

| # | Item | Status | Cite |
|---|------|--------|------|
| 1 | Installer on fresh macOS | Shipped | `python3 scripts/setup.py --i-am-agent`, one command, no PATH change. On a PEP-668 interpreter with no deps, `provision_deps` in `scripts/setup.py` calls `engine_interpreter.ensure` (`coordinator_core/install/engine_interpreter.py`): one `pip install --target <settings-home>/engine-site`, no venv, never `--break-system-packages` (DR-478, `docs/decisions/DR-engine-interpreter-on-a-guarded-base.md`; DR-411). |
| 2a | Capability probe | Shipped | `python -m coordinator_core.group_em.box_probe` writes `capability` into `<home>/state/group-em/box.json` (`write_capability`; fields `os`, `arch`, `cpu_count`, `ram_mb`, `python`, `gpu` (null), `has_p4`, `has_ue`; zero spawn, `shutil.which` plus `UE_ROOT`/`UNREAL_ENGINE_ROOT`). File: `coordinator_core/group_em/box_probe.py`. |
| 2b | Identity, registry row | Shipped | `box_registry.ensure_identity` mints `box.json`; op `groupem.box_register` (`coordinator_core/ops/group_em_box_federation.py`) writes the registry row via `box_registry.register`, including the `transport` record. |
| 2c | Key issuance | Shipped | `box_key.ensure_key` mints this box's key; op `groupem.box_key_pin` pins a peer's key (`box_key.pin_peer_key`). File: `coordinator_core/group_em/box_key.py`. |
| 2d | Machine-local `box.*` keys | GUARDED-STOP: no engine writer exists; machine-local is DoE's surface (plan 7cbfb1 "Out of scope") | Route to DoE. Engine reads `box.json` only. |
| 3 | No direct ethernet | Shipped, no code | `box_transport` ssh transport (`ssh_host`) takes any `host` in the box's `{kind: ssh, host, invoke}` transport: a LAN address or a tunnel endpoint. The relay falls back to the settings-home coordinator-invoke door when the engine is absent (`_receive_via_door` in `coordinator/bin/unimatrix-relay.py`, d890b6), covered by `coordinator/bin/tests/test_unimatrix_relay_grant_mode.py::test_relay_without_engine_replies_through_the_door`. Direct-cable ethernet transport: out of scope. |
| 4a | Roster and heartbeat | Shipped | op `groupem.box_roster`; `box_heartbeat.stamp` (heartbeat) and `remote_roster.refresh` / `publish_local` (snapshots). Files: `coordinator_core/group_em/box_heartbeat.py`, `remote_roster.py`. |
| 4b | Placement | Shipped | op `groupem.box_place` with a capability requirement, e.g. `{"run": "<label>", "requirement": {"has_ue": true, "min_ram_mb": 60000}}` selects a registered box whose probe record satisfies it (`placement.satisfies` / `place_run`; `min_<key>` is a floor, other keys exact). |
| 4c | Register the live Mac Mini | GUARDED-STOP: operator act, needs the box online and ssh-reachable | Run 2a-2c on the Mini, then `groupem.box_register` with its transport (and `key_b64` when the Mini's root is not locally readable); its `pins` reply names each pinned and unpinned peer id in both directions, and `groupem.box_key_pin` is the remedy for each id it names unpinned. |

## Order on the new box

1. Item 1: `python3 scripts/setup.py --i-am-agent`.
2. Item 2a-2c.
3. Item 4c registration once the box is reachable; verify with `groupem.box_roster` and a `groupem.box_place` with the requirement above.
