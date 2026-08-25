"""coordinator_core.search -- in-process text-search building blocks that
let the engine answer a `grep`-shaped agent request with Python `re`
instead of spawning a `grep`/`egrep`/`fgrep`/`rg` child process.

Currently holds one module, `regex_translate`, the POSIX BRE/ERE -> Python
`re` pattern translator that unblocks that path (see its own module
docstring for the "why now" -- the 1,707-refusal measurement).
"""
