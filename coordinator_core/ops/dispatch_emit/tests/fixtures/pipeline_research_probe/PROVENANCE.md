# pipeline_research_probe fixture provenance

`contract/scouts-ask-fragment.json` is a byte copy of coordinator-content-repo's
`coordinator/contract/scouts-ask-fragment.json` at 3ab1994b66 (identical at c150e22ae9).

`pipelines/deep-research/scouts.manifest.yaml` is hand-authored, a stand-in for DoE's not-yet-landed
C13 shape: `inputs.lists` gains `probes` (strings) and `inputs.flags` gains `local_probe`
(true/false). Stage `probe` (haiku, `scouts-probe-prompt-template.md`, fan_out over `inputs.probes`,
`when: {flag: local_probe}`, optional) precedes `scout`, which depends on it. The templates are
one-paragraph stubs. Replace the manifest with DoE's once C13 lands.
