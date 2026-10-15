# pipeline_research fixture provenance

Hand-authored fixture manifests for `test_research_emit.py`. `scouts` mirrors coordinator-content-repo's
`coordinator/pipelines/deep-research/scouts.manifest.yaml` (same dialect, shortened template).
`notebooklm` mirrors DoE's stage ids at 9ca2650434 (`scope`, `scout`, `workers` over
`inputs.notebooks`, `sweep` writing `merged-claims.json`) with one shortened shared template, so
`research_emit.gate_notebooklm` binds against the ids DoE ships. `web` (a `produces_brief` scope
stage), `nlm-preflight` (a `halts_unless` stage) and `unblock` (a roster list) are shapes DoE has
not yet committed; they pin the loader's dialect only and are not copies of any DoE file.
