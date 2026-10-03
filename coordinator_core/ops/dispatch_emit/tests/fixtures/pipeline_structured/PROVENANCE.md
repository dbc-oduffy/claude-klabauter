# pipeline_structured fixture provenance

`oracle/structured-research-fixture.workflow.mjs` is the output of coordinator-content-repo's
`coordinator/pipelines/deep-research/emit-structured-fire.py`, run at coordinator-content-repo sha `6d7e4bada`
(extracted with `git archive`, so no uncommitted DoE edit leaks in) with `--config
oracle/config.json --subjects oracle/subjects.txt`.

Two deliberate departures from a raw run:

- `scratch_root` was a real directory when the emitter ran. Every occurrence of that absolute
  prefix in the emitted script was rewritten to the literal `<scratch_root>`, and
  `oracle/config.json` carries the same placeholder.
- DoE's three `structured-*-prompt-template.md` files (28 KB together, repeated per subject and
  topic) were replaced for the run by one-line stubs that keep the emitter's `[MARKER]` tokens.
  Graph parity compares agent call sites, not prompt prose, and the real templates would add
  about 130 KB to the oracle.

The agent graph is unchanged by either departure: scout (haiku), verifier (sonnet, schema),
rebuttal (sonnet, schema), synthesizer (opus), the middle two inside `inChunks(`.

`pipelines/deep-research/structured.manifest.yaml` is the v1 manifest that models the same graph. It is an in-repo copy,
never DoE's file.

The oracle is frozen and still chunks at `MAX_CONCURRENT = 5`. Parity compares agent graph shape only (agent type,
model, schema, fanned), never chunk size; the pipeline route no longer has a default chunk size or a web-caller ceiling.
