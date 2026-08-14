"""
coordinator_core.reconcile — auto-reconcile decision engines for open handoffs.

Purpose: home for the COMPUTE_ONLY compute engines that back the
`handoff.reconcile_open` op (DR-208 classification: pure-compute, no writes / no
git mutation). commit_reality.py is the DEC-1 three-signal shipped-ness matcher;
gate_eval.py (C3) is the unified structured/prose gate evaluator. Both take a
loaded policy dict (C9's policy_loader) as input rather than encoding thresholds
themselves — a policy-YAML data edit, not a code change, is how DoE amends the
bar.

Spec backlink: pln-claude-klabauter-auto-reconcile-pass-off-425848 § C2/C3
"""

from __future__ import annotations
