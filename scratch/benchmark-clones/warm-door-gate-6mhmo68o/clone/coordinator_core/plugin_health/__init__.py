"""
coordinator_core.plugin_health — read-only health/currency probes for DoE-claude's
plugin ecosystem (live-install drift, doctor-sentinel probes, addon-health scan).

Sibling modules (drift.py, sentinel.py, scan.py, probe_select.py) each
self-register their own op(s) via a module-level register_op(...) call — this
__init__ stays minimal and carries no shared state sibling modules depend on.

Negative-spec: entirely unrelated to coordinator_core.doctor_envelope, which is
Claude-klabauter's OWN two-tier doctor for claude-klabauter's own health (see that module's negative-
spec for the disambiguation both directions).

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T3a-g2/T3b
"""
