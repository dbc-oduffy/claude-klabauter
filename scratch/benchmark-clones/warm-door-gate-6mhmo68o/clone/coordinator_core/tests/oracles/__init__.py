"""Executable oracles for the amplification gate's exemption claims.

An exemption in `_EXEMPT_SITES` asserts a per-item spawn site is unbatchable by construction.
An adversarial re-verification on 2026-08-19 read all 75 then-entries at the call site and found
22 that did not survive -- the register had grown by category, and a claim written once was never
re-checked against the code beneath it. The PM's ruling was that the register should not exist.

Most of what survived is not a defect in the collector; it is a claim about a COMMAND-LINE
SURFACE -- "`git config --unset` accepts exactly one key", "this sibling CLI takes one record per
invocation". No AST pass can know that. But a TEST can, and that is the whole difference: a
sentence a human wrote rots silently, while an assertion fails loudly the moment it stops being
true.

So each module here measures one such claim, and `_ORACLE_CLAIMS` in
`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py` binds a site key to the oracle
that proves its exemption. A key cannot be claimed without a test that exists and passes:
`test_every_oracle_claim_names_a_real_oracle` enforces the binding, and the oracles run in the
same suite as the gate, so a claim that stops holding turns the gate red rather than quietly
continuing to suppress.

Negative-spec: NO STATUS CACHE. An earlier design routed suppression through a generated
`oracle_status.json` with a staleness window. That reintroduces exactly the failure being
removed -- a recorded verdict that can outlive the fact it recorded. The oracles are cheap
enough to run with the gate, so they do.
"""
