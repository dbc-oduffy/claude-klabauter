"""coordinator_core.contract.memo_conformance -- example-doctrine-repo-contract conformance gate for the
native cross-repo memo tool (memo.send/list/draft/compose).

Holds the round-trip conformance gate that runs the round-trip fixture -- CLAUDE-KLABAUTER-OWNED
in-tree at ``coordinator/bin/test_cross_repo_memo_roundtrip.py`` since the 2026-07-21
executable-surface migration, no longer example-doctrine-repo-hosted and never vendored -- plus
CONTRACT-VS-ERGONOMIC.md, the classification of which
of the example-doctrine-repo CLI's own tests are contract-invariant (must survive the cutover) versus
CLI-ergonomic (specific to the bash CLI's UX, not part of the wire contract claude-klabauter
must honor). See docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C8 (A7).
"""
