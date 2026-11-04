# pipeline_doe_structured fixture provenance

`pipelines/deep-research/structured.manifest.yaml` and the four `structured-*-prompt-template.md`
files are byte copies of coordinator-content-repo `coordinator/pipelines/deep-research/` at coordinator-content-repo blob
`0dac31ee4` (last commit touching the manifest). They pin DoE's schema dialect (`phase`,
`max_concurrent`, inline `schema`, `subjects_mode`) so the route is exercised without DoE's tree.
`structured-scope-prompt-template.md` was re-copied at coordinator-content-repo blob `15168e086a` (last commit touching
it; DoE HEAD `06caac3c0c`, 2026-11-04). The manifest and the other four templates still carry the older
blob and differ from DoE's tree.
Re-copy them when DoE changes the manifest; `test_pipeline_doe_manifests.py` compares them against
DoE's tree when it is present.
