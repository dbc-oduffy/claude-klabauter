"""coordinator_core.ops.session._path_shape_regexes — shared path-shape
regex primitives for the guards in this package that need to recognize a
Windows drive-letter absolute path.

Single source for this one shape because it has a load-bearing subtlety that
must not drift out of sync between copies: the lookbehind. Without it, the
drive letter can be satisfied by the last letter of a URL scheme (`https://`
supplies `s` + `:` + `/`), so an ordinary https URL false-positives as a
foreign Windows path. `guard_foreign_platform_paths.py` and
`guard_concrete_path_citations.py` both need this exact shape and previously
each carried its own copy of the regex — this module is the fix for that
duplication, not a new capability.

Second load-bearing subtlety, added for the 2026-07-31 false-positive fix:
inside a JSON or Python string literal, a backslash-letter escape sequence
(newline/tab/CR/etc.) supplies a bare backslash immediately followed by a
single letter or digit -- so ordinary prose ending a sentence/word in a
letter-colon pair, followed by an escaped newline, phantom-matches as a
one-character drive path (a real fleet example: text ending "...as e:" then
a JSON-escaped newline then "    # e." reads as root "e:" + segment "n").
Reproduced against coordinator-claude's `state/cockpit-emission.json`.

Fix chosen: exclude a SINGLE-CHARACTER segment drawn from the escape-letter
alphabet these languages use for control characters, via a trailing
negative lookahead, rather than a blanket minimum-segment-length rule. A
minimum-length rule would also suppress a genuine one-character path
segment -- rare but not impossible, and indistinguishable from noise by
length alone. The escape-letter set is narrow, enumerated, and matches
exactly the shape a string-literal escape produces: one of those letters
immediately followed by a segment boundary (not by another word character,
which would make it a real segment merely STARTING with that letter -- a
segment like "temp" or "nginx" still fires normally, since the lookahead
only fires when the escape letter is the WHOLE segment).

Residual: a genuine real-world single-character path segment that happens
to BE one of these seven letters/digit (an actual directory named with a
single escape-alphabet character, not an escape sequence) stays unmatched.
Judged acceptable -- no real fleet path uses a bare single escape-alphabet
character as a full path segment, and the alternative (matching it) is the
false-positive class this fix exists to close.
"""
from __future__ import annotations

import re

# A real drive letter is never preceded by a word character; `https:` always
# is (the `s` before the `:` is itself preceded by the alnum `p`). The
# trailing negative lookahead excludes the string-escape false positive
# described in the module docstring above: a bare escape-letter/digit
# standing alone as the entire "segment" after the separator is not a real
# path component. `(?![A-Za-z0-9_\-])` after the candidate escape char is
# what discriminates "the whole segment is just this one escape char" from
# "this is a real segment that happens to START with one of these letters".
# The escape-letter set mirrors Python/JSON string-escape letters: n t r b
# f v, plus the digit 0 (null-byte escape).
WIN_DRIVE_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?![ntrbfv0](?![A-Za-z0-9_\-]))"
)
