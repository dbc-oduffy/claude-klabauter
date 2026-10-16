# pipeline_research_multi_repo fixture provenance

Hand-authored content root for the multi-repo research tests. Nothing here is a copy of a
Coordinator-content-repo file. `repo.manifest.yaml` takes the stage ids and the `chunks` / `haiku_scouts` lists
and `deepest` / `sonnet_scouts` flags that DoE's repo manifest declares, with one shortened shared
template and a final stage writing `synthesis.md`. `repo-compare.manifest.yaml` is the shape the
N-repo plan's C7 memo asks DoE to ship (DoE has no such manifest yet); its stage writes
`comparison.md`. The shared `pipeline_research/` tree is untouched.
