"""Standing tripwire (2026-07-28): no module outside
``coordinator_core/frontmatter/`` may compile a GENERIC frontmatter-key
resolution regex whose value pad is ``\\s`` — because ``\\s`` matches a
NEWLINE, and a newline-crossing pad silently reads (or overwrites) the
FOLLOWING line whenever the key is present but empty.

The defect this gate exists to prevent recurring
=================================================
``^<key>:\\s*(.+)`` applied to whole-document text with ``re.MULTILINE`` does
not do what it reads like it does. On::

    blocking_notes:
    status: open

the ``\\s*`` walks past the line break and ``(.+)`` captures ``status: open``.
A reader mis-reports; a ``sub()`` driven by the same pattern OVERWRITES the
``status:`` line, destroying an unrelated field with no error anywhere. The
canonical fix is to pad with horizontal whitespace only (``[ \\t]*``) and to
bound the key with the lookahead ``(?=[ \\t]|\\r?$)`` — both are implemented,
tested and documented at length in
``coordinator_core.frontmatter.primitives`` (``read_fm_field``,
``replace_fm_field_raw``), which is the single canonical home for this pattern.

The class has now recurred at least eight times independently. Three copies were
root-fixed in ``primitives`` (01abd643 / fbf375a8 / ff30c16f — the last added
``replace_fm_field_raw`` specifically to remove the last legitimate reason to
fork the pattern); a later tree-wide sweep found two more live forks
(``ops/docgen/volatility.py`` ``redact_text``, ``ops/changelog_ops.py``
``_extract_field_fallback``); and a 2026-07-28 follow-up dispatch fixed the two
in ``coverage.py`` (``_parse_handoff_consumed_by``, ``_parse_handoff_deliverable_id``)
that this file's ``_KNOWN_UNFIXED`` used to carry — both now route through
``primitives.read_fm_field_unquoted``, so that exemption set is empty. Prose
alone has demonstrably not stopped the pattern from reappearing, which is why
this is a test.

Why the detector is scoped the way it is
========================================
A naive "flag ``:\\s*`` in a regex" gate matches ~76 string literals in this
repo, the overwhelming majority of them correct and unrelated (``^MemTotal:
\\s*(\\d+)`` against /proc/meminfo, ``^SCAN:\\s*(.*)$`` against a publish
log, per-line YAML scrapers that never see a newline at all). A gate that
loud gets disabled, which is strictly worse than no gate. Three conjunctive
narrowings bring it to a population this repo can actually hold at zero:

1. **The key must be RUNTIME-INTERPOLATED**, not a literal — an f-string
   ``{...}``, a ``str.format`` ``{field}`` template, a ``%s``, or a
   concatenated ``re.escape(key)``. That is precisely the shape of a *generic
   frontmatter-key resolver*: a function that resolves an arbitrary
   caller-supplied key, i.e. a fork of ``primitives.read_fm_field``'s job. All
   three known real instances of the defect have this shape. A one-off literal
   scraper for a single named field is a different (and far more numerous)
   thing; see § Known blind spots.
2. **The pad must be ``\\s``**, in the value position immediately after the
   key's colon. ``[ \\t]*`` — the canonical fix — is accepted, as is no pad.
   This is what keeps the gate off legitimate regexes that merely contain
   ``\\s*`` somewhere.
3. **The pattern must be used with ``re.MULTILINE``.** Without it ``^`` only
   matches at the subject's start, so the newline-crossing pad has almost no
   reach. More usefully in practice: the per-line appliers in this tree (the
   ``for line in content.splitlines(): prefix_re.match(line)`` shape, e.g.
   ``ops/completion_ops.py``'s ``_read_frontmatter_field`` and
   ``_read_frontmatter_list_field``) are STRUCTURALLY immune — a
   ``splitlines()`` element contains no newline for the pad to cross — and
   they uniformly omit MULTILINE. Requiring the flag keeps two provably-safe
   readers out of the allowlist rather than parking them there behind a
   "trust me, it's per-line" note.

Known blind spots (deliberate, not oversights)
==============================================
- **Literal-key patterns are not flagged** (narrowing (1)). This was the blind
  spot that hid ``coordinator_core/coverage.py``'s ``_parse_handoff_deliverable_id``
  (``^deliverable_id:\\s*…``) while its interpolated-key sibling in the same file
  was flagged — the false-assurance shape of a green gate over a broken file.
  Both are now fixed.

  **Measured 2026-07-28 — the 76-hit figure does NOT apply to this gate as it
  now stands, and a future agent should not decline widening on that number.**
  76 was measured against a NAIVE ``:\\s*``-anywhere grep, before narrowings
  (2) and (3) existed. Re-measured by relaxing narrowing (1) alone (literal
  keys admitted; ``^``-anchor, ``\\s``-in-value-position and ``re.MULTILINE``
  all still required), the population is **10** for any literal key and **8**
  restricted to snake_case keys — not a flood::

      hooks/nudge_unauthorized_handoff.py:53,56,59   ^kind:/^install_chain_order:
      ops/bootstrap_orchestrate.py:104               ^schema_version:\\s*(.*)$
      ops/completion_ops.py:1823                     ^Session-Id:\\s*(\\S+)
      ops/cruft_sweep.py:634                         ^predecessor:\\s*(<uuid>)
      ops/probe_onboarding_currency.py:96            ^schema_version:\\s*(.*)$
      reconcile/commit_reality.py:726                ^status:\\s*(\\S+)\\s*$
      session_attribution.py:338                     ^Session-Id:\\s*(\\S+)\\s*$
      subagent_sandbox/harvest_exit_interviews.py:51 ^agent_type:\\s*(?P<value>.+?)\\s*$

  Widening was NOT applied here, for a reason that is about sequencing rather
  than noise: several of those shapes look like live instances of this very
  defect (``\\s*(\\S+)`` skips the newline and takes the following line's first
  token), and each needs its own reproduction to separate a real defect from a
  provably-safe single-line subject. Flipping the matcher first would land a
  standing gate that is red across eight files on arrival — and the only way to
  green it without doing that work is eight unverified ``_KNOWN_UNFIXED``
  entries, i.e. a gate that ships pre-muted. Correct order: sweep and fix (or
  clear) the eight sites, THEN relax narrowing (1) permanently.

  Note that those sites could NOT have been parked in ``_KNOWN_UNFIXED`` in the
  meantime even as a stopgap: that set is self-invalidating via
  ``test_known_unfixed_entries_still_describe_a_real_violation``, which re-scans
  each named file with the CURRENT ``_FORKED_KEY_SHAPE``. A literal-key entry
  produces no match under narrowing (1) and would be reported STALE, failing
  that test. Hence this prose list — which is the dispatchable artifact — rather
  than an exemption set the gate would reject.
- **Dynamically-assembled patterns are not flagged.** Only literals,
  f-strings, ``+`` concatenations, ``%`` interpolations, ``.format()`` calls,
  and module-level string constants are rendered; a pattern built by a helper
  function or read from config is skipped.
- **Non-MULTILINE whole-document searches are not flagged** (narrowing (3)) —
  such a pattern can still mis-match if the key happens to be the subject's
  very first line.

False negatives are the accepted cost of zero false positives here. This gate
proves it has teeth via the planted-fixture self-tests at the bottom of this
file, not by passing against an empty tree.

Shape follows ``coordinator_core/frontmatter/tests/test_no_node_schema_shellout.py``
(AST-based so comments and docstrings are structurally excluded; explicit,
dated, self-invalidating exemptions rather than a widened matcher).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOT = _REPO_ROOT / "coordinator_core"

# The canonical home. `primitives.py` is where this pattern is SUPPOSED to
# live; gating it against itself would be incoherent. (It uses `[ \t]*` and
# would pass anyway — the exclusion is about ownership, not about the check.)
_CANONICAL_PACKAGE_PREFIX = "coordinator_core/frontmatter/"

# `re` module functions that take a pattern as their first positional argument.
_RE_PATTERN_CALLS = {
    "compile", "search", "match", "fullmatch", "sub", "subn",
    "finditer", "findall", "split",
}

# Flag names that mean "^ matches at every line start".
_MULTILINE_FLAG_NAMES = {"MULTILINE", "M"}

# Placeholder standing in for a runtime-interpolated span in a rendered
# pattern source. \x00 cannot occur in a Python string literal written in this
# repo's source, so it can never collide with real pattern text.
_VAR = "\x00"

# A frontmatter-key resolution regex with a newline-crossing value pad:
#   ^ , optional group-open / leading indent-tolerance pad,
#   an interpolated key, an optional short literal suffix on the key,
#   `:` , an optional boundary lookahead, then a `\s` pad.
_FORKED_KEY_SHAPE = re.compile(
    r"\^"                                # anchored at a line start
    r"[(]?"                              # optional capture-group open
    r"(?:\\s\*|\[ \\t\]\*)?"             # optional leading indent tolerance
    r"[(]?"
    + re.escape(_VAR) +                  # the interpolated key
    r"[^" + re.escape(_VAR) + r":\n]{0,20}"  # optional literal key suffix
    r":"                                 # the key/value separator
    r"(?:\(\?[^)]*\))?"                  # optional boundary lookahead
    r"\\s"                               # THE DEFECT: a newline-crossing pad
)

# Explicit, dated, narrowly-scoped exemptions. Each entry names the file, the
# symbol, why it is still here, and the fix path that retires the entry. This
# set shrinks; it does not grow by silently widening the matcher.
#
# EMPTY as of 2026-07-28. Its only entry — `coverage.py`'s
# `_parse_handoff_consumed_by` — was deleted when that function and its
# literal-key sibling `_parse_handoff_deliverable_id` were both routed through
# `primitives.read_fm_field_unquoted`. Adding an entry here is a last resort:
# `test_known_unfixed_entries_still_describe_a_real_violation` below re-scans
# the named file and fails if the entry no longer describes a live violation,
# so an entry cannot outlive its fix.
_KNOWN_UNFIXED: dict[tuple[str, int | None], str] = {}


def _relpath(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        # `root` is outside the repo (a tmp_path self-test fixture) — a
        # root-relative path simply never matches a real exemption key.
        return path.resolve().relative_to(root.resolve()).as_posix()


def _is_excluded_source_path(relpath: str, path: Path) -> bool:
    """Tests and the canonical package are out of scope.

    Test files are excluded on the same reasoning as the sibling node-shellout
    gate: this repo carries deliberate parity ORACLES that must mirror a
    foreign implementation warts-and-all (``coordinator_core/tests/
    _baton_dag_oracle.py`` reproduces the JS ``^%s:\\s*(.*)$`` verbatim on
    purpose — "fixing" it would destroy the parity signal it exists to
    provide). A gate that forced those oracles to diverge from their subject
    would be actively harmful.
    """
    if relpath.startswith(_CANONICAL_PACKAGE_PREFIX):
        return True
    if path.name.startswith("test_") or path.name.startswith("_"):
        return True
    return "tests" in path.parts


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "..."`` string assignments, so a pattern template
    held in a constant (``_FM_FIELD_RE_TMPL.format(field=...)``) is rendered at
    its compile site rather than skipped as opaque."""
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = node.value.value
    return consts


def _render_pattern(node: ast.expr, consts: dict[str, str], *, top: bool = True) -> str | None:
    """Render a regex-source expression to text, marking runtime-interpolated
    spans with ``_VAR``.

    Returns ``None`` when the expression is not a recognisable literal pattern
    at all (a bare Name that is not a known module constant, a helper call, a
    comprehension) — such patterns are skipped rather than guessed at. Nested
    operands of a recognised form ARE allowed to be opaque; an opaque operand
    inside a concatenation is exactly the interpolated-key span this gate looks
    for (``r"^" + re.escape(field) + r":\\s*(.+)"``).
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value
        return None if top else _VAR
    if isinstance(node, ast.Name):
        if node.id in consts:
            return consts[node.id]
        return None if top else _VAR
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append(piece.value)
            else:
                parts.append(_VAR)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _render_pattern(node.left, consts, top=False)
        right = _render_pattern(node.right, consts, top=False)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        left = _render_pattern(node.left, consts, top=False)
        if left is None:
            return None
        return re.sub(r"%[sr]", _VAR, left)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "format":
            template = _render_pattern(func.value, consts, top=False)
            if template is None:
                return None
            return re.sub(r"\{[^{}]*\}", _VAR, template)
        return None if top else _VAR
    return None if top else _VAR


def _mentions_multiline(node: ast.expr | None) -> bool:
    """True when a flags expression names ``re.MULTILINE``/``re.M`` (or the
    bare ``MULTILINE``/``M`` of a ``from re import`` style)."""
    if node is None:
        return False
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in _MULTILINE_FLAG_NAMES:
            return True
        if isinstance(sub, ast.Name) and sub.id in _MULTILINE_FLAG_NAMES:
            return True
    return False


def _call_flags_expr(call: ast.Call, pattern_arg_index: int) -> ast.expr | None:
    """The flags argument of an ``re.*`` call, keyword or positional.

    ``re.compile(p, flags)`` puts flags right after the pattern;
    ``re.search(p, s, flags)`` / ``re.sub(p, r, s, count, flags)`` put it
    later. Rather than model each signature, treat ANY remaining positional
    argument as a flags candidate — a false "has MULTILINE" reading is only
    reachable if some other positional argument literally names ``re.M``,
    which no signature in this module set does.
    """
    for kw in call.keywords:
        if kw.arg == "flags":
            return kw.value
    remaining = call.args[pattern_arg_index + 1:]
    if not remaining:
        return None
    # Wrap the remaining positionals so a single walk covers all of them.
    return ast.Tuple(elts=list(remaining), ctx=ast.Load())


def _re_call_pattern_arg(call: ast.Call) -> tuple[ast.expr, int] | None:
    """The pattern argument of an ``re.<fn>(...)`` call, with its index."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else None
    )
    if name not in _RE_PATTERN_CALLS:
        return None
    for kw in call.keywords:
        if kw.arg == "pattern":
            return kw.value, -1
    if not call.args:
        return None
    return call.args[0], 0


def find_forked_frontmatter_key_regexes(root: Path) -> list[tuple[str, int, str]]:
    """Walk ``root`` for .py files and return every
    ``(relpath, lineno, rendered-pattern)`` that compiles a generic
    frontmatter-key regex with a newline-crossing ``\\s`` value pad under
    ``re.MULTILINE``.

    Used both against the real ``coordinator_core/`` tree (the standing gate)
    and against isolated ``tmp_path`` fixtures (the gate's own self-tests,
    proving it detects rather than merely passing by absence).
    """
    violations: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        relpath = _relpath(path, root)
        if _is_excluded_source_path(relpath, path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        consts = _module_string_constants(tree)
        seen_lines: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            found = _re_call_pattern_arg(node)
            if found is None:
                continue
            pattern_expr, index = found
            rendered = _render_pattern(pattern_expr, consts)
            if rendered is None or not _FORKED_KEY_SHAPE.search(rendered):
                continue
            if not _mentions_multiline(_call_flags_expr(node, index)):
                continue
            if node.lineno in seen_lines:
                continue
            seen_lines.add(node.lineno)
            if (relpath, None) in _KNOWN_UNFIXED:
                continue
            if (relpath, node.lineno) in _KNOWN_UNFIXED:
                continue
            violations.append((relpath, node.lineno, rendered.replace(_VAR, "<KEY>")))
    return violations


def test_no_forked_frontmatter_key_regex_outside_canonical_module():
    """Standing gate: outside ``coordinator_core/frontmatter/``, no module may
    compile a generic ``^<key>:\\s*…`` frontmatter resolver under
    ``re.MULTILINE``. Route through ``frontmatter.primitives`` (``read_fm_field``
    / ``replace_fm_field_raw``), or — where the primitive genuinely does not fit
    — pad with ``[ \\t]*`` and say in a comment why the primitive was not used,
    as ``ops/changelog_ops.py`` does.
    """
    violations = find_forked_frontmatter_key_regexes(_SCAN_ROOT)
    assert violations == [], (
        "Found frontmatter-key regex fork(s) with a newline-crossing `\\s` pad "
        "outside coordinator_core/frontmatter/. `\\s` matches a NEWLINE, so on "
        "a present-but-empty key this reads (or overwrites) the FOLLOWING "
        f"line. Pad with `[ \\t]*` instead: {violations}"
    )


def test_gate_detects_a_reverted_read_side_pad(tmp_path):
    """Teeth, read side: the exact pre-fix shape of
    ``ops/changelog_ops.py``'s ``_extract_field_fallback``."""
    fixture = tmp_path / "fixture_read_fork.py"
    fixture.write_text(
        "import re\n"
        "\n"
        "def extract(field, content):\n"
        '    fm_re = re.compile(r"^" + re.escape(field) + r":\\s*(.+)",'
        " re.IGNORECASE | re.MULTILINE)\n"
        "    m = fm_re.search(content)\n"
        "    return m.group(1) if m else None\n",
        encoding="utf-8",
    )

    violations = find_forked_frontmatter_key_regexes(tmp_path)

    assert len(violations) == 1, violations
    relpath, lineno, rendered = violations[0]
    assert relpath == "fixture_read_fork.py"
    assert lineno == 4
    assert rendered == "^<KEY>:\\s*(.+)"


def test_gate_detects_a_reverted_write_side_pad(tmp_path):
    """Teeth, write side: the exact pre-fix shape of
    ``ops/docgen/volatility.py``'s ``redact_text`` — an f-string pattern whose
    LEADING ``\\s*`` (deliberate indent tolerance) must not mask the trailing
    one."""
    fixture = tmp_path / "fixture_write_fork.py"
    fixture.write_text(
        "import re\n"
        "\n"
        "def redact(text, field, token):\n"
        '    pattern = re.compile(rf"^(\\s*{re.escape(field)}:\\s*).*$",'
        " flags=re.MULTILINE)\n"
        "    return pattern.sub(lambda m: m.group(1) + token, text)\n",
        encoding="utf-8",
    )

    violations = find_forked_frontmatter_key_regexes(tmp_path)

    assert len(violations) == 1, violations
    assert violations[0][1] == 4
    assert violations[0][2] == "^(\\s*<KEY>:\\s*).*$"


def test_gate_detects_a_templated_constant_fork(tmp_path):
    """A pattern held in a module-level ``.format()`` template is rendered at
    its compile site, not skipped as opaque."""
    fixture = tmp_path / "fixture_template_fork.py"
    fixture.write_text(
        "import re\n"
        '\n_FM_FIELD_RE_TMPL = r"^{field}:\\s*(.+)"\n'
        "\n"
        "def extract(field, content):\n"
        "    return re.compile(_FM_FIELD_RE_TMPL.format(field=re.escape(field)),"
        " re.MULTILINE).search(content)\n",
        encoding="utf-8",
    )

    violations = find_forked_frontmatter_key_regexes(tmp_path)

    assert len(violations) == 1, violations
    assert violations[0][2] == "^<KEY>:\\s*(.+)"


def test_gate_accepts_the_canonical_horizontal_pad(tmp_path):
    """Negative control: the fixed form of both sites above, plus the
    canonical primitive's boundary-lookahead shape, must all pass."""
    fixture = tmp_path / "fixture_fixed.py"
    fixture.write_text(
        "import re\n"
        "\n"
        "def read(field, content):\n"
        '    a = re.compile(r"^" + re.escape(field) + r":[ \\t]*(.+)", re.MULTILINE)\n'
        '    b = re.compile(rf"^(\\s*{re.escape(field)}:[ \\t]*).*?(\\r?)$",'
        " flags=re.MULTILINE)\n"
        '    c = re.compile(r"^" + re.escape(field) + r":(?=[ \\t]|\\r?$)[ \\t]*(.*?)'
        '[ \\t]*\\r?$", re.MULTILINE)\n'
        "    return a, b, c\n",
        encoding="utf-8",
    )

    assert find_forked_frontmatter_key_regexes(tmp_path) == []


def test_gate_ignores_literal_key_and_non_frontmatter_regexes(tmp_path):
    """Negative control: the noise this gate must NOT flag — literal-key
    scrapers, regexes that merely contain ``\\s*`` elsewhere, and an
    interpolated-key pattern applied per-line WITHOUT ``re.MULTILINE`` (the
    structurally-immune ``splitlines()`` shape)."""
    fixture = tmp_path / "fixture_benign.py"
    fixture.write_text(
        "import re\n"
        "\n"
        '_MEM_RE = re.compile(r"^MemAvailable:\\s*(\\d+)", re.MULTILINE)\n'
        '_LOG_RE = re.compile(r"^\\s*FAIL:\\s*(.+?)\\s*$", re.MULTILINE)\n'
        '_RUN_RE = re.compile(r"\\(run:\\s*(?P<run_id>\\S+)\\)\\s*$")\n'
        "\n"
        "def read_per_line(content, key):\n"
        '    prefix_re = re.compile(rf"^{re.escape(key)}:\\s*(.*)$")\n'
        "    for line in content.splitlines():\n"
        "        m = prefix_re.match(line)\n"
        "        if m:\n"
        "            return m.group(1)\n"
        "    return None\n",
        encoding="utf-8",
    )

    assert find_forked_frontmatter_key_regexes(tmp_path) == []


def test_known_unfixed_entries_still_describe_a_real_violation():
    """Self-invalidating exemptions: an entry in ``_KNOWN_UNFIXED`` naming a
    file that no longer contains a violation is stale and must be deleted, so
    the set can only shrink. Without this, a fixed site would leave a
    permanent mute behind that silently re-excuses a future regression in the
    same file.
    """
    stale: list[str] = []
    for (relpath, lineno), reason in _KNOWN_UNFIXED.items():
        path = _REPO_ROOT / relpath
        if not path.exists():
            stale.append(f"{relpath} (file no longer exists)")
            continue
        # Re-scan just this file with the exemption set bypassed.
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        consts = _module_string_constants(tree)
        hits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            found = _re_call_pattern_arg(node)
            if found is None:
                continue
            rendered = _render_pattern(found[0], consts)
            if rendered is None or not _FORKED_KEY_SHAPE.search(rendered):
                continue
            if _mentions_multiline(_call_flags_expr(node, found[1])):
                hits.append(node.lineno)
        if not hits or (lineno is not None and lineno not in hits):
            stale.append(f"{relpath}:{lineno} (no matching violation found; reason was: {reason})")

    assert stale == [], (
        "Stale _KNOWN_UNFIXED exemption(s) — the named site no longer violates "
        f"the gate, so the entry must be deleted: {stale}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
