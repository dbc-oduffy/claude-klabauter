# coordinator-settings-home.ps1 — bare-name forwarder (PowerShell twin of the bash
# coordinator/bin/coordinator-settings-home forwarder). The real resolver installs to
# <CLAUDE_HOME>\.claude\bin (CLAUDE_HOME-aware, retained compat forwarder that itself
# resolves the settings home); this forwarder lives in the harness-injected plugin bin
# so bare `coordinator-settings-home.ps1` resolves on Windows/pwsh tool shells where
# <CLAUDE_HOME>\.claude\bin is NOT on PATH — parity with the machine-local / claude-home
# PowerShell forwarders (see coordinator/templates/bin/claude-machine-local.ps1).
#
# WHY THIS EXISTS: shape-(iv) install-chain consumers bind to
# $(coordinator-settings-home)\<repo-id>\ per the agent-install-contract. Without this
# forwarder the seam CLI resolves only by absolute path (<CLAUDE_HOME>\.claude\bin or
# the settings-home bin, neither reliably on PATH), so consumers cannot bind bare —
# the exact gap example-game-repo-em flagged on 2026-07-06 for the bash resolver, mirrored here
# for the PowerShell/.ps1-seeder consumers (example-retrieval-repo, cockpit, ue-addon, example-game-repo).
# Spec: docs/plans/2026-07-06-durable-substrate-to-settings-home.md § C1/C9
#       docs/plans/2026-07-08-install-baton-rendezvous-off-dotclaude.md § C4b

# CLAUDE_HOME is a $HOME-substitute per machine-local-registry.md §4a (CLAUDE_HOME=/x →
# install at /x/.claude/), so the .claude suffix is correct here — same as the bash
# forwarder and the machine-local forwarder. PowerShell-native fallback chain:
# $env:CLAUDE_HOME, else $env:HOME (explicit, not the automatic $HOME — on Windows pwsh's
# automatic $HOME mirrors $env:USERPROFILE and does NOT read $env:HOME, so reading it
# explicitly here is what catches an operator who deliberately sets HOME, e.g. git-bash),
# else $env:USERPROFILE (explicit, for the same reason the HOME rung is explicit: pwsh's
# automatic $HOME is derived ONCE at session start and never re-reads the environment, so
# it cannot serve as the USERPROFILE rung — a process that sets $env:USERPROFILE after
# the session began gets the stale machine home back, silently and with no error), else
# the automatic $HOME as terminal — no bash `${VAR:-default}` parameter expansion.
#
# Negative spec: do NOT collapse the USERPROFILE rung back into the automatic $HOME on
# the reasoning that "$HOME mirrors $env:USERPROFILE on Windows". It mirrors its value at
# session start, not the variable; that reasoning is what AC10b's permuted-env probe
# falsified. The peer template (coordinator-claude@coordinator/templates/bin/coordinator-settings-home.ps1)
# already carries the explicit USERPROFILE rung — this file had drifted BEHIND it while
# citing it as the peer shape, so there is nothing to relay upstream.
#
# Resolve-ClaudeHomeBase — named export mirroring this same ladder, additive alongside
# the inline resolution above (no restructuring). Peer:
# coordinator-claude@coordinator/templates/bin/coordinator-settings-home.ps1's
# Resolve-ClaudeHomeBase (line 26, read at coordinator-claude@9e0fb5c44).
# Spec: docs/plans/2026-08-07-home-resolution-gate-family-reference-rule.md § C7b
function Resolve-ClaudeHomeBase {
    if ($env:CLAUDE_HOME) {
        $env:CLAUDE_HOME
    } elseif ($env:HOME) {
        $env:HOME
    } elseif ($env:USERPROFILE) {
        $env:USERPROFILE
    } else {
        $HOME
    }
}

$claudeHomeBase = Resolve-ClaudeHomeBase

$real = Join-Path (Join-Path (Join-Path $claudeHomeBase '.claude') 'bin') 'coordinator-settings-home.ps1'

if (-not (Test-Path $real)) {
    [Console]::Error.WriteLine("coordinator-settings-home: resolver not installed at $real -- run /coordinator:setup (Phase 3).")
    exit 127
}

& $real @args
exit $LASTEXITCODE
