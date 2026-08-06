"""coordinator_core.probes — read-only diagnostic probes over fleet-wide transcript
and telemetry corpora.

Purpose: home for probes that answer "what is the fleet actually doing" questions
by scanning Claude Code session transcripts (``~/.claude/projects/*/*.jsonl``) or
similar external evidence — distinct from ``coordinator_core.ops``, whose modules
mutate or emit work-state artifacts. A probe here is read-only by construction: it
never writes to the corpus it scans, and where it registers a JSON-RPC op (see
``fork_census.py``) that op is COMPUTE_ONLY.

Spec backlink: docs/plans/2026-07-29-fleet-wide-bash-spawn-fan-out.md § C1.
"""

from __future__ import annotations
