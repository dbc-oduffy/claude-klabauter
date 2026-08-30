r"""
coordinator_core.ops.session.guard_concrete_path_citations — generalized
concrete-absolute-path-citation detector, for ANY tracked text file, not
just `settings.json`.

Why this exists
----------------
`guard_foreign_platform_paths.py` (this package) only ever scanned
`settings.json` -- a live, machine-executable config. On 2026-07-28/29 a
hardcoded Windows path (`X:\\example-game-workbench-repo`) sitting in an
agent-readable PROSE doctrine file caused a live session to state a false
fact about where a repo lived: nothing in the settings.json-only guard ever
looked at that file. PM ruling: **any concrete full-path citation anywhere
in a repo is incorrect, including in doctrine and prose** -- broader than
"foreign to this host" (`guard_foreign_platform_paths.py`'s own scope,
host-conditioned by construction): a real username or drive-letter baked
into checked-in prose is wrong on EVERY host, including the one that wrote
it, because it names one operator's machine as if it were universal.

Scope is deliberately unrestricted by file type -- `.md` is explicitly IN
scope, unlike DoE-claude's older `coordinator/tests/test_no_absolute_path_
literals.py` (Rule A/B), which carried a documented decision to exclude
markdown as "documentary". That decision is what this incident falsifies:
a documentary-looking line in a doctrine file is exactly the kind of
citation a dispatched agent reads and TRUSTS. This module does not inherit
that exclusion -- prose is a first-class scan target, not a carve-out.
This "unrestricted by file type" claim covers four of the five rules below;
`dead-registry-rung` is the deliberate exception, scoped narrower for a
harm-model reason stated in its own section -- read that section before
assuming its narrower scope is an oversight to "fix".

What counts as a violation -- five independent shape rules
------------------------------------------------------------
  - `posix-home`   -- `/Users/<seg>/…` or `/home/<seg>/…` where `<seg>` is a
                       CONCRETE (non-placeholder) segment. Also the two
                       Windows-under-a-POSIX-root spellings of the same home
                       directory: WSL's `/mnt/<drive>/Users/<seg>/…` and
                       git-bash's `/<drive>/Users/<seg>/…`, which name one
                       operator's box exactly as much as the bare form does.
  - `drive-letter` -- any Windows drive-letter root, `[A-Za-z]:[\\/]`, via
                       the shared `_path_shape_regexes.WIN_DRIVE_RE` (the
                       lookbehind that keeps `https://` from matching as
                       drive `s:` lives there once, not copied here).
  - `unc`          -- a Windows UNC path, `\\\\host\\share`.
  - `mixed-separators` -- a path token anchored by one of the three roots
                       above that mixes `/` and `\\` within itself (e.g.
                       `C:\\Users/foo\\bar`) -- a literal that cannot even be
                       correct on ONE platform, so it is flagged independent
                       of whether its root segment is a placeholder.
  - `dead-registry-rung` -- two narrower literal shapes where a LIVE READ is
                       always wrong, regardless of whose username is (or
                       isn't) involved: the dead machine-local registry rung
                       (`~/.claude/machine-local`, `$HOME/.claude/machine-
                       local` -- the registry actually lives under
                       `~/.coordinator-claude-settings/machine-local`) and a
                       bare machine-specific plugin-exec path
                       (`~/.claude/plugins/...`, `$HOME/.claude/plugins/...`).
                       See its own section below -- unlike the other four,
                       this rule is both MENTION-AWARE and SCOPE-LIMITED to
                       executable/config file types, because (a) both shapes
                       are overwhelmingly cited documentarily, and (b), more
                       fundamentally, neither shape is machine-specific at
                       all -- so unlike the other four, a documentary mention
                       of one in prose is not even a latent defect, and the
                       rule's whole-corpus scope would be actively wrong.

Root-concreteness and segment-concreteness are DIFFERENT tests, and the
`drive-letter` rule applies both (a corpus-reconciliation correction to the
first cut of this rule, which conflated the two). The ROOT itself is not
symmetric between the two POSIX-home-style rules and `drive-letter`:
`/Users/` is universal, every macOS host has it, so `posix-home` never
flags the root, only a concrete SEGMENT after it. A drive-letter root
(`X:\`, `E:\`, ...) is itself one operator's drive mapping and exists on no
other machine -- so a BARE drive root with nothing after it (`X:\` alone,
"assets that don't belong on `X:\`") is a machine-specific assertion in its
own right and STAYS FLAGGED; it is not exempt just for lacking a segment.
Where symmetry DOES apply is the segment: the same placeholder word list
`posix-home` checks (`alice`, `bob`, `username`, `you`, `me`, `foo`,
`bar`, `baz`, `test`, `example`, `someone`, `operator`, `yourname`, any
`<angle-bracketed>` generic, the literal `...`, and any `$`-led runtime
substitution) exempts a `posix-home` segment OR a `drive-letter` path's
FIRST segment after the root, when that segment is present -- and, when
that first segment is a USER-PROFILE ROOT (`Users`/`home`), the segment
one deeper instead, since the profile root itself is a universal OS
convention and the ACCOUNT NAME behind it is the machine-specific part.
That deeper test is not a second implementation: it runs `_POSIX_HOME_RE`
itself over the token's post-root remainder (`_posix_home_account_segment`),
so `drive-letter` and `posix-home` cannot drift about where an account name
sits or what exempts one. Its absence was a HARD defect, not a missing
nicety: with only the first segment tested, `Users` was neither a
placeholder nor a well-known root, so a concrete drive-rooted home path AND
its own corrected form `C:/Users/<username>` both fired -- the rule
rejected every spelling of the path, leaving a flagged citation with no
satisfiable fix. A bare drive-plus-`Users` root (no account segment behind
it) stays flagged, per the drive-root paragraph above. This was the
real asymmetry the reconciliation found -- `posix-home` applies its
exemption at the segment; the first cut of `drive-letter` support applied
none at all, flagging the root-plus-segment as one indivisible unit. A
`drive-letter` path is additionally exempt when `...` appears as a segment
in the path token AND every segment between the root and that ellipsis is
itself placeholder-shaped -- unlike `posix-home`'s single fixed "username
slot", a drive-letter path has no one canonical position for "the
interesting segment", so a worked example like "a repo, then some subpath"
(`X:\...\topic.md`) needs the ellipsis check to look past the first segment.
The "every segment before it must be placeholder-shaped too" qualifier is
load-bearing, not incidental: a real citation followed by a natural
trailing `...` (`C:\Users\example-operator\project\...`) has a genuine concrete
segment (`example-operator`) ahead of the ellipsis and must NOT be swallowed by the
same exemption that legitimizes a worked shape illustration.

Well-known Windows system roots are not machine-specific either, and are
exempt by a SMALL, ENUMERATED name list, not a broad drive-letter carve-out:
the standard OS install directories (an installation convention, not one
operator's choice) and the debugger symbol-cache convention's root segment
(that convention's syntax embeds a URL right after the root, which also
made it double-flag as `mixed-separators` -- a real false positive this
list closes). See `_WELL_KNOWN_WIN_ROOT_SEGMENTS` below for the exact
enumerated set. Checked against the same first-segment-after-root position
as the placeholder check above.

`dead-registry-rung` -- scope-limited, and mention-aware within that scope
----------------------------------------------------------------------------
Ported from DoE-claude's older `coordinator/tests/test_no_absolute_path_
literals.py` (its former Rule B) when that gate was rewritten to consume
this shared module -- the rewrite widened scope but silently dropped this
rule's coverage for one review cycle before it was ported back in, WITH the
scope restriction its original design always carried (not a new narrowing).

WHY THIS RULE IS SCOPE-LIMITED WHILE THE OTHER FOUR ARE NOT -- the harm
model is genuinely different, not a hedge. The other four rules catch
MACHINE-SPECIFICITY: `/Users/<user>/...` or `X:\...` names one operator's
box, so the exact same sentence is FALSE on every other machine -- true
regardless of whether it appears in code or prose, which is why widening
those four to the whole corpus (including `.md`) was correct (see "Why this
exists" above). `dead-registry-rung` catches something else: a path that is
DEAD OR WRONG TO **READ**. `~/.claude/plugins/...` is `~`-rooted and carries
ZERO machine-specific information -- it is byte-identical on every host.
Its harm is narrower and different in kind: code that LIVE-READS
`~/.claude/machine-local` finds nothing and silently reports "key unset"
instead of failing loud. A changelog or daily-summary sentence narrating
"installed to ~/.claude/plugins/coordinator-claude" is a TRUE statement
about what happened and misleads no one reading it as history. Scanning the
whole corpus for this rule would score hundreds of correct historical
sentences as violations and wedge the commit gate on true content -- exactly
the failure mode ("a gate that fires on correct content trains the reader to
stop reading its output") the rest of this module's design works to avoid.
So `_DEAD_RUNG_SCOPE_EXTENSIONS` restricts the "executable code" half of
this rule's scope to `.py`/`.sh`/`.bats`, where a match is a live read by
construction. `_DEAD_RUNG_STRUCTURED_EXTENSIONS` (`.json`/`.toml`/`.yaml`/
`.yml`) covers the "config" half named in this rule's summary above --
config is machine-parsed data, not documentary prose, so a match there is
also plausibly a live read. An early revision of this scope excluded the
structured-data extensions entirely: a corpus reconciliation against
DoE-claude found every `.json`/`.yaml` hit under a naive whole-file scan
was a NARRATIVE RECORD that happens to use a structured serialization
format -- a bug-backlog entry, a lesson, a review-sidecar finding, a task
flight-recorder dump -- with the citation living in a free-text field
(`title:`, `body:`, `description:`, ...). That finding didn't justify
dropping structured formats from scope altogether, though -- it justified
building `_structured_data_documentary_lines` (below) as the SAME kind of
mention-awareness layer the AST/comment/echo layers already give code
files, so a genuine narrative field stays exempt while a non-prose key
(`cwd:`, `scope:`, ...) sitting in the same file stays caught. Re-read the
harm-model paragraph above: the other four rules catch MACHINE-
SPECIFICITY; this one catches a path DEAD OR WRONG TO **READ**, and both
executable code and machine-parsed config are places a "read" plausibly
happens. A live doctrine assertion of the dead rung as CURRENT ("the
registry lives at ~/.claude/machine-local", written today in a wiki or
skill) is the real incident shape this rule exists to prevent; markdown/
prose files still stay out of this rule's scope entirely (caught instead,
for a NEW write, by the write-time guard's `new_violations`, which
evaluates every new write regardless of file type before it ever becomes
"historical"). Only prose files are declined by this scope limit --
structured config is not.

Within that scope, mention-awareness is layered, and applies UNIFORMLY --
every layer runs regardless of file type, not one layer per extension (an
earlier revision of this rule branched entirely on `filename.endswith(".py")`
and skipped the comment/echo layer for `.py` files; that branching is what
let a live `#` comment through in `publish_sync.py` while the docstring
claimed comments were "excluded for free" -- they were never excluded at
all, just never covered by the narrower Python layer either):
  - Python detection is CONTENT-based (`ast.parse` attempted on every
    in-scope file), not extension-based. This fleet keeps some real Python
    files under a `.sh` extension on purpose, for bash-oracle-parity naming
    (`dev-sync.sh`, a `#!/usr/bin/env python3` script) -- gating on
    `filename.endswith(".py")` alone missed that file's own module-level
    documentation text entirely. Only a `.py`-EXTENSION file that fails to
    parse triggers the fail-safe (skip `dead-registry-rung` for the whole
    file, matching the original ported rule's behavior for genuinely broken
    Python source); a `.sh`/`.bats`/`.yaml`/`.json` file failing to parse as
    Python is the ordinary, expected case and falls through to the layers
    below instead.
  - Any bare string-literal `Expr` statement, anywhere in a successfully-
    parsed file's AST -- not just a module/class/function docstring at
    body-index-0. Widened deliberately: a bare, unused string literal is,
    BY CONSTRUCTION, never a live read regardless of its position in the
    body (Python evaluates and discards it), which is exactly the property
    this rule is checking for. `help=`/`description=`/`epilog=`/`usage=`
    keyword-argument string literals are excluded on the same reasoning --
    argparse text is shown to a human, never read as a path.
  - A `#`-comment line (stripped form starts with `#`) or a printed-message
    line (first token `echo`/`printf`) is skipped -- applies to EVERY
    in-scope file, Python included, not only shell.
  - A match inside a backtick-delimited span (`` `...` ``, markdown-style
    inline-code quoting used throughout this repo's own prose-in-source) is
    prose, skipped. A `${VAR:-default}` bash default-expansion span is
    scrubbed before matching -- `${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/
    plugins/...}` is the documented sanctioned fallback shape, not a bug.
  - `.json`/`.yaml`/`.yml`/`.toml` ADDITIONALLY get a structured-data
    prose-key exemption (`_structured_data_documentary_lines`): a line
    sitting inside a known PROSE-bearing key's value (`_PROSE_KEYS` --
    `title`, `body`, `description`, `summary`, etc., corpus-checked, not
    guessed -- see that constant's own comment) is documentary, because
    these keys hold free-text narrating a defect or an observation (a bug
    write-up, a lesson entry, a review finding's description), never a
    live path read. JSON string values never need continuation tracking (a
    raw newline can't appear inside one), but a YAML plain/unquoted scalar
    routinely folds across multiple lines with no repeated key --
    `_structured_data_documentary_lines` tracks the key line's indentation
    and covers every following line indented deeper, until a same-or-
    shallower-indent key, list item, or dedent ends the run.
The other four rules apply uniformly regardless of file type or context
(deliberately -- see each rule's own reasoning above); this file-type-
conditional scope AND mention-awareness are unique to `dead-registry-rung`
and are not a precedent for narrowing the other four.

Escape hatch -- same-line `abs-path-ok: <reason>`
----------------------------------------------------
A reason string is MANDATORY: a bare `abs-path-ok:` with nothing after the
colon (or only whitespace) does not exempt the line. This matters because a
genuine incident writeup (this guard's own originating incident, or the
sibling `foreign-platform-path-guard.md` wiki page) quotes the real
corrupted path AS the evidence and must stay quotable -- the escape hatch
exists for exactly that case, reviewable per-diff, never a silent registry.

Evidence-artifact exemption -- an ARTIFACT-CLASS carve-out, not a directory allowlist
--------------------------------------------------------------------------------------
Three tracked PREFIXES are machine-generated RECORDS of a citation that
exists elsewhere, not fresh corpus debt, and this module skips them entirely
(every rule, the whole file) rather than flag them:
  - `state/review-trail/diffs/` -- a frozen diff is a TRANSCRIPT of a
    rewrite. A sweep that replaced a real citation with a portable form
    produces a diff whose `-` line still contains the original literal, or
    the record has been falsified. Flagging the transcript asks the record
    to lie about what it recorded.
  - `state/review-findings/` -- machine-generated review output, and the
    same transcription argument twice over: a `**Working directory:**`
    provenance field the harness stamps in from the invoking environment,
    and embedded `diff.patch` bodies whose `-`/`+` lines are the frozen
    diffs of the case above. Neither ORIGINATES a citation; both quote one.
  - `state/subagent-share/` -- a reviewer/integrator sidecar QUOTES an
    offending line as the evidence for its own finding; that quotation IS
    the finding, not a new citation the guard hasn't seen yet.

All of these classes are STRUCTURALLY guaranteed to contain the literal they
document -- exempting them closes a circularity, not a hole: without it,
the guard flags the transcripts of its own findings, and that count grows
every time the guard (or a reviewer quoting one of its findings) is used.
This is why the carve-out is framed by what the file structurally IS
(a machine-generated record, or fixed test input the escape hatch cannot
reach), not by where it happens to live -- do not extend these prefixes to
a hand-authored surface just because it lives under one of them, and do not
read this section as precedent for exempting a *new* prefix without the
same "structurally guaranteed to transcribe, not originate" property.

A THIRD, EXACT-FILE (never prefix) class covers byte-exact serialization/
escaping conformance fixtures, where the literal is deliberately-chosen
synthetic test input and the file's format (JSON) has no comment syntax --
the `abs-path-ok:` escape hatch below is syntactically unusable there, and
any edit to add trailing marker text would itself change the exact bytes
the fixture asserts. See `_EVIDENCE_ARTIFACT_EXACT_FILES` for the
(deliberately short, individually-justified) allowlist -- an exact-file
match, not a prefix, so this can never silently widen into "any fixture"
the way a bare `/fixtures/` directory match once did in this fleet's own
`.sh`/`.bats` exclusion history.

Capture-data exemption -- a FORMAT-scoped class, not a directory allowlist
----------------------------------------------------------------------------
A sibling repo (DoE-claude) asked for `state/audits/data/` and
`state/recovery/` to be added to `_EVIDENCE_ARTIFACT_PATH_PREFIXES` above.
That literal ask is REJECTED as written: both directories mix
machine-generated capture data with hand-authored `.md` prose and `.py`
scripts, and a blanket prefix would silently exempt real, hand-authored
citations sitting right next to the capture data it's meant to cover.

The discriminator that makes the two directories above safe to exempt
whole is not "where the file lives" -- it's ARTIFACT STRUCTURE (a frozen
transcript, a quoted finding). Neither directory here has that property
uniformly: `state/recovery/` in THIS repo alone carries 121 hand-written
`.py` probes and `.md` findings that are ordinary corpus debt, not
transcripts of anything. So this class narrows the discriminator one step
further: FILE FORMAT. `.md` and `.py` both have comment syntax, so a
hand-authored file in either format keeps the `abs-path-ok:` escape hatch
and stays fully in scope, marker required, exactly like anywhere else in
the corpus -- being under `state/audits/data/` or `state/recovery/` earns
it NOTHING. A serialized capture format (`.json`, `.jsonl`, `.patch`,
`.diff`) has no comment syntax at all, so the escape hatch is
syntactically unusable there in exactly the way it's unusable for the
exact-file conformance-fixture class above -- that shared unusability is
the actual justification, not the directory name. See
`_CAPTURE_DATA_PATH_PREFIXES` for the prefix/extension pairing; this is
deliberately narrower than the ask that prompted it -- do not widen the
prefix list to cover a hand-authored surface just because a capture file
happens to sit in the same directory.

Comment-syntax-free formats -- format WITHOUT a prefix
----------------------------------------------------------------------------
Once file format is the discriminator, the prefix pairing is redundant for
any format that has no comment syntax ANYWHERE. `.json` and `.jsonl` have
none at any path, so pairing them with two directories left the rule
firing, outside those directories, on lines that can never carry the
`abs-path-ok:` remedy -- an undischargeable red, which is the defect this
guard's own escape hatch exists to prevent. `_COMMENT_SYNTAX_FREE_EXTENSIONS`
exempts them prefix-independently.

Measured before shipping, over every tracked file in this repo: all 61
`.json`/`.jsonl` files that were firing are machine-emitted state
(`state/cockpit-emission.json`, `state/ceremony/wsc/*`,
`state/memo-outbox/sent-ledger.jsonl`, golden fixtures). Zero
hand-authored JSON carrying a concrete path was in the firing set, so the
coverage this drops is coverage that had no discharge to begin with.

Position-scoped exemptions -- classes 2 and 3, a content-model parse
----------------------------------------------------------------------------
`abs-path-ok:` is a LINE COMMENT. Everything above this section shares one
justification for exempting content: the remedy is syntactically unusable
at that position. `.json`/`.jsonl` (the class above) earn that unusability
from their FILE FORMAT -- no position in a JSON file has comment syntax, so
an extension test is a sound proxy for "the hatch cannot work here". `.yaml`
and `.md` are different in kind: both formats DO have comment syntax, so the
unusability is POSITIONAL, not format-wide -- it depends on WHERE in the
file a match lands, and no extension test can express that. The governing
question for these two classes is therefore not "where does this file
live" or "what is this file's format" but "can THIS LINE carry the
remedy" -- answered by a content-model parse, not a prefix or extension
check.

  - Class 2 -- YAML **block scalars only** (`style` in `"|"`, `">"`), in a
    `.yaml`/`.yml` file body. Corrected from an earlier line-granular
    revision of this class that exempted EVERY YAML scalar style (plain,
    single-quoted, double-quoted, AND block) -- measured against a spike
    (`docs/research/spike-verdicts/2026-08-14-positional-parse-for-abs-
    path-ok-discharge.md`) and found wrong: a trailing `# abs-path-ok:
    <reason>` after a plain or quoted scalar is LEGAL YAML and leaves the
    parsed value byte-identical (`yaml.safe_load("note: at /path  #
    abs-path-ok: r")["note"] == "at /path"`), so the hatch is fully usable
    there and those two styles must stay IN SCOPE, marker required, same
    as anywhere else. Only a block scalar (`|`/`>`) genuinely corrupts the
    value -- inside one, the `#` becomes literal value content rather than
    a comment. Markdown YAML frontmatter is explicitly NOT covered by this
    class either, for the identical reason: a frontmatter value is a YAML
    scalar like any other, so a plain/quoted frontmatter value keeps the
    working hatch and stays in scope; only a block-scalar frontmatter
    value would qualify, and frontmatter bodies in this fleet's corpus
    don't use block scalars, so this class is scoped to `.yaml`/`.yml`
    files only.
    `_yaml_block_scalar_spans` implements this: `yaml.compose_all` gives a
    node graph with exact `start_mark`/`end_mark` character offsets
    (`.index`), so the check is a character-offset SPAN membership test,
    not a line-granularity approximation -- the earlier line-granular
    model over-exempted a `<path>: value` mapping line where the path was
    the KEY and only the value scalar should have been protected (see the
    spike verdict's premise-correction section); offset spans close that
    hole structurally rather than patching around it. Any exception while
    composing (malformed YAML, tabs where YAML forbids them, etc.) yields
    NO protected spans at all -- keep-firing, matching this module's
    uncertainty-resolves-to-keep-firing invariant.
  - Class 3 -- markdown code blocks. A match inside a fenced block
    (``` `…` ``` or `~~~…~~~`) or a 4-space/tab-indented block in a `.md`
    file is not a citation: an HTML comment would render as literal text
    inside the quoted transcript, so the hatch is unusable there for the
    same "corrupts what it annotates" reason, not because the block lacks
    comment syntax in the abstract. `_markdown_code_block_lines` implements
    this. An UNTERMINATED fence (no matching close before EOF) does not
    open an exempt span at all -- the alternative (treating the rest of
    the file as code because a stray triple-backtick appeared) would
    silently swallow real prose citations behind it, an under-firing
    failure in exactly the direction this module's own docstring warns is
    silent and ships.

Both classes are prose-preserving, not blanket per-file carve-outs: ordinary
markdown prose, YAML comments, and code outside these positions stay fully
in scope, marker required, exactly as before. `.patch`/`.diff` are
deliberately NOT generalized into either class -- they stay prefix-paired in
`_CAPTURE_DATA_PATH_PREFIXES`/`_CAPTURE_DATA_EXTENSIONS` above, because a
diff transcribes lines from files that DO have comment syntax, which is a
different (weaker) unusability argument than either class here relies on.

Detect-only
-----------
This module never rewrites a file -- it returns structured `Finding`
records (file, line, rule, matched text) for a caller to report or gate on.
See `coordinator_core.write_guards.guard_concrete_path_citations` for the
mid-session Write/Edit deny leg built on top of `detect_in_text`.

Performance
-----------
`scan_repo` is the one place this module touches disk at scale: one
`git ls-files -z` spawn, a cheap substring pre-filter before any regex work
(most tracked files mention none of the marker substrings at all), a size
cap on individual files, and each file opened at most once.
"""
from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import yaml
from coordinator_core.win_portability import leaf_spawn_creationflags
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple

from coordinator_core.ops.session._path_shape_regexes import WIN_DRIVE_RE

# --- root-shape regexes -------------------------------------------------

# POSIX home path with a captured user segment. The character class for the
# captured segment deliberately includes `$`, `{`, `}`, `<`, `>` so a
# runtime-substitution or placeholder-generic segment is captured whole
# (needed for the placeholder check below to see the FULL segment, not a
# truncated prefix).
#
# Two alternatives open the match, because a Windows home directory reaches
# a POSIX-looking root by two well-known translations, and BOTH put a bare
# drive letter immediately before `/Users` -- a character the plain
# lookbehind's excluded class `[A-Za-z0-9_./\\:-]` rejects, so a single
# lookbehind-only anchor silently accepted every one of them (the real gap:
# under-enforcement of the fleet's P0 platform, in both the commit-time
# gate and the write-time Write/Edit guard that share this constant):
#   - WSL      -- `/mnt/c/Users/<seg>/...`
#   - git-bash -- `/c/Users/<seg>/...`
# The drive-prefix alternative is deliberately NOT a bare "allow any
# preceding letter": it consumes an explicit `/mnt/<letter>` or `/<letter>`
# root that must itself clear the same lookbehind, and requires `/Users/`
# or `/home/` to follow immediately. So a mid-path coincidence
# (`docs/c/Users/...`, `pkg/mnt/d/home/...`) still fails to anchor, exactly
# as it did before, and the prefix is captured into the match text so a
# reported finding quotes the whole citation rather than a suffix of it.
_POSIX_HOME_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_./\\:-])(?:/mnt)?/[A-Za-z](?=/(?:Users|home)/)"
    r"|(?<![A-Za-z0-9_./\\:-]))"
    r"/(?:Users|home)/([A-Za-z0-9_.\-<>${}]+)"
    r"(?![A-Za-z0-9_.\-<>${}])"
)

# UNC path: \\host\share. The lookbehind keeps a THIRD leading backslash
# (an escaped-backslash artifact, e.g. `\\\\host\\share` in a JSON string)
# from being mis-anchored mid-token. The negative lookahead on the host
# segment excludes the Win32 DEVICE NAMESPACE forms `\\.\` and `\\?\`
# -- reserved syntax for a local kernel object, not a network share, and not
# machine-specific in the sense this rule cares about (the same device path
# is valid on every Windows host). Calibrated against a real false-positive:
# this shape recurs throughout this fleet's own Windows-compat doctrine, the
# openssh-agent-forwarding named-pipe path.  # abs-path-ok: documentary mention of the false-positive shape this fix excludes
#
# Third load-bearing subtlety, matching `_path_shape_regexes.WIN_DRIVE_RE`'s
# 2026-07-31 fix for the same JSON/Python string-escape false positive: a
# backslash-letter escape sequence (`\n`, `\r`, ...) inside an escaped JSON
# string supplies a bare backslash immediately followed by a single escape
# letter, which can phantom-match as the SHARE segment of a UNC path when a
# HOST segment happens to precede it on the same line (`\\sizing\n` -- exactly
# 2 leading backslashes; host segment "sizing", share segment "n" -- the
# escaped newline's own backslash masquerading as the UNC share separator).
# Review: coordinator:code-reviewer -- prior wording named a "share" segment
# reading "share", which does not occur in this example and misled the
# companion regression test's backslash count.
# The trailing negative lookahead on the share segment
# excludes a bare escape-letter/digit standing alone as the WHOLE share
# segment, same alphabet and same "whole segment, not merely starting with
# it" discrimination as `WIN_DRIVE_RE`'s own lookahead.
_UNC_RE = re.compile(
    r"(?<!\\)\\\\(?![.?]\\)[A-Za-z0-9_.\-]+\\(?![ntrbfv0](?![A-Za-z0-9_\-]))[A-Za-z0-9_.\-]+"
)

# The three anchors that open a "path token" candidate for the
# mixed-separator check -- a concrete drive letter, a UNC share, or a POSIX
# home root are the only shapes this module treats as "clearly the start of
# an absolute path", so mixed-separator detection rides on the same anchors
# rather than re-deriving its own notion of "looks like a path".
_ANCHOR_RES = (WIN_DRIVE_RE, _UNC_RE, _POSIX_HOME_RE)

# A contiguous non-whitespace, non-quote run starting at an anchor match --
# the "token" a mixed-separator check inspects for both slash kinds.
_TOKEN_RE = re.compile(r"[^\s\"'`]+")

# Truncates a `mixed-separators` candidate token at the first GATED
# string-escape pair -- see the comment at its use site in `detect_in_text`
# for the full rationale (same JSON/Python escape-letter false positive
# `WIN_DRIVE_RE`/`_UNC_RE` gate, applied here as a token-prefix cut rather
# than a match-position exclusion since this rule is not itself a regex).
# The lookahead is load-bearing: it requires the escape letter to be the
# WHOLE remaining segment (not a real segment merely starting with one of
# these letters), so a genuine path like `C:\test\project/sub\file` is never
# cut mid-segment.
_ESCAPE_CUT = re.compile(r"\\[ntrbfv0](?![A-Za-z0-9_\-])")

# `user` is deliberately ABSENT while `username` stays. `User` is a real,
# common Windows default-account segment, so exempting the bare word
# blanket-masks a genuine machine-specific citation on the platform this
# fleet treats as P0 -- `username` carries no such collision and was never
# in dispute. This restores a 2026-07-22 ratified finding that four sibling
# repos already carry; this constant was independently authored and drifted
# from it, never by a decision anyone took.
_PLACEHOLDER_WORDS = frozenset(
    {
        "alice", "bob", "username", "you", "me",
        "foo", "bar", "baz", "test", "example", "someone",
        "operator", "yourname",
        # UNC-shaped generics. Documentation explaining UNC syntax spells the
        # host segment as one of these; they name no machine any more than
        # `alice` names a person.
        "host", "hostname", "server", "share", "fileserver", "machine",
    }
)

# Small, enumerated set of well-known Windows system-install roots -- NOT a
# broad drive-letter carve-out (see module docstring). Checked against the
# first path segment after a drive-letter root, case-insensitively.
_WELL_KNOWN_WIN_ROOT_SEGMENTS = frozenset(
    {
        "windows", "program files", "program files (x86)", "programdata",
        # _NT_SYMBOL_PATH debugger symbol-cache convention's root segment.
        "symbols",
    }
)

# --- dead-registry-rung shapes -------------------------------------------

_DEAD_RUNG_RE = re.compile(r"(?:~|\$HOME)/\.claude/machine-local\b")
_PLUGIN_EXEC_RE = re.compile(r"(?:~|\$HOME)/\.claude/plugins\b")
_DEAD_RUNG_RES = (_DEAD_RUNG_RE, _PLUGIN_EXEC_RE)

# Cheap hint substrings -- neither regex above can match a line that lacks
# one of these, so a per-file AST parse (the expensive part of this rule) is
# only attempted when at least one is present.
_DEAD_RUNG_HINTS = ("claude/machine-local", "claude/plugins")

# `dead-registry-rung`'s file scope is EXECUTABLE/CONFIG file types -- NOT
# the whole corpus the other four rules cover. Two sub-classes, each with
# its own documentary-exemption layer: executable code (`.py`/`.sh`/
# `.bats`), where the AST/comment/echo layers apply, and structured config
# (`.json`/`.toml`/`.yaml`/`.yml`), where the `_structured_data_documentary_
# lines` prose-key exemption applies instead (see the module docstring's
# "dead-registry-rung -- scope-limited" section and its "Within that scope"
# layering list). The other four rules catch MACHINE-SPECIFICITY, true in
# code or prose alike; `dead-registry-rung` catches a path DEAD OR WRONG TO
# READ, which narrows its scope to files where a match is plausibly a live
# read -- executable code always, and structured config because it is
# machine-parsed data, NOT documentary prose (a bug-backlog/lesson/review-
# sidecar entry narrating a defect in a `title:`/`body:`/`description:`
# free-text field is exempted by the prose-key layer instead of by scope
# exclusion, so a non-prose key like `cwd:` or `scope:` still gets scanned,
# per the module docstring's "the prose-key exemption is a named list, not
# a blanket YAML carve-out"). Markdown/prose files stay out of scope
# entirely -- only these two sub-classes are ever scanned for this rule.
_DEAD_RUNG_SCOPE_EXTENSIONS = (".py", ".sh", ".bats")
_DEAD_RUNG_STRUCTURED_EXTENSIONS = (".json", ".toml", ".yaml", ".yml")

# argparse/CLI text: shown to a human, never traversed as a filesystem path.
_DOC_KEYWORD_ARGS = frozenset({"help", "description", "epilog", "usage"})

_BACKTICK_SPAN_RE = re.compile(r"`[^`\n]*`")
_DEFAULT_EXPANSION_RE = re.compile(r"\$\{[^{}\n]*:-[^{}\n]*\}")


def _spans(rx: "re.Pattern[str]", text: str) -> List[Tuple[int, int]]:
    return [m.span() for m in rx.finditer(text)]


def _inside_any_span(start: int, end: int, spans: List[Tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


def _py_documentary_lines(text: str) -> Optional[Set[int]]:
    """Line numbers covered by a bare string-literal `Expr` statement
    (a module/class/function docstring, OR any other unused string-literal
    statement -- e.g. a header/purpose block placed after a `from __future__
    import` line, which is not technically `ast.get_docstring()`'s notion of
    "the" docstring since it isn't body[0], but is syntactically identical:
    a bare string expression Python evaluates and immediately discards,
    never assigned, never read, never live-executed) or a `help=`/
    `description=`/`epilog=`/`usage=` keyword-argument string literal --
    the `dead-registry-rung` rule's Python mention-awareness. Widening from
    "only body[0]" to "any bare string-Expr statement" is deliberate, not a
    loosened check: a bare, unused string literal is BY CONSTRUCTION never a
    live read regardless of its position in the body, which is exactly the
    property this rule cares about. `None` (not an empty set) signals `text`
    failed to parse as Python; the caller must then skip the rule entirely
    for this file rather than guess at documentary boundaries in broken
    source."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    lines: Set[int] = set()

    def _cover(const: ast.Constant) -> None:
        start = const.lineno
        end = getattr(const, "end_lineno", start) or start
        lines.update(range(start, end + 1))

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            _cover(node.value)
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if (
                    kw.arg in _DOC_KEYWORD_ARGS
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    _cover(kw.value)
    return lines


# --- structured-data (YAML/JSON/TOML) prose-key exemption -----------------
# Corpus-checked, not guessed: `body`, `summary`, `description`,
# `suggested_fix`, `how_to_apply`, `one_liner`, `title`, `decomposition`,
# `filled`, `scout_evidence` are every distinct key this rule's own
# DoE-claude corpus scan found carrying a `dead-registry-rung` shape
# (bug-backlog/lesson/review-sidecar/task-flight-recorder/sizing-scout
# entries narrating a defect or an observation, never live-reading a
# path). `rationale`, `notes`, `text`, `evidence` are included on the same
# reasoning even though this corpus doesn't currently exercise them under
# this rule -- they are the same free-text-field shape used elsewhere in
# this fleet's queue/record schemas. Deliberately excluded: `cwd` -- a
# captured-evidence field (a frozen hook-payload snapshot) legitimately
# quotes a real path as historical evidence too, but it is a literal-VALUE
# field, not a prose field, and folding it into this list would blur the
# distinction this list exists to draw; that residual is a documented,
# individually-marked case, not a class this mechanism should swallow.
_PROSE_KEYS = frozenset(
    {
        "title", "body", "description", "summary", "notes", "text",
        "rationale", "evidence", "suggested_fix", "how_to_apply",
        "one_liner", "decomposition", "filled", "scout_evidence",
    }
)

# Matches a YAML/JSON/TOML "key: value" or "key = value" line, optionally
# quoted (JSON) and optionally list-item-prefixed (YAML `- key: value`).
# Captures the key name and its indentation depth for the caller to track
# YAML's plain (unquoted) scalar folding, where a value continues on
# following lines with NO repeated key -- JSON strings never need this
# (a raw newline can't appear inside one; an embedded "\n" is an escape on
# one physical line), but a YAML plain scalar routinely wraps.
_STRUCTURED_KEY_LINE_RE = re.compile(
    r'^(?P<indent>[ \t]*)(?:-[ \t]+)?"?(?P<key>[A-Za-z_][A-Za-z0-9_]*)"?[ \t]*[:=][ \t]?'
)


def _structured_data_documentary_lines(text: str) -> Set[int]:
    """Line numbers sitting inside a known PROSE-bearing key's value (see
    `_PROSE_KEYS`) in YAML/JSON/TOML text. A key line itself is covered when
    its key is a prose key; a following line is covered too, as a folded-
    scalar CONTINUATION, only while it stays indented deeper than that key
    line AND doesn't itself start a new key -- the same rule that ends a
    continuation (a same-or-shallower-indent key, list item, or dedent)
    also fires for a NON-prose key line, so an active prose continuation
    never bleeds past the next real key regardless of that key's own
    prose-ness."""
    lines_out: Set[int] = set()
    active_indent = -1
    in_prose = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        m = _STRUCTURED_KEY_LINE_RE.match(line)
        if m:
            indent = len(m.group("indent"))
            if m.group("key") in _PROSE_KEYS:
                in_prose = True
                active_indent = indent
                lines_out.add(lineno)
            elif in_prose and indent > active_indent:
                # Review: coordinatorcode-reviewer-3e4f4e1b — a folded
                # block-scalar continuation line that merely starts with a
                # `Word:` shape (e.g. "Note:", "Fix:") still matches the
                # key-line regex, but it's still folded content of the
                # active prose value, not a sibling key, while its indent
                # stays deeper than the prose key's own indent.
                lines_out.add(lineno)
            else:
                in_prose = False
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        if in_prose and indent > active_indent:
            lines_out.add(lineno)
        else:
            in_prose = False
    return lines_out


# --- position-scoped content model -- YAML scalars, markdown code blocks --
# (classes 2 and 3 -- see the module docstring's "Position-scoped
# exemptions" section for the full rationale.)


def _walk_yaml_value_nodes(
    node: "yaml.nodes.Node", is_value: bool, spans: List[Tuple[int, int]]
) -> None:
    """Recurse a `yaml.compose_all` node graph, tracking which nodes are a
    VALUE position (as opposed to a mapping KEY) -- keys never protect a
    citation even when key-shaped text happens to look like one, matching
    the module docstring's "mapping KEYS of any style" negative-spec item.
    A `ScalarNode` that is a value AND whose `.style` is `"|"` or `">"`
    (PyYAML normalizes every folded-scalar chomping/indicator spelling --
    `>`, `>-`, `>+`, `>1`, ... -- down to the single style character `">"`,
    same for `|`) gets its exact character-offset span recorded via
    `start_mark.index`/`end_mark.index`, which PyYAML's `Mark` already
    computes as an absolute offset into the composed stream -- no separate
    line-start table is needed to get there."""
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            _walk_yaml_value_nodes(key_node, False, spans)
            _walk_yaml_value_nodes(value_node, True, spans)
    elif isinstance(node, yaml.SequenceNode):
        for item_node in node.value:
            _walk_yaml_value_nodes(item_node, is_value, spans)
    elif isinstance(node, yaml.ScalarNode):
        if is_value and node.style in ("|", ">"):
            spans.append((node.start_mark.index, node.end_mark.index))


def _yaml_block_scalar_spans(text: str) -> List[Tuple[int, int]]:
    """Character-offset spans (into `text`) covering every YAML block
    scalar (`|`/`>`) VALUE in `text`, across every document in a possibly
    multi-document stream. `[]` on ANY compose failure (malformed YAML,
    tabs where YAML forbids them, ...) -- the caller then protects nothing,
    which keeps every citation firing rather than guessing at a boundary
    in text that didn't even parse. See the module docstring's "Position-
    scoped exemptions" section, class 2, for the corrected (block-scalar-
    only, not every scalar style) model this implements."""
    spans: List[Tuple[int, int]] = []
    try:
        for doc_node in yaml.compose_all(io.StringIO(text)):
            if doc_node is not None:
                _walk_yaml_value_nodes(doc_node, True, spans)
    except Exception:
        return []
    return spans


# Recognizes a YAML frontmatter OPENING delimiter -- ONLY when `---` is
# literally the first line of the file. A `---` appearing anywhere else in
# a `.md` file is a markdown horizontal rule, not a frontmatter fence, and
# must not be mistaken for one -- this is the sole discriminator, matching
# the convention every frontmatter-consuming tool in this fleet already
# relies on.
_FRONTMATTER_DELIM_RE = re.compile(r"^-{3,}[ \t]*$")


def _frontmatter_range(doc_lines: List[str]) -> Optional[Tuple[int, int]]:
    """`(first_content_line, last_content_line)`, 1-based and inclusive, for
    the YAML content strictly BETWEEN a leading `---` delimiter and its
    matching close -- `None` if the file doesn't open with one, or if no
    closing delimiter exists before EOF. The unterminated case deliberately
    returns `None` rather than treating the rest of the file as
    frontmatter: an opening `---` with no close is not a frontmatter block
    at all, and swallowing everything after it would be exactly the
    silent-under-firing failure this module's own docstring warns against."""
    if not doc_lines or not _FRONTMATTER_DELIM_RE.match(doc_lines[0]):
        return None
    for idx in range(1, len(doc_lines)):
        if _FRONTMATTER_DELIM_RE.match(doc_lines[idx]):
            if idx == 1:
                return None  # empty frontmatter -- no content lines
            return (2, idx)  # 1-based, between the two delimiter lines
    return None


# A fenced code block's opening line: up to 3 leading spaces (CommonMark's
# own indentation allowance before a fence still counts as "not indented
# code"), then 3-or-more backticks or tildes. The captured run's character
# and length are what the closing fence must match or exceed.
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")

# A 4-space or tab-indented, non-blank line -- CommonMark's indented code
# block shape.
_INDENTED_CODE_RE = re.compile(r"^(?: {4,}|\t)")

# A list-item marker at ANY indent. The capture groups give the marker's own
# indent and its width, which together fix the item's content indent -- the
# baseline an indented code block inside that item must clear by a FURTHER
# four columns.
#
# The indent is deliberately uncapped. Capping it at CommonMark's top-level
# 0-3 looks right and is not: at three levels of nesting the markers
# themselves sit past column 3, so a capped pattern stops matching, the
# threshold freezes at the last recognised level, and the deep item's own
# paragraph text starts clearing a stale-low bar and is silently exempted as
# code. Reaching this branch already proves the line is NOT code -- the
# threshold test runs first -- so an uncapped indent cannot swallow a real
# indented block.
_LIST_MARKER_RE = re.compile(r"^( *)(?:[-*+]|\d{1,9}[.)])( +)(?=\S)")


def _line_indent(line: str) -> int:
    """Visual indent, tabs expanded to CommonMark's 4-column tab stops."""
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4 - (width % 4)
        else:
            break
    return width


def _markdown_code_block_lines(doc_lines: List[str], start: int = 1) -> Set[int]:
    """Line numbers (1-based) inside a fenced (``` or `~~~`) or 4-space/
    tab-indented code block in markdown text, scanning from `start` (1-
    based, inclusive) onward -- `start` defaults to 1 but a caller passes
    the first line AFTER a leading frontmatter block so a fence-like
    coincidence inside frontmatter is never mistaken for a code fence
    (frontmatter content is never emitted as a protected span either way,
    since class 2 no longer covers `.md` frontmatter at all -- see the
    module docstring's "Position-scoped exemptions" section). An
    UNTERMINATED fence (no matching close before EOF) opens NO exempt span
    at all -- see that same section for why the alternative (treating the
    remainder of the file as code) is the wrong direction to fail in."""
    out: Set[int] = set()
    n = len(doc_lines)
    i = start - 1
    in_indented_block = False
    list_content_indent: Optional[int] = None
    while i < n:
        line = doc_lines[i]
        m = _FENCE_OPEN_RE.match(line)
        if m:
            in_indented_block = False
            fence_char = m.group(1)[0]
            fence_len = len(m.group(1))
            close_re = re.compile(
                r"^[ \t]{0,3}" + re.escape(fence_char) + "{" + str(fence_len) + ",}[ \t]*$"
            )
            close_idx = None
            for j in range(i + 1, n):
                if close_re.match(doc_lines[j]):
                    close_idx = j
                    break
            if close_idx is not None:
                for k in range(i, close_idx + 1):
                    out.add(k + 1)
                i = close_idx + 1
                continue
            i += 1
            continue
        if not line.strip():
            # A blank line ends an in-progress indented-code run (its next
            # non-blank successor must itself be preceded-by-blank to
            # start a NEW one) but a blank line is never itself emitted.
            in_indented_block = False
            i += 1
            continue
        # The threshold test runs FIRST, before the list-marker match. A line
        # deep enough to be code is code even when it looks like a list
        # marker, and running this first is what lets the marker pattern
        # accept any indent without ever swallowing a real indented block.
        indent = _line_indent(line)
        threshold = 4 if list_content_indent is None else list_content_indent + 4
        prev_blank = i > 0 and not doc_lines[i - 1].strip()
        if (
            _INDENTED_CODE_RE.match(line)
            and indent >= threshold
            and (in_indented_block or prev_blank)
        ):
            # A genuine indented code block: it clears the threshold set by
            # any list item still open, and it either opens after a blank
            # line or continues a run with no intervening blank.
            out.add(i + 1)
            in_indented_block = True
            i += 1
            continue
        in_indented_block = False
        marker = _LIST_MARKER_RE.match(line)
        if marker:
            # Opening a list item rebases the threshold: content sits at the
            # marker's indent plus its width, and only FOUR columns beyond
            # THAT is code. Inside an item, a merely-4-space line is the
            # item's own paragraph -- a CommonMark LAZY CONTINUATION, not
            # code, and it must stay in scope.
            list_content_indent = len(marker.group(0))
        elif indent == 0:
            # A non-blank line at the margin closes any open list item, so
            # the threshold drops back to the document's own.
            list_content_indent = None
        i += 1
    return out


# Two spellings, both honored. `foreign-path-ok:` is the older token from the
# sibling host-conditioned guard and already carries ~132 adjudicated reasons
# across this fleet's trees; a marker's job is to record a human decision ONCE
# and keep working, so recognizing only the newer spelling would silently void
# every one of them. Migrating those lines to the new token buys nothing and
# churns files other sessions are editing -- accept both instead.
_MARKER_TOKENS = ("abs-path-ok:", "foreign-path-ok:")

# Anchors a marker token so it must read as intentional -- start of line, or
# preceded by whitespace/punctuation (a comment leader like `#`, `//`,
# `<!--`, or plain prose punctuation) -- never embedded mid-word. Without
# this, `line.find(token)` matched the marker as a bare substring anywhere
# in the line, so a marker spelled out INSIDE a longer word or sentence
# (discussing the marker itself, or an accidental boundary collision) could
# exempt the entire line, including a genuine path citation elsewhere on
# that same line that has nothing to do with the marker.
_MARKER_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_])(?:abs-path-ok|foreign-path-ok):"
)


def _is_placeholder_segment(seg: str) -> bool:
    core = seg.rstrip("/\\")
    if core == "...":
        return True
    if core.startswith("<") and core.endswith(">"):
        return True
    if core.startswith("$"):
        # Any runtime-substitution form ($USER, ${USER}, or any other
        # $NAME/${NAME}) -- resolved at run time, never a baked-in literal.
        return True
    return core.lower() in _PLACEHOLDER_WORDS


def _first_segment_after_root(token: str, root_len: int) -> str:
    """The path segment immediately following a matched root (a 3-character
    `WIN_DRIVE_RE` match: drive letter, colon, one separator), up to the
    next separator or end of token. Empty string for a bare root with
    nothing after it."""
    rest = token[root_len:]
    m = re.match(r"[^/\\]*", rest)
    return m.group(0) if m else ""


def _has_ellipsis_segment(token: str, root_len: int) -> bool:
    """True iff `...` appears as a whole path segment past the root in
    `token` AND every segment between the root and that ellipsis is itself
    placeholder-shaped -- this repo's own documented convention for "some
    segment goes here" in a worked path-shape example (a drive root, an
    ellipsis, then a filename). Not pinned to the first segment: unlike
    `posix-home`, a drive-letter path has no single canonical position for
    the illustrative placeholder.

    The "every prior segment is placeholder-shaped" qualifier is the fix for
    a real false negative: without it, a genuine citation that merely
    trails off with a natural typographic ellipsis was fully exempt because
    SOME segment, anywhere, was `...` -- even though a real, concrete
    segment (a real username) sat right there ahead of it. Requiring every
    segment before the ellipsis to already read as a placeholder keeps the
    worked-example shape exempt while a real path that happens to trail off
    stays flagged on its own concrete segment."""
    for seg in re.split(r"[\\/]+", token[root_len:]):
        if seg == "...":
            return True
        if seg and not _is_placeholder_segment(seg):
            return False
    return False


# The two user-profile root segments a drive-letter path can carry. Under
# one of these, the FIRST segment is a universal OS convention (every
# Windows host has a drive-rooted `Users` folder) and the ACCOUNT NAME one
# segment deeper is the machine-specific part -- exactly `posix-home`'s own
# structure.
_USER_PROFILE_ROOT_SEGMENTS = frozenset({"users", "home"})


def _posix_home_account_segment(token: str, root_len: int) -> Optional[str]:
    """The account-name segment of a drive-letter token whose first segment
    after the root is a user-profile root, obtained by running the SAME
    `_POSIX_HOME_RE` over the token's post-root remainder that `posix-home`
    runs over a bare POSIX home path -- same regex, same capture group, same
    notion of where the account name sits. `None` when the token is not that
    shape at all, INCLUDING a bare drive-plus-`Users` root with no account
    segment behind it (the regex requires at least one character there), so
    that spelling stays non-exempt.

    Delegating rather than re-deriving is the point: a parallel
    "segment after Users" implementation is precisely the drift that left
    the `drive-letter` rule with NO satisfiable placeholder spelling --
    `/Users/<username>` passed while `C:/Users/<username>` was rejected for
    the same reason its concrete form was, so every spelling of a Windows
    home path was a violation and a real citation could not be corrected."""
    rest = "/" + token[root_len:].replace("\\", "/")
    m = _POSIX_HOME_RE.match(rest)
    return m.group(1) if m else None


def _rest_segments_after(token: str, root_len: int, first_seg: str) -> List[str]:
    """Every non-empty path segment in `token` past the first one after the
    root -- used to check for concreteness further along a token whose
    first segment alone doesn't decide exemption (see
    `_is_win_drive_root_exempt`)."""
    rest = token[root_len + len(first_seg):]
    return [s for s in re.split(r"[\\/]+", rest) if s]


def _is_win_drive_root_exempt(token: str, root_len: int) -> bool:
    """True iff the WHOLE drive-letter token is exempt. The segment right
    after the root being a placeholder exempts unconditionally (matching
    `posix-home`'s single-segment test). A well-known Windows system root
    (`Windows`, `Program Files`, `symbols`, ...) is an installation or
    debugger-cache convention, not a machine-specific claim, and a bare
    well-known root with nothing further stays exempt -- but the exemption
    must not swallow a REAL segment smuggled in deeper in the same token.
    A first segment naming a USER-PROFILE ROOT (`Users`/`home`) is handled
    before either of those: that segment is a universal OS convention, so
    the concreteness test moves one segment deeper, onto the ACCOUNT NAME,
    and is delegated to `_posix_home_account_segment` so it is literally
    `posix-home`'s own regex and capture group deciding it. Without this the
    rule had no satisfiable placeholder form at all -- a concrete
    drive-rooted home path fired because `Users` is neither placeholder nor
    well-known, and `C:/Users/<username>` fired for exactly the same reason,
    so the guard rejected the corrected spelling of every citation it
    flagged. A bare drive-plus-`Users` root with no account segment is still
    NOT exempt: the drive root is machine-specific in its own right, per the
    paragraph above, and there is no account name to adjudicate.

    Two distinct shapes carry that risk, and each gets its own narrow check
    rather than a blanket "every remaining segment must be a placeholder"
    (which would also re-flag entirely generic, non-machine-specific
    subpaths like a Windows system-directory or executable name):
      - `symbols` -- the `_NT_SYMBOL_PATH` convention's OWN syntax embeds a
        URL directly after the root (`http:`/`https:`); anything else in
        that position is exactly the shape a real, machine-specific
        symbol-server name takes, so only the URL shape stays exempt.
      - any OTHER well-known root -- generic subpaths under it are fine,
        but a nested user-profile segment (a `Users` or `home` segment
        followed by another segment) is the one shape that smuggles a real
        identity into an otherwise-generic install path, so the segment
        immediately following such an anchor is checked for concreteness."""
    first_seg = _first_segment_after_root(token, root_len)
    if _is_placeholder_segment(first_seg):
        return True
    if first_seg.lower() in _USER_PROFILE_ROOT_SEGMENTS:
        account = _posix_home_account_segment(token, root_len)
        return account is not None and _is_placeholder_segment(account)
    if first_seg.lower() not in _WELL_KNOWN_WIN_ROOT_SEGMENTS:
        return False
    rest_segments = _rest_segments_after(token, root_len, first_seg)
    if not rest_segments:
        return True
    if first_seg.lower() == "symbols":
        return rest_segments[0].lower().startswith(("http:", "https:"))
    for i in range(len(rest_segments) - 1):
        if rest_segments[i].lower() in ("users", "home") and not _is_placeholder_segment(
            rest_segments[i + 1]
        ):
            return False
    return True


@dataclass(frozen=True)
class Finding:
    """One concrete-path-citation violation."""

    file: str
    line: int
    rule: str  # "posix-home" | "drive-letter" | "unc" | "mixed-separators" | "dead-registry-rung"
    matched: str


def _line_has_marker_with_reason(line: str) -> bool:
    for m in _MARKER_RE.finditer(line):
        if line[m.end():].strip():
            return True
    return False


def _extract_token(text: str, start: int) -> str:
    m = _TOKEN_RE.match(text, start)
    return m.group(0) if m else text[start : start + 1]


# --- evidence-artifact exemption ------------------------------------------
# See the module docstring's "Evidence-artifact exemption" section for the
# full ARTIFACT-CLASS rationale -- this is not a directory allowlist, it is
# a recognition that these two prefixes are, by construction, machine-
# generated RECORDS of a citation that exists elsewhere (a frozen diff
# transcript, a reviewer sidecar quoting its own finding), not fresh corpus
# debt. `filename` is repo-relative (as `scan_repo` and the write-time
# guard's `_repo_relative` both hand in), so a prefix match here is a
# specific artifact CLASS, not an ambient "any file under state/" carve-out
# -- `state/lessons/`, `state/handoffs/`, etc. are NOT covered and stay
# fully in scope.
#
# NOT unified with `fix_concrete_path_citations._RECORDED_PATH_PREFIXES`
# even though that module already imports from this one. The two sets are
# deliberately different, not drifted: the fixer's set covers
# `state/review-trail/` WHOLE, because a wrong REWRITE of a hand-authored
# review note is merely an un-made correction; this set stops at
# `state/review-trail/diffs/`, because a wrong EXEMPTION there would stop
# scanning hand-authored `.md` findings that DO live directly under
# `state/review-trail/` and are ordinary corpus debt. Merging them would
# have to pick one scope and silently change the other module's behavior --
# a detect-side hole or a remedy-side falsification. Left as two constants
# with this comment naming the asymmetry, so a future reader finds the
# reason rather than assuming drift.
_EVIDENCE_ARTIFACT_PATH_PREFIXES: Tuple[str, ...] = (
    "state/review-trail/diffs/",
    # Machine-generated review output that TRANSCRIBES paths it did not
    # originate: a `**Working directory:**` provenance header the harness
    # stamped in, and embedded `diff.patch` bodies carrying the same frozen
    # `-`/`+` lines the review-trail diffs do. Structurally the same
    # "guaranteed to transcribe, not originate" property as the two
    # siblings, which is the test this list is framed by -- not "lives
    # under state/".
    "state/review-findings/",
    "state/subagent-share/",
)

# A THIRD, narrower evidence-artifact class: byte-exact serialization/
# escaping conformance fixtures, where the literal is a deliberately-chosen
# synthetic test input (exercising backslash/quote escaping specifically),
# not documentation of a real path, and the same-line `abs-path-ok:` marker
# escape hatch is unusable -- JSON has no comment syntax, and adding any
# trailing text to the line would itself change the exact bytes the fixture
# asserts, corrupting the very thing under test. An EXACT-file allowlist
# (never a prefix/directory match -- deliberately narrower than the two
# prefixes above) so this can't silently grow into a "any fixture" carve-out
# the way a `/fixtures/` directory match once did in this fleet's own
# `.sh`/`.bats` exclusion history (see `test_bash_exclusion_gate.py`'s own
# post-mortem comment on why a directory name was dropped as a condition).
_EVIDENCE_ARTIFACT_EXACT_FILES = frozenset(
    {
        "coordinator/tests/fixtures/step-zero-conformance.json",
    }
)

# A FOURTH evidence-artifact class, FORMAT-scoped rather than directory-
# scoped -- see the module docstring's "Capture-data exemption" section for
# the full rationale. Deliberately narrower than the ask that prompted it:
# `state/audits/data/` and `state/recovery/` both mix machine-generated
# capture data with hand-authored `.md`/`.py` content, so only a file BOTH
# under one of these prefixes AND in one of these extensions is exempt --
# a `.md` or `.py` file under the same prefix is untouched by this
# constant and stays fully in scope, marker required. The extension set is
# every serialized capture format in the corpus that lacks comment syntax
# (so the `abs-path-ok:` escape hatch is syntactically unusable there, the
# same property the exact-file conformance-fixture class above relies on)
# -- `.json`/`.jsonl` for structured capture dumps, `.patch`/`.diff` for
# frozen capture-time diffs.
_CAPTURE_DATA_PATH_PREFIXES: Tuple[str, ...] = (
    "state/audits/data/",
    "state/recovery/",
)
_CAPTURE_DATA_EXTENSIONS: Tuple[str, ...] = (".json", ".jsonl", ".patch", ".diff")

# The subset of `_CAPTURE_DATA_EXTENSIONS` whose no-comment-syntax property
# holds for the WHOLE file regardless of where it lives, so the prefix
# pairing above buys nothing and only produces undischargeable reds outside
# the two directories -- see the module docstring's "Comment-syntax-free
# formats" section. `.patch`/`.diff` stay prefix-paired: a diff body
# transcribes lines from files that DO have comment syntax, so its
# unusability argument is weaker and no caller has asked for it.
_COMMENT_SYNTAX_FREE_EXTENSIONS: Tuple[str, ...] = (".json", ".jsonl")


def _is_evidence_artifact(filename: str) -> bool:
    if not filename:
        return False
    if filename.startswith(_EVIDENCE_ARTIFACT_PATH_PREFIXES):
        return True
    if filename in _EVIDENCE_ARTIFACT_EXACT_FILES:
        return True
    if filename.lower().endswith(_COMMENT_SYNTAX_FREE_EXTENSIONS):
        return True
    if filename.startswith(_CAPTURE_DATA_PATH_PREFIXES) and filename.lower().endswith(
        _CAPTURE_DATA_EXTENSIONS
    ):
        return True
    return False


def detect_in_text(text: str, filename: str = "") -> List[Finding]:
    """Pure, filesystem-free -- returns every `Finding` in `text`.

    A line carrying `abs-path-ok: <non-empty reason>` is skipped entirely
    (for all four rules); a bare `abs-path-ok:` with no reason is NOT an
    exemption and the line is scanned normally.

    A `filename` matching `_EVIDENCE_ARTIFACT_PATH_PREFIXES`, an exact file
    in `_EVIDENCE_ARTIFACT_EXACT_FILES`, or the format-scoped capture-data
    class (`_CAPTURE_DATA_PATH_PREFIXES` + `_CAPTURE_DATA_EXTENSIONS`)
    short-circuits to no findings at all -- see the module docstring's
    "Evidence-artifact exemption" and "Capture-data exemption" sections. An
    empty `filename` (the default, and what a caller with no repo-relative
    path to hand in passes) can never match any of the three, so this is a
    no-op for any caller that doesn't supply one.

    A `.yaml`/`.yml` file also exempts a match sitting inside a YAML BLOCK
    SCALAR (`|`/`>`) value (class 2, char-offset-span-scoped -- NOT plain
    or quoted scalars, and NOT markdown frontmatter, both of which stay
    fully in scope because the hatch is lossless there); a `.md` file also
    exempts a match inside a fenced or 4-space/tab-indented code block
    (class 3, line-scoped) -- both positional, computed from `filename`'s
    extension, and both apply only to the four rules that scan the whole
    corpus (not `dead-registry-rung`, which keeps its own, separately-
    scoped mention-awareness). See the module docstring's "Position-scoped
    exemptions" section. An empty `filename` matches neither extension, so
    this is likewise a no-op without one.
    """
    if _is_evidence_artifact(filename):
        return []

    findings: List[Finding] = []

    # `dead-registry-rung` mention-awareness setup -- computed once for the
    # whole text, not per line. `_dead_rung_hinted` short-circuits the AST
    # parse and structured-data scan (the expensive parts) for the
    # overwhelming majority of scoped files, which mention neither shape at
    # all.
    #
    # Python detection is CONTENT-based (attempt `ast.parse`), not
    # extension-based, because this fleet has real Python files that keep a
    # `.sh` extension on purpose for oracle-parity naming (e.g.
    # `dev-sync.sh`, a `#!/usr/bin/env python3` script). Gating AST
    # mention-awareness on `filename.endswith(".py")` alone missed that
    # file's own module-level doc text entirely -- it was scanned with the
    # far weaker line-based rules instead of never having AST parsing
    # attempted on it, which is the actual bug this fixes. The "skip the
    # whole file if unparseable" fail-safe (`py_is_extension`) stays
    # extension-gated, though: a REAL `.sh`/`.bats`/`.yaml`/`.json` file is
    # expected to fail `ast.parse` (it isn't Python), and that must fall
    # through to the structured-data/line-based rules below, never to a
    # whole-file skip -- only a `.py`-extension file failing to parse is
    # the broken-source case that fail-safe exists for.
    is_py_extension = filename.endswith(".py")
    _dead_rung_in_scope = filename.endswith(
        _DEAD_RUNG_SCOPE_EXTENSIONS + _DEAD_RUNG_STRUCTURED_EXTENSIONS
    )
    _dead_rung_hinted = _dead_rung_in_scope and any(h in text for h in _DEAD_RUNG_HINTS)
    py_doc_lines: Optional[Set[int]] = None
    if _dead_rung_hinted:
        py_doc_lines = _py_documentary_lines(text)
    py_unparseable = is_py_extension and py_doc_lines is None

    structured_doc_lines: Set[int] = set()
    if _dead_rung_hinted and filename.endswith((".json", ".yaml", ".yml", ".toml")):
        structured_doc_lines = _structured_data_documentary_lines(text)

    # Classes 2/3 -- position-scoped content model, computed once per file
    # (never per-match) and only for files whose extension can even carry
    # either position. See the module docstring's "Position-scoped
    # exemptions" section. `class2_spans` stays `[]` and `class3_lines`
    # stays empty for every file outside these two extensions, so the
    # membership checks below are a no-op.
    #
    # Class 2 is CHARACTER-OFFSET-span-scoped, not line-scoped -- a
    # `.yaml`/`.yml` file's block-scalar (`|`/`>`) VALUE spans, computed
    # once via `_yaml_block_scalar_spans`. `.md` frontmatter is
    # deliberately NOT fed into class 2 at all (see the module docstring):
    # a frontmatter value is a YAML scalar like any other, and only a
    # block-scalar frontmatter value would ever qualify, which this
    # fleet's corpus doesn't use.
    lower_filename = filename.lower()
    class2_spans: List[Tuple[int, int]] = []
    class3_lines: Set[int] = set()
    line_offsets: List[int] = []
    if lower_filename.endswith((".yaml", ".yml")):
        class2_spans = _yaml_block_scalar_spans(text)
        if class2_spans:
            cum = 0
            for lw in text.splitlines(keepends=True):
                line_offsets.append(cum)
                cum += len(lw)
    elif lower_filename.endswith(".md"):
        doc_lines = text.splitlines()
        fm_range = _frontmatter_range(doc_lines)
        body_start = fm_range[1] + 2 if fm_range is not None else 1
        class3_lines = _markdown_code_block_lines(doc_lines, start=body_start)

    def _yaml_protected(line_start_offset: int, m_start: int, m_end: int) -> bool:
        if not class2_spans:
            return False
        abs_start = line_start_offset + m_start
        abs_end = line_start_offset + m_end
        return _inside_any_span(abs_start, abs_end, class2_spans)

    for lineno, line in enumerate(text.splitlines(), start=1):
        if _line_has_marker_with_reason(line):
            continue

        line_start_offset = line_offsets[lineno - 1] if line_offsets else 0

        # A match sitting inside a YAML block-scalar span (class 2) or a
        # markdown code block (class 3) is not a citation -- the
        # `abs-path-ok:` remedy is syntactically unusable at that position.
        # Scoped to exactly these four rules: `dead-registry-rung` keeps its
        # own, separately-scoped mention-awareness layer below, untouched.
        if lineno not in class3_lines:
            for m in _POSIX_HOME_RE.finditer(line):
                if _yaml_protected(line_start_offset, m.start(), m.end()):
                    continue
                if not _is_placeholder_segment(m.group(1)):
                    findings.append(Finding(filename, lineno, "posix-home", m.group(0)))

            for m in WIN_DRIVE_RE.finditer(line):
                token = _extract_token(line, m.start())
                root_len = m.end() - m.start()
                # A BARE drive root is deliberately NOT exempt. `/Users/` is
                # universal -- every macOS host has it -- which is why the posix
                # rule tests only the segment after it. `X:\` is one operator's
                # drive mapping and exists on no other machine, so prose like
                # "assets that don't belong on X:\" is itself the machine-specific
                # assertion this guard exists to catch. Root-concreteness and
                # segment-concreteness are different tests; the drive rule applies
                # both.
                if _is_win_drive_root_exempt(token, root_len) or _has_ellipsis_segment(
                    token, root_len
                ):
                    continue
                if _yaml_protected(line_start_offset, m.start(), m.start() + len(token)):
                    continue
                findings.append(Finding(filename, lineno, "drive-letter", token))

            for m in _UNC_RE.finditer(line):
                # Same segment test the other two rules apply. A UNC host is no
                # more "concrete by construction" than a drive letter was: the
                # canonical way to WRITE a UNC path in documentation uses a
                # placeholder hostname, so flagging that shape makes every correct
                # explanation of UNC syntax a finding -- including this comment,
                # which this guard refused to let be written until the marker
                # below was added.  abs-path-ok: naming the placeholder host forms
                # this branch exempts; they are the documentation shape, not a
                # machine
                host = m.group(0).lstrip("\\").split("\\")[0]
                if _is_placeholder_segment(host):
                    continue
                unc_token = _extract_token(line, m.start())
                if _yaml_protected(line_start_offset, m.start(), m.start() + len(unc_token)):
                    continue
                findings.append(Finding(filename, lineno, "unc", unc_token))

            for rx in _ANCHOR_RES:
                for m in rx.finditer(line):
                    token = _extract_token(line, m.start())
                    # Truncate at the first GATED escape pair before the both-
                    # separator test -- same JSON/Python string-escape false
                    # positive `WIN_DRIVE_RE` and `_UNC_RE` both defend against:
                    # a backslash-letter escape (`\n`, `\r`, ...) inside an
                    # escaped string supplies a bare backslash that, followed
                    # later on the same token by a `/`, phantom-matches as
                    # "mixes both separators" with no real path present
                    # (`X:/DoE-claude/.../SKILL.md\r\n` -- the trailing `\r\n`
                    # is the sole source of the backslash). The lookahead gate
                    # is load-bearing, not a simplification: an UNGATED cut
                    # (`\\[ntrbfv0]` alone) would also cut a REAL path at a real
                    # segment merely STARTING with an escape letter --
                    # `C:\test\project/sub\file` would truncate at `\t` in
                    # "test" and lose the genuine `/` further down the token,
                    # turning a real mixed-separator citation into a false
                    # negative. The gate (`(?![A-Za-z0-9_\-])`) requires the
                    # escape letter to be the WHOLE segment, exactly the
                    # discrimination `WIN_DRIVE_RE`/`_UNC_RE` already apply.
                    cut = _ESCAPE_CUT.search(token)
                    probe = token[: cut.start()] if cut else token
                    if "/" not in probe or "\\" not in probe:
                        continue
                    if _yaml_protected(line_start_offset, m.start(), m.start() + len(probe)):
                        continue
                    # No placeholder/well-known-root/ellipsis exemption here, for
                    # ANY of the three anchors -- matching the module docstring's
                    # invariant that mixed-separators fires "independent of
                    # whether its root segment is a placeholder". A token mixing
                    # `/` and `\` cannot be correct on any platform regardless of
                    # what its first segment says; a `WIN_DRIVE_RE` anchor used
                    # to silently reuse `drive-letter`'s exemption here, which
                    # contradicted this exact sentence for any placeholder-shaped
                    # first segment (`C:\test\project/sub\file` went undetected).
                    findings.append(Finding(filename, lineno, "mixed-separators", probe))

        if not _dead_rung_hinted:
            continue

        # `.py`-extension file with broken syntax: skip this rule for the
        # whole file (fail-safe -- no reliable documentary boundary in
        # unparseable source). Every other case falls through to the
        # unified line-based checks below, which apply regardless of
        # whether `ast.parse` succeeded for THIS file (a real `.sh`/`.yaml`/
        # `.json` file failing to parse as Python is the expected, common
        # case, not a fail-safe trigger).
        if py_unparseable:
            continue
        if py_doc_lines is not None and lineno in py_doc_lines:
            continue
        if lineno in structured_doc_lines:
            continue

        # Comment/echo/printf-line exclusion applies UNIFORMLY -- including
        # to `.py` files. This was a real hole: the AST-based check above
        # only ever covers bare string-literal statements, never `#`
        # comments (comments don't appear in the AST at all, so there was
        # nothing here to exclude them until this line-based check was
        # added). A `#`-led module-level comment explaining this rule's own
        # dead-rung shape is exactly the same "the guard refuses the text
        # that explains it" failure this rule exists to avoid.
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        first_token = stripped.split(None, 1)[0] if stripped else ""
        if first_token in ("echo", "printf"):
            continue
        scrubbed = _DEFAULT_EXPANSION_RE.sub("", line)
        backtick_spans = _spans(_BACKTICK_SPAN_RE, scrubbed)
        for rx in _DEAD_RUNG_RES:
            for m in rx.finditer(scrubbed):
                if _inside_any_span(m.start(), m.end(), backtick_spans):
                    continue
                findings.append(Finding(filename, lineno, "dead-registry-rung", m.group(0)))

    return findings


# ---------------------------------------------------------------------------
# Corpus scan -- one git spawn, cheap pre-filter, size cap, single open/file.
# ---------------------------------------------------------------------------

# Substrings that MUST appear somewhere in a file before it is worth regex
# work at all. Deliberately broad (a superset of what any individual rule
# needs) so the pre-filter can never under-match: `/Users/` and `/home/`
# cover posix-home; `:\\` and `:/` cover drive-letter (a superset -- most
# `:/` hits are URLs and get discarded by the real regex, but the pre-filter
# only needs to not throw away a real hit, cheap over-inclusion is fine
# here); `\\\\` covers unc; `claude/machine-local` and `claude/plugins`
# cover dead-registry-rung (neither of its two regexes can match without one
# of these substrings present).
_CHEAP_MARKERS = ("/Users/", "/home/", ":\\", ":/", "\\\\", "claude/machine-local", "claude/plugins")

# 2MB cap -- anything bigger in a doctrine/code tree is not prose or source
# this guard is meant to read, and scanning it line-by-line would dominate
# the whole corpus scan's runtime for zero expected signal.
_MAX_FILE_BYTES = 2 * 1024 * 1024


def _tracked_files(root: Path) -> Tuple[str, ...]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        **leaf_spawn_creationflags(),
        check=True,
    ).stdout
    return tuple(p for p in out.split("\0") if p)


def _maybe_relevant(text: str) -> bool:
    return any(marker in text for marker in _CHEAP_MARKERS)


# foreign-identity: OUT-OF-CLASS — docstring, not agent-facing rendered text
def scan_repo(root: Path) -> List[Finding]:
    """Scan every tracked file under `root` (any git repo -- this module is
    not DoE-claude-specific) and return all `Finding`s. Never raises on a
    per-file read failure; that file is simply skipped."""
    findings: List[Finding] = []
    for rel in _tracked_files(root):
        full = root / rel
        try:
            if full.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _maybe_relevant(text):
            continue
        findings.extend(detect_in_text(text, rel))
    return findings


def new_violations(before: str, after: str, filename: str = "") -> List[Finding]:
    """Multiset difference of `detect_in_text(after)` over
    `detect_in_text(before)`, keyed by `(rule, matched)` -- ignores line
    number (a write shifts line numbers even for untouched content). A
    citation that exists identically in `before` is never re-flagged; a
    write that introduces a NEW one, or duplicates an existing one an
    additional time, is. Used by the write-time guard leg so the corpus's
    pre-existing violations are never re-flagged on an unrelated edit to the
    same file.

    `filename` is optional and defaults to `""` (matches nothing) for
    backward compatibility -- pass the repo-relative target path so the
    evidence-artifact exemption (see `detect_in_text`) applies here too; a
    reviewer sidecar under `state/subagent-share/` quoting its own finding
    must not be denied by the very guard whose finding it's quoting.
    """
    before_counts = Counter(
        (f.rule, f.matched) for f in detect_in_text(before, filename)
    )
    seen: Counter = Counter()
    new: List[Finding] = []
    for f in detect_in_text(after, filename):
        key = (f.rule, f.matched)
        seen[key] += 1
        if seen[key] > before_counts.get(key, 0):
            new.append(f)
    return new
