# pipeline_doe_structured fixture provenance

`pipelines/deep-research/structured.manifest.yaml` and the four `structured-*-prompt-template.md`
files are byte copies of coordinator-content-repo `coordinator/pipelines/deep-research/` at coordinator-content-repo blob
`0dac31ee4` (last commit touching the manifest). They pin DoE's schema dialect (`phase`,
`max_concurrent`, inline `schema`, `subjects_mode`) so the route is exercised without DoE's tree.
Re-copy them when DoE changes the manifest; `test_pipeline_doe_manifests.py` compares them against
DoE's tree when it is present.
