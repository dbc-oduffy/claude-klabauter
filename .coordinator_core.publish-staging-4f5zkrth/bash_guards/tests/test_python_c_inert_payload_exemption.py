"""Adversarial corpus for the provably-inert Python ``-c`` payload
exemption in ``coordinator_core.bash_guards.block_subagent_commit`` (part 14
of that module's docstring).

The exemption suppresses ONE leg -- the string-literal argv reconstruction
(``_python_c_payload_argv_text``) -- for a payload
``_python_c_payload_is_provably_inert`` can PROVE carries no execution sink.
Its whole safety argument is the fail-closed direction, so this module is
weighted accordingly, and it pins that direction at TWO layers, because the
end-to-end verdict alone cannot carry it:

1. ``_python_c_payload_is_provably_inert`` must REFUSE every payload in the
   sink corpus below (the layer part 14 actually changed).
2. The guard's end-to-end verdict for every adversarial command must be
   exactly what it was before part 14 (the layer that must not move).

Layer 2 is expressed as a measured ``expect_deny`` per command rather than a
blanket "all deny", because the two parts this module covers move that
column in OPPOSITE directions and both movements have to stay pinned.

Part 15 (the argv0-position bypass) is covered here too, in the same
corpus, because the two changes are a designed pair and splitting their
corpora would let one be edited without re-measuring the other. Its defect:
``_python_c_payload_argv_text`` rebuilds a space-joined line from the
payload's string literals IN APPEARANCE ORDER, and the original three
matchers resolve binary identity at ARGV0/segment-head -- so a sink whose
own literals come first (``getattr(__import__('os'),'system')('<helper>')``
reconstructs to ``os system <helper>``) pushed the helper out of argv0 and
ALLOWED a real commit. ``_has_reconstructed_commit_identity`` matches at any
token position of that synthetic line, and the rows this moved are marked
at their definition below and pinned as a measured set by
``test_part15_corpus_moves_exactly_the_measured_set``.

Pure Python -- no shell spawns, no filesystem writes (Windows+macOS
first-class). Identity resolution is monkeypatched through the sibling
module's own seam helpers, so no git repo or back-pointer chain on disk is
required.

Spec backlink: coordinator_core/bash_guards/block_subagent_commit.py
  (module docstring, "2026-08-04 update, part 14" and "part 15").
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.bash_guards.tests import test_block_subagent_commit as base

HELPER = "coordinator/bin/scoped-git-commit"

# ---------------------------------------------------------------------------
# Layer 1 -- payload sources the checker must refuse. Raw Python source, no
# shell quoting in the way: this is the exact string the exemption decides
# on, so a false clear here is the bypass this whole change risks.
# ---------------------------------------------------------------------------

_SINK_PAYLOADS = [
    ("subprocess.run", "import subprocess; subprocess.run(['%s','-m','x'])" % HELPER),
    ("subprocess.Popen", "import subprocess; subprocess.Popen(['%s'])" % HELPER),
    (
        "subprocess.check_output",
        "import subprocess; subprocess.check_output(['%s'])" % HELPER,
    ),
    ("subprocess.call", "import subprocess; subprocess.call(['%s'])" % HELPER),
    ("os.system", "import os; os.system('%s -m x')" % HELPER),
    ("os.popen", "import os; os.popen('%s -m x').read()" % HELPER),
    ("os.execv", "import os; os.execv('/bin/sh', ['sh','-c','%s'])" % HELPER),
    ("os.fork", "import os; os.fork() or os.system('%s')" % HELPER),
    ("os.spawnl", "import os; os.spawnl(os.P_WAIT, '%s')" % HELPER),
    ("dunder-import", "__import__('os').system('%s -m x')" % HELPER),
    (
        "getattr-over-dunder-import",
        "getattr(__import__('os'),'system')('%s -m x')" % HELPER,
    ),
    ("eval", "eval(\"__import__('os').system('%s')\")" % HELPER),
    ("exec", "exec(\"import os; os.system('%s')\")" % HELPER),
    ("compile", "exec(compile(\"import os; os.system('%s')\", '<s>', 'exec'))" % HELPER),
    (
        "importlib-imported",
        "import importlib; importlib.import_module('subprocess').run(['%s'])" % HELPER,
    ),
    (
        "importlib-ambient",
        "importlib.import_module('subprocess').run(['%s'])" % HELPER,
    ),
    (
        "subclasses-reflection",
        "[c for c in ().__class__.__bases__[0].__subclasses__() "
        "if c.__name__ == 'Popen'][0](['%s'])" % HELPER,
    ),
    ("dunder-globals", "print([].__class__.__globals__)"),
    ("dunder-builtins", "print(''.__class__.__builtins__)"),
    # Found by adversarially reviewing the checker, not by the brief: in a
    # `-c` main module `__builtins__` IS the builtins module, and `eval` at
    # ATTRIBUTE position is not the `eval` NAME the builtin set refuses.
    # Both spellings below cleared as inert until `_identifier_is_dunder`
    # made any dunder -- at name OR attribute position -- a refusal, and
    # `_attribute_name_is_non_inert` started consulting the builtin set too.
    ("dunder-builtins-eval", "__builtins__.eval(\"__import__('os').system('%s')\")" % HELPER),
    ("dunder-builtins-open-write", "__builtins__.open('%s','w').write('x')" % HELPER),
    (
        "dunder-builtins-getattr",
        "__builtins__.getattr(__builtins__, 'eval')('1')",
    ),
    ("dunder-loader", "__loader__.load_module('os').system('%s')" % HELPER),
    ("builtin-name-at-attribute-position", "m.eval('%s')" % HELPER),
    ("f-string-assembled", "import os; h = '%s'; os.system(f'{h} -m x')" % HELPER),
    ("percent-assembled", "import os; os.system('%%s -m x' %% '%s')" % HELPER),
    ("format-assembled", "import os; os.system('{} -m x'.format('%s'))" % HELPER),
    ("join-assembled", "import os; os.system(' '.join(['%s','-m','x']))" % HELPER),
    ("split-module-name", "__import__('sub' + 'process').run(['%s'])" % HELPER),
    ("binop-in-call-func-position", "('a' + 'b')(open('%s').read())" % HELPER),
    ("fstring-in-call-func-position", "f'ab'(open('%s').read())" % HELPER),
    ("subscript-in-call-func-position", "d = {}\nd['k'](open('%s').read())" % HELPER),
    ("pickle.loads", "import pickle; pickle.loads(open('%s','rb').read())" % HELPER),
    ("pickle-ambient", "pickle.loads(open('%s','rb').read())" % HELPER),
    ("runpy.run_path", "import runpy; runpy.run_path('%s')" % HELPER),
    ("pty.spawn", "import pty; pty.spawn(['%s'])" % HELPER),
    ("ctypes", "import ctypes; ctypes.CDLL(None).system('%s')" % HELPER),
    ("shutil", "import shutil; shutil.rmtree('%s')" % HELPER),
    ("pathlib-write", "import pathlib; pathlib.Path('%s').write_text('x')" % HELPER),
    ("tempfile", "import tempfile; print(tempfile.mkdtemp())"),
    ("unparseable-unterminated-string", "import ast; ast.parse('%s" % HELPER),
    ("unparseable-unbalanced-paren", "import ast; ast.parse('%s'" % HELPER),
    ("open-write-mode", "open('%s','w').write('x')" % HELPER),
    ("open-append-mode", "open('%s','a').write('x')" % HELPER),
    ("open-exclusive-mode", "open('%s','x').write('y')" % HELPER),
    ("open-read-plus-mode", "open('%s','r+').write('x')" % HELPER),
    ("open-mode-kwarg-write", "open('%s', mode='a').write('x')" % HELPER),
    ("open-nonliteral-mode", "m = 'w'; open('%s', m).write('x')" % HELPER),
    ("open-computed-mode", "open('%s', 'r' + '+').write('x')" % HELPER),
    ("open-star-args", "a = ('%s','w'); open(*a).write('x')" % HELPER),
    ("open-kwargs-splat", "k = {'mode':'w'}; open('%s', **k).write('x')" % HELPER),
    ("bare-open-reference", "f = open; f('%s','w')" % HELPER),
    ("nested-python-c-source", "import os; os.system('python3 -c \"x\"')"),
    # --- PART 21 (2026-08-05): ``open`` AT ATTRIBUTE POSITION. The mode check
    #     was reached only from the ``Name`` leg, so any receiver at all --
    #     ``codecs`` is reachable as ``from json import codecs as c`` -- cleared
    #     a WRITE under an inert certificate, which is precisely the mutation
    #     `_INERT_SAFE_CALLABLE_NAMES`' soundness argument says cannot happen.
    ("attr-open-write-mode", "from json import codecs as c; c.open('%s','w')" % HELPER),
    (
        "attr-open-append-kwarg",
        "from json import codecs as c; c.open('%s', mode='a')" % HELPER,
    ),
    (
        "attr-open-json-dump",
        "import json\nfrom json import codecs as c\njson.dump('x', c.open('%s','w'))" % HELPER,
    ),
    ("attr-open-computed-mode", "from json import codecs as c; m='w'; c.open('%s', m)" % HELPER),
    ("attr-open-star-args", "from json import codecs as c; a=('%s','w'); c.open(*a)" % HELPER),
    ("bare-attr-open-reference", "from json import codecs as c; f = c.open; print(f)"),
    # --- PART 21: THE CALLABLE-ALLOWLIST CLOSURE, which the docstring claimed
    #     and the code did not enforce. Each of these certified INERT: an
    #     unlisted callable was bound to an allowlisted SPELLING and then
    #     invoked through it, or handed to an allowlisted higher-order callable.
    ("rebind-builtin-to-allowlisted-name", "sorted = type; C = sorted('C', (), {})"),
    (
        "import-launders-into-allowlisted-name",
        "from ast import literal_eval as get; print(get('1+1'))",
    ),
    (
        "assignment-launders-an-import",
        "from ast import literal_eval as z; get = z; print(get('1+1'))",
    ),
    (
        "import-bound-name-as-callback",
        "from ast import literal_eval as z; print(sorted(['1+1'], key=z))",
    ),
    ("module-alias-into-allowlisted-name", "import json as sorted; print(sorted.dumps({}))"),
    ("from-import-binds-a-refused-module", "from ast import sys as z; print(z)"),
    ("ambient-builtin-read", "print(type)"),
    ("unbound-free-name-read", "print(frobnicate)"),
]

#: Refused for a reason no sink corpus entry above isolates on its own --
#: the AST-shape allowlist itself. Each is an unrecognised construct, not a
#: known-dangerous one, which is the direction that has to fail closed.
_UNRECOGNISED_PAYLOADS = [
    ("unlisted-import-root", "import os"),
    ("unlisted-submodule-root", "import os.path"),
    ("unlisted-third-party-root", "import xml.etree.ElementTree"),
    ("from-import-unlisted", "from subprocess import run"),
    ("relative-import", "from . import x"),
    ("star-import-unlisted", "from os import *"),
    ("from-import-dangerous-name", "from builtins import eval"),
    ("bare-eval-reference", "f = eval"),
    ("bare-getattr-reference", "g = getattr"),
    ("bare-setattr-reference", "s = setattr"),
    ("bare-delattr-reference", "d = delattr"),
    ("bare-vars-reference", "v = vars"),
    ("bare-globals-reference", "g = globals"),
    ("bare-locals-reference", "l = locals"),
    ("bare-breakpoint-reference", "b = breakpoint"),
    ("bare-input-reference", "i = input"),
    ("bare-memoryview-reference", "m = memoryview"),
    ("bare-super-reference", "s = super"),
    ("bare-module-name-reference", "x = sys"),
    ("lambda", "f = lambda: 1"),
    ("function-def", "def f():\n    return 1\n"),
    ("class-def", "class C:\n    pass\n"),
    ("decorator", "@d\ndef f():\n    pass\n"),
    ("walrus", "(y := 1)"),
    ("del-statement", "x = 1\ndel x"),
    ("global-statement", "def f():\n    global x\n"),
    ("async-construct", "async def f():\n    await g()\n"),
    ("yield-construct", "def f():\n    yield 1\n"),
    ("raise-statement", "raise SystemExit(1)"),
    ("match-statement", "match 1:\n    case 1:\n        pass\n"),
    # --- PART 22: `str.format`'s field grammar is a SECOND attribute-chaining
    #     mini-language, living inside a plain `ast.Constant` where the walk's
    #     dunder check (which only reads real Name/Attribute identifiers) could
    #     not see it. Reflection-only -- the field grammar has no call operator,
    #     so nothing here ever SPAWNS -- but the certifier's stated invariant is
    #     that a dunder is refused at name and attribute position, and these
    #     reached `__class__`/`__globals__` with that invariant left standing.
    ("format-field-dunder-attribute", "'{0.__class__}'.format(())"),
    ("format-field-dunder-chain", "'{0.__class__.__bases__[0]}'.format(())"),
    (
        "format-field-dunder-subclasses",
        "'{0.__class__.__bases__[0].__subclasses__}'.format(())",
    ),
    ("format-field-dunder-index", "'{0[__globals__]}'.format({})"),
    (
        "format-field-dunder-off-open-handle",
        "h = open('%s')\nprint('{0.__class__.__init__.__globals__[os]}'.format(h))\n" % HELPER,
    ),
    ("format-field-dunder-via-bound-template", "t = '{0.__class__}'\nprint(t.format(()))\n"),
    ("format-field-dunder-nested-spec", "'{0:{1}}'.format('{0.__class__}', 5)"),
    # A template ASSEMBLED from fragments defeats a literal-only field check,
    # so a computed `.format` receiver is refused outright.
    ("format-receiver-assembled", "('{0.__' + 'class__}').format(())"),
    ("format-receiver-computed-call", "''.join(['{0.', '__class__}']).format(())"),
]

#: The narrow set the exemption exists for: no import at all, or an import
#: from `_INERT_PAYLOAD_IMPORT_ROOTS`, reading a file and manipulating text.
_INERT_PAYLOADS = [
    ("empty", ""),
    ("literal-only", "'%s'" % HELPER),
    ("listed-imports", "import ast, json, difflib"),
    ("listed-from-import", "from collections import Counter"),
    ("listed-submodule-import", "import collections.abc"),
    ("ast-parse-over-read", "import ast; ast.parse(open('%s').read())" % HELPER),
    ("json-loads-over-read", "import json; json.loads(open('%s').read())" % HELPER),
    ("open-no-mode", "print(open('%s').read())" % HELPER),
    ("open-r-mode", "print(open('%s','r').read())" % HELPER),
    ("open-mode-kwarg-r", "print(open('%s', mode='r').read())" % HELPER),
    ("open-rb-mode", "print(len(open('%s','rb').read()))" % HELPER),
    (
        "with-open",
        "with open('%s') as f:\n    print(len(f.read()))\n" % HELPER,
    ),
    ("comprehension-over-read", "print([l for l in open('%s').read().split()])" % HELPER),
    (
        "try-except-around-read",
        "try:\n    open('%s').read()\nexcept Exception:\n    pass\n" % HELPER,
    ),
    ("fstring-not-in-func-position", "p = '%s'\nprint(f'{p} scanned')" % HELPER),
    (
        "hashlib-over-read",
        "import hashlib; print(hashlib.sha256(open('%s','rb').read()).hexdigest())"
        % HELPER,
    ),
    # --- PART 21: the shapes the closure fix must NOT cost. Each names
    #     something outside `_INERT_SAFE_CALLABLE_NAMES` in a position where
    #     it can never be applied: an ``except`` type (compared, never called),
    #     an import bound but unused, a local variable holding a value.
    ("except-type-name", "try:\n    open('%s').read()\nexcept ValueError:\n    pass\n" % HELPER),
    (
        "except-type-tuple",
        "try:\n    open('%s').read()\nexcept (OSError, ValueError) as e:\n    print(e)\n"
        % HELPER,
    ),
    ("read-open-at-attribute-position", "from json import codecs as c; print(c.open('%s').read())" % HELPER),
    ("for-loop-target", "for line in open('%s').read().splitlines():\n    print(line)\n" % HELPER),
    ("stored-value-reread", "t = open('%s').read()\nprint(len(t), t.split())" % HELPER),
    # --- PART 22: the ordinary text formatting the field-syntax fix must not
    #     cost. None of these names an attribute chain in a field, which is the
    #     whole of what the new check refuses.
    ("format-empty-field", "print('{}'.format(1))"),
    ("format-indexed-field", "print('{0} {1}'.format(1, 2))"),
    ("format-named-field", "print('{name}'.format(name='x'))"),
    ("format-spec-alignment", "print('{0:>10}'.format('x'))"),
    ("format-nested-spec", "print('{0:{1}}'.format('x', 5))"),
    ("format-bound-template", "t = '{} {}'\nprint(t.format(1, 2))"),
    ("format-builtin-two-arg", "print(format(3.14159, '.2f'))"),
    ("format-over-read", "print('{}'.format(open('%s').read()[:20]))" % HELPER),
]


@pytest.mark.parametrize(
    "label,payload", _SINK_PAYLOADS, ids=[c[0] for c in _SINK_PAYLOADS]
)
def test_checker_refuses_every_sink_payload(label, payload):
    """The fail-closed direction, case by case: a payload carrying an
    execution sink -- or one this checker cannot parse at all -- is never
    cleared as inert, so the reconstruction leg still runs against it.
    """
    assert not guard._python_c_payload_is_provably_inert(payload)


@pytest.mark.parametrize(
    "label,payload", _UNRECOGNISED_PAYLOADS, ids=[c[0] for c in _UNRECOGNISED_PAYLOADS]
)
def test_checker_refuses_unrecognised_construct(label, payload):
    """Allowlist, not denylist: a construct the walk does not explicitly
    clear is refused even when nothing about it is known-dangerous.
    """
    assert not guard._python_c_payload_is_provably_inert(payload)


@pytest.mark.parametrize(
    "label,payload", _INERT_PAYLOADS, ids=[c[0] for c in _INERT_PAYLOADS]
)
def test_checker_clears_sinkless_read_only_payload(label, payload):
    assert guard._python_c_payload_is_provably_inert(payload)


# ---------------------------------------------------------------------------
# Layer 2 -- end-to-end verdicts. ``expect_deny`` is the MEASURED pre-part-14
# verdict for each command, so this matrix is a parity pin, not a wish list:
# a False row means the reconstruction never reached that shape in the first
# place (see this module's docstring for the argv0-position residual), and
# part 14 must not change that either.
# ---------------------------------------------------------------------------

_ADVERSARIAL_COMMANDS = [
    (
        "subprocess.run",
        "python3 -c \"import subprocess; subprocess.run(['%s','-m','x'])\"" % HELPER,
        True,
    ),
    (
        "subprocess.Popen",
        "python3 -c \"import subprocess; subprocess.Popen(['%s','-m','x'])\"" % HELPER,
        True,
    ),
    (
        "subprocess.check_output",
        "python3 -c \"import subprocess; subprocess.check_output(['%s'])\"" % HELPER,
        True,
    ),
    ("os.system", "python3 -c \"import os; os.system('%s -m x')\"" % HELPER, True),
    ("os.popen", "python3 -c \"import os; os.popen('%s -m x').read()\"" % HELPER, True),
    (
        "os.execv",
        "python3 -c \"import os; os.execv('/bin/sh', ['sh','-c','%s -m x'])\"" % HELPER,
        True,
    ),
    ("os.fork", "python3 -c \"import os; os.fork() or os.system('%s')\"" % HELPER, True),
    ("f-string-assembled", "python3 -c \"import os; h='%s'; os.system(f'{h}')\"" % HELPER, True),
    (
        "join-assembled",
        "python3 -c \"import os; os.system(' '.join(['%s','-m','x']))\"" % HELPER,
        True,
    ),
    ("pickle.loads", "python3 -c \"import pickle; pickle.loads(open('%s','rb').read())\"" % HELPER, True),
    ("runpy.run_path", "python3 -c \"import runpy; runpy.run_path('%s')\"" % HELPER, True),
    ("pty.spawn", "python3 -c \"import pty; pty.spawn(['%s','-m','x'])\"" % HELPER, True),
    ("ctypes", "python3 -c \"import ctypes; ctypes.CDLL(None).system('%s -m x')\"" % HELPER, True),
    ("unparseable-payload", "python3 -c \"import ast; ast.parse('%s'\"" % HELPER, True),
    ("open-write-mode", "python3 -c \"open('%s','w').write('x')\"" % HELPER, True),
    ("open-append-mode-kwarg", "python3 -c \"open('%s', mode='a').write('x')\"" % HELPER, True),
    ("open-read-plus-mode", "python3 -c \"open('%s','r+').write('x')\"" % HELPER, True),
    (
        "python-c-inside-sh-c",
        "sh -c \"python3 -c \\\"import subprocess; subprocess.run(['%s'])\\\"\"" % HELPER,
        True,
    ),
    (
        "python-c-inside-sh-c-inside-python-c",
        "python3 -c \"import os; os.system(\\\"sh -c 'python3 -c \\\\\\\"import "
        "subprocess; subprocess.run([\\\\\\\\\\\"%s\\\\\\\\\\\"])\\\\\\\"'\\\")\""
        % HELPER,
        True,
    ),
    (
        "plain-git-commit-in-payload",
        "python3 -c \"import os; os.system('git commit -m x')\"",
        True,
    ),
    (
        "invoke-op-in-payload",
        "python3 -c \"import subprocess; subprocess.run(['python3','-m',"
        "'coordinator_core.invoke','ceremony.scoped_git_commit','{}'])\"",
        True,
    ),
    # --- Rows below allowed before part 15 and DENY as of it: the
    #     reconstruction puts the sink's own literals ahead of the helper, so
    #     the helper never landed at argv0 and no argv0-anchored matcher ever
    #     saw it. `_has_reconstructed_commit_identity` resolves identity at
    #     ANY token position of that synthetic line, which is what moves them.
    #     The flip is measured, not assumed -- see
    #     `test_part15_corpus_moves_exactly_the_measured_set` below, which
    #     forces the part-15 matcher off and pins the exact moving set.
    ("dunder-import", "python3 -c \"__import__('os').system('%s -m x')\"" % HELPER, True),
    (
        "getattr-over-dunder-import",
        "python3 -c \"getattr(__import__('os'),'system')('%s -m x')\"" % HELPER,
        True,
    ),
    ("eval", "python3 -c \"eval(\\\"__import__('os').system('%s')\\\")\"" % HELPER, True),
    ("exec", "python3 -c \"exec(\\\"import os; os.system('%s')\\\")\"" % HELPER, True),
    (
        "compile",
        "python3 -c \"exec(compile(\\\"import os; os.system('%s')\\\",'<s>','exec'))\""
        % HELPER,
        True,
    ),
    (
        "importlib-imported",
        "python3 -c \"import importlib; importlib.import_module('subprocess')"
        ".run(['%s'])\"" % HELPER,
        True,
    ),
    (
        "importlib-ambient",
        "python3 -c \"importlib.import_module('subprocess').run(['%s'])\"" % HELPER,
        True,
    ),
    (
        "subclasses-reflection",
        "python3 -c \"[c for c in ().__class__.__bases__[0].__subclasses__() "
        "if c.__name__ == 'Popen'][0](['%s'])\"" % HELPER,
        True,
    ),
    ("percent-assembled", "python3 -c \"import os; os.system('%%s' %% '%s')\"" % HELPER, True),
    (
        "format-assembled",
        "python3 -c \"import os; os.system('{} -m x'.format('%s'))\"" % HELPER,
        True,
    ),
    (
        "split-module-name",
        "python3 -c \"__import__('sub'+'process').run(['%s'])\"" % HELPER,
        True,
    ),
    (
        "binop-in-call-func-position",
        "python3 -c \"('a'+'b')(open('%s').read())\"" % HELPER,
        True,
    ),
    (
        "open-nonliteral-mode",
        "python3 -c \"m = 'w'; open('%s', m).write('x')\"" % HELPER,
        True,
    ),
]

#: Commands that DENIED before part 14 and must now ALLOW -- the entire
#: intended blast radius of this change, enumerated so a reviewer can see it
#: is exactly these shapes and nothing else.
_NEWLY_ALLOWED_COMMANDS = [
    ("ast.parse-over-open-read", "python3 -c \"import ast; ast.parse(open('%s').read())\"" % HELPER),
    ("bare-open-read-no-import", "python3 -c \"open('%s').read()\"" % HELPER),
    (
        "json.loads-over-open-read",
        "python3 -c \"import json; json.loads(open('%s').read())\"" % HELPER,
    ),
    ("open-explicit-r-mode", "python3 -c \"print(open('%s','r').read())\"" % HELPER),
    ("open-mode-kwarg-r", "python3 -c \"print(open('%s', mode='r').read())\"" % HELPER),
    ("open-rb-mode", "python3 -c \"print(len(open('%s','rb').read()))\"" % HELPER),
    (
        "with-open-read",
        "python3 -c \"\nwith open('%s') as f:\n    print(len(f.read()))\n\"" % HELPER,
    ),
    (
        "difflib-over-read",
        "python3 -c \"import difflib; print(list(difflib.unified_diff("
        "open('%s').read().splitlines(), [])))\"" % HELPER,
    ),
    (
        "hashlib-over-read",
        "python3 -c \"import hashlib; print(hashlib.sha256("
        "open('%s','rb').read()).hexdigest())\"" % HELPER,
    ),
]


@pytest.mark.parametrize(
    "label,cmd,expect_deny",
    _ADVERSARIAL_COMMANDS,
    ids=[c[0] for c in _ADVERSARIAL_COMMANDS],
)
def test_adversarial_command_verdict_is_unmoved(monkeypatch, label, cmd, expect_deny):
    """Layer 2: every adversarial command keeps the verdict it had before
    part 14. Measured, not assumed -- see this module's docstring.
    """
    if expect_deny:
        base._denies(monkeypatch, cmd)
    else:
        base._allows(monkeypatch, cmd)


@pytest.mark.parametrize(
    "label,cmd",
    _NEWLY_ALLOWED_COMMANDS,
    ids=[c[0] for c in _NEWLY_ALLOWED_COMMANDS],
)
def test_sinkless_read_only_command_now_allows(monkeypatch, label, cmd):
    """The false positive part 14 closes: the helper path named as DATA in a
    payload with no execution sink is a read, not a commit attempt.
    """
    base._allows(monkeypatch, cmd)


@pytest.mark.parametrize(
    "label,cmd,_expect_deny",
    _ADVERSARIAL_COMMANDS,
    ids=[c[0] for c in _ADVERSARIAL_COMMANDS],
)
def test_adversarial_verdict_is_identical_with_the_exemption_disabled(
    monkeypatch, label, cmd, _expect_deny
):
    """Blast-radius pin, and the strongest form of the parity claim: with
    the inert check forced to ``False`` -- byte-for-byte the pre-part-14
    behaviour, since that predicate has exactly one call site and it gates
    exactly one leg -- every adversarial command reaches the SAME verdict it
    reaches with the exemption live. Only the sinkless read-only corpus
    (``_NEWLY_ALLOWED_COMMANDS``) may differ, and it is enumerated there.
    """
    base._subagent(monkeypatch)
    live = guard.check(base._payload(cmd, agent_type=base._SUBAGENT_TYPE)) is not None
    monkeypatch.setattr(
        guard, "_python_c_payload_is_provably_inert", lambda payload: False
    )
    disabled = guard.check(base._payload(cmd, agent_type=base._SUBAGENT_TYPE)) is not None
    assert live == disabled, cmd


@pytest.mark.parametrize(
    "label,cmd", _NEWLY_ALLOWED_COMMANDS, ids=[c[0] for c in _NEWLY_ALLOWED_COMMANDS]
)
def test_newly_allowed_command_denied_before_the_exemption(monkeypatch, label, cmd):
    """The other half of the blast-radius pin: each newly-allowed command
    genuinely DID deny before, so this corpus is the change's real effect
    and not a list of commands that always allowed.
    """
    base._subagent(monkeypatch)
    monkeypatch.setattr(
        guard, "_python_c_payload_is_provably_inert", lambda payload: False
    )
    assert guard.check(base._payload(cmd, agent_type=base._SUBAGENT_TYPE)) is not None


# ---------------------------------------------------------------------------
# Scope of the exemption -- it suppresses ONE leg, for Python payloads only.
# ---------------------------------------------------------------------------


def test_exemption_suppresses_only_the_reconstruction_leg():
    """An inert payload still yields the payload text itself -- only the
    synthetic argv line rebuilt from its string literals is withheld.
    """
    cmd = "python3 -c \"import ast; ast.parse(open('%s').read())\"" % HELPER
    legs = list(guard._wrapped_shell_c_payload_legs(cmd))
    assert legs == [("import ast; ast.parse(open('%s').read())" % HELPER, "")]


def test_non_inert_payload_still_yields_the_reconstruction_leg():
    """The other side of the same seam: a payload with a sink reaches the
    reconstruction exactly as it did before, tagged as before.
    """
    cmd = "python3 -c \"import subprocess; subprocess.run(['%s','-m','x'])\"" % HELPER
    legs = dict(guard._wrapped_shell_c_payload_legs(cmd))
    assert legs["%s -m x" % HELPER] == guard._PAYLOAD_LEG_PYTHON_STRING_LITERALS


def test_shell_c_payload_is_untouched_by_the_exemption():
    """A shell ``-c`` payload never reaches the inert check -- it is not
    Python source, and its own unwrap leg is unchanged.
    """
    assert list(guard._wrapped_shell_c_payload_legs("sh -c 'git commit -m x'")) == [
        ("git commit -m x", "")
    ]


def test_inert_python_payload_still_denies_a_real_commit_in_the_outer_command(
    monkeypatch,
):
    """The exemption is payload-scoped, not command-scoped: an inert Python
    payload chained alongside a real commit denies on the outer command.
    """
    base._denies(
        monkeypatch,
        "python3 -c \"import ast; ast.parse(open('%s').read())\" && git commit -m x"
        % HELPER,
    )


def test_inert_python_payload_still_denies_a_helper_in_the_outer_command(monkeypatch):
    """Same property against the helper matcher rather than plain git."""
    base._denies(
        monkeypatch,
        "python3 -c \"open('%s').read()\"; scoped-git-commit -m x -- src/foo.py"
        % HELPER,
    )


def test_non_python_commands_keep_their_verdicts(monkeypatch):
    """Reuse of the sibling module's verdict-parity matrix: no command
    outside the Python ``-c`` family moves.
    """
    for cmd, expect_deny in base._VERDICT_PARITY_MATRIX:
        if "python" in cmd and "-c " in cmd:
            continue
        if expect_deny:
            base._denies(monkeypatch, cmd)
        else:
            base._allows(monkeypatch, cmd)


# ---------------------------------------------------------------------------
# Part 15 -- the argv0-POSITION bypass parts 13/14 documented as a residual
# and this corpus previously pinned as ALLOW. Every command below invokes a
# real commit through a Python `-c` payload whose own string literals sit
# ahead of the commit identity in the reconstruction.
# ---------------------------------------------------------------------------

_OP = "ceremony.scoped_git_commit"

_BYPASS_COMMANDS = [
    ("dunder-import-system", "python3 -c \"__import__('os').system('%s -m x')\"" % HELPER),
    (
        "getattr-over-dunder-import",
        "python3 -c \"getattr(__import__('os'),'system')('%s -m x')\"" % HELPER,
    ),
    ("eval-wrapping-a-sink", "python3 -c \"eval(\\\"__import__('os').system('%s')\\\")\"" % HELPER),
    ("exec-wrapping-a-sink", "python3 -c \"exec(\\\"import os; os.system('%s')\\\")\"" % HELPER),
    (
        "compile-wrapping-a-sink",
        "python3 -c \"exec(compile(\\\"import os; os.system('%s')\\\",'<s>','exec'))\"" % HELPER,
    ),
    (
        "importlib-import-module",
        "python3 -c \"import importlib; importlib.import_module('subprocess')"
        ".run(['%s'])\"" % HELPER,
    ),
    (
        "importlib-ambient",
        "python3 -c \"importlib.import_module('subprocess').run(['%s'])\"" % HELPER,
    ),
    (
        "subclasses-reflection-to-Popen",
        "python3 -c \"[c for c in ().__class__.__bases__[0].__subclasses__() "
        "if c.__name__ == 'Popen'][0](['%s'])\"" % HELPER,
    ),
    (
        "concat-assembled-module-name",
        "python3 -c \"__import__('sub'+'process').run(['%s'])\"" % HELPER,
    ),
    (
        "fstring-assembled-module-name",
        "python3 -c \"m='process'; __import__(f'sub{m}').run(['%s'])\"" % HELPER,
    ),
    ("percent-assembled", "python3 -c \"import os; os.system('%%s' %% '%s')\"" % HELPER),
    (
        "format-assembled",
        "python3 -c \"import os; os.system('{} -m x'.format('%s'))\"" % HELPER,
    ),
    # The same shapes reaching plain `git commit` and the invoke CLI rather
    # than the helper binary -- both identities the reconstruction must
    # resolve at a non-zero position too.
    ("git-commit-via-dunder-import", "python3 -c \"__import__('os').system('git commit -m x')\""),
    (
        "git-commit-via-getattr",
        "python3 -c \"getattr(__import__('os'),'system')('git commit -m x')\"",
    ),
    (
        "invoke-op-via-dunder-import",
        "python3 -c \"__import__('os').system("
        "'python3 -m coordinator_core.invoke %s {}')\"" % _OP,
    ),
    (
        "invoke-op-via-getattr",
        "python3 -c \"getattr(__import__('os'),'system')("
        "'python3 -m coordinator_core.invoke %s {}')\"" % _OP,
    ),
    # Helper at reconstruction positions 1, 2 and last.
    ("helper-at-position-1", "python3 -c \"__import__('os').system('%s -m x')\"" % HELPER),
    (
        "helper-at-position-2",
        "python3 -c \"getattr(__import__('os'),'system')('%s -m x')\"" % HELPER,
    ),
    (
        "helper-at-last-position",
        "python3 -c \"import subprocess; subprocess.run(['sh','-c','%s'])\"" % HELPER,
    ),
]

#: Boundary anchoring is the ONE thing position-relaxation must not take
#: with it: a name that merely CONTAINS a helper name, with nothing but a
#: non-separator character before it, is not that helper at position 0 and is
#: not that helper at position 3 either.
_BOUNDARY_NEGATIVE_COMMANDS = [
    ("evil-prefixed-helper-direct", "evil-scoped-git-commit -m x"),
    (
        "evil-prefixed-helper-in-reconstruction",
        "python3 -c \"__import__('os').system('evil-scoped-git-commit -m x')\"",
    ),
    (
        "evil-prefixed-helper-at-argv0-in-reconstruction",
        "python3 -c \"import os; os.system('evil-scoped-git-commit -m x')\"",
    ),
]


@pytest.mark.parametrize(
    "label,cmd", _BYPASS_COMMANDS, ids=[c[0] for c in _BYPASS_COMMANDS]
)
def test_reconstruction_bypass_command_now_denies(monkeypatch, label, cmd):
    """Part 15's whole point: a real commit reached through a Python `-c`
    payload denies regardless of where the helper/`git commit`/invoke-op
    identity lands in the literal reconstruction.
    """
    base._denies(monkeypatch, cmd)


@pytest.mark.parametrize(
    "label,cmd",
    _BOUNDARY_NEGATIVE_COMMANDS,
    ids=[c[0] for c in _BOUNDARY_NEGATIVE_COMMANDS],
)
def test_boundary_anchoring_survives_position_relaxation(monkeypatch, label, cmd):
    base._allows(monkeypatch, cmd)


@pytest.mark.parametrize(
    "label,cmd", _NEWLY_ALLOWED_COMMANDS, ids=[c[0] for c in _NEWLY_ALLOWED_COMMANDS]
)
def test_inert_read_only_command_survives_the_any_position_matcher(
    monkeypatch, label, cmd
):
    """The designed pair, asserted as a pair: any-position matching would
    flag every one of these read-only commands (each NAMES the helper in a
    string literal), and the part-14 inertness check is the only reason they
    still allow -- it withholds the reconstruction leg entirely, so part 15's
    matcher never sees them.
    """
    base._allows(monkeypatch, cmd)


def test_part15_matcher_never_fires_on_an_inert_payload():
    """Mechanism, not just verdict: the pair's ORDERING is what makes the
    read-only cases above survive -- inert check first, any-position
    matching only for what is left.
    """
    for _label, cmd in _NEWLY_ALLOWED_COMMANDS:
        assert not guard._has_reconstructed_commit_identity(cmd), cmd


#: Measured blast radius of part 15 -- the exact corpus labels whose verdict
#: moves when `_has_reconstructed_commit_identity` is forced off (that matcher
#: has exactly one call site in `check()`, so forcing it reproduces
#: pre-part-15 behaviour byte-for-byte). ALLOW -> DENY only; a DENY -> ALLOW
#: entry would mean the matcher had somehow suppressed an existing match,
#: which it structurally cannot.
_PART15_MOVING_LABELS = frozenset(
    {
        "dunder-import",
        "getattr-over-dunder-import",
        "eval",
        "exec",
        "compile",
        "importlib-imported",
        "importlib-ambient",
        "subclasses-reflection",
        "percent-assembled",
        "format-assembled",
        "split-module-name",
        "binop-in-call-func-position",
        "open-nonliteral-mode",
    }
)


def test_part15_corpus_moves_exactly_the_measured_set(monkeypatch):
    """Blast-radius pin for part 15, the same shape part 14's own pin takes:
    measure every adversarial command with the matcher live and with it
    forced off, and assert the moving set is EXACTLY the enumerated one --
    so a future widening of position-relaxation shows up as a diff here
    rather than as a quiet change in what this guard denies.

    Part 16 (2026-08-05) forces its two matchers off alongside part 15's
    here, and the reason is worth stating rather than reading as a
    weakening: constant folding reaches MOST of the same rows by a different
    route (it resolves the assembled name, then runs the same any-position
    identity matching), so leaving part 16 live would measure "rows only
    part 15 can reach" instead of "rows part 15 moved" -- an empty set, and
    a pin that silently stopped testing anything. The measured set below is
    unchanged, which is itself the assertion: part 16 did not move part 15's
    boundary, it added a second, overlapping one.
    """
    base._subagent(monkeypatch)

    def _verdict(cmd):
        return guard.check(base._payload(cmd, agent_type=base._SUBAGENT_TYPE)) is not None

    monkeypatch.setattr(guard, "_has_folded_commit_identity", lambda cmd, legs=None: False)
    monkeypatch.setattr(guard, "_has_opaque_execution_sink", lambda cmd, legs=None: False)
    live = {label: _verdict(cmd) for label, cmd, _ in _ADVERSARIAL_COMMANDS}
    monkeypatch.setattr(
        guard, "_has_reconstructed_commit_identity", lambda cmd, legs=None: False
    )
    before = {label: _verdict(cmd) for label, cmd, _ in _ADVERSARIAL_COMMANDS}

    to_deny = {label for label in live if live[label] and not before[label]}
    to_allow = {label for label in live if before[label] and not live[label]}
    assert to_deny == set(_PART15_MOVING_LABELS)
    assert to_allow == set()


def test_part15_moves_no_command_outside_the_python_c_family(monkeypatch):
    """The scope claim, mechanically: real shell segments, real ``sh -c``
    payloads and the outer command keep their argv0-anchored semantics --
    forcing the part-15 matcher off changes no verdict in the sibling
    module's parity matrix outside the Python ``-c`` family.
    """
    base._subagent(monkeypatch)

    def _verdict(cmd):
        return guard.check(base._payload(cmd, agent_type=base._SUBAGENT_TYPE)) is not None

    outside = [c for c, _ in base._VERDICT_PARITY_MATRIX if not ("python" in c and "-c " in c)]
    live = [_verdict(c) for c in outside]
    monkeypatch.setattr(
        guard, "_has_reconstructed_commit_identity", lambda cmd, legs=None: False
    )
    assert live == [_verdict(c) for c in outside]


def test_reconstruction_matcher_ignores_a_real_shell_payload():
    """Leg-scoping, at the matcher rather than the verdict: a genuine
    ``sh -c`` payload is never tagged as the literal-reconstruction leg, so
    this matcher declines it even though its text names a commit outright.
    """
    assert not guard._has_reconstructed_commit_identity("sh -c 'git commit -m x'")
    assert not guard._has_reconstructed_commit_identity("sh -c '%s -m x'" % HELPER)


# ---------------------------------------------------------------------------
# Part 17 (2026-08-05) -- the confirmed P0: the certifier was a DENYLIST
# wearing an allowlist's docstring, so every callable nobody had enumerated
# was inert BY DEFAULT. `typing.get_type_hints` and `dataclasses.make_
# dataclass` evaluate strings as code, cleared as inert, and the payload
# below ran a real process at GUARD=ALLOW.
#
# The rows here are kept OUT of `_ADVERSARIAL_COMMANDS` deliberately: that
# list feeds `test_part15_corpus_moves_exactly_the_measured_set`, whose
# enumerated moving set is a pin on part 15's boundary and not a place to
# record part 17's.
# ---------------------------------------------------------------------------

#: The exact program the review weaponized, ord-encoded so no leg that
#: recovers CONTAINED text can see it.
_ORD_ENCODED_PROGRAM = ",".join(
    str(ord(c)) for c in "__import__('os').system('%s -m x')" % HELPER
)

_ANNOTATION_EVAL_PAYLOADS = [
    (
        "reviewer-composite",
        "import typing, dataclasses\n"
        "s = ''.join([chr(c) for c in [%s]])\n"
        "C = dataclasses.make_dataclass('C', [('a', s)])\n"
        "typing.get_type_hints(C)\n" % _ORD_ENCODED_PROGRAM,
    ),
    ("get-type-hints", "import typing; typing.get_type_hints(int)"),
    ("forwardref-evaluate", "import typing; typing.ForwardRef('x')._evaluate(None, None)"),
    ("make-dataclass", "import dataclasses; dataclasses.make_dataclass('C', [])"),
    ("bare-get-type-hints", "from typing import get_type_hints; get_type_hints(int)"),
    ("typevar-bound", "import typing; typing.TypeVar('T', bound='%s')" % HELPER),
    ("namedtuple-annotation", "import typing; typing.NamedTuple('N', [('a', '%s')])" % HELPER),
]


@pytest.mark.parametrize(
    "label,payload",
    _ANNOTATION_EVAL_PAYLOADS,
    ids=[c[0] for c in _ANNOTATION_EVAL_PAYLOADS],
)
def test_annotation_eval_primitive_is_never_inert(label, payload):
    """The P0, pinned at the layer that failed: every one of these certified
    ``inert=True`` before part 17, and each is a minimal runtime-eval
    primitive -- annotation evaluation is a documented ``eval`` path, not an
    obscure corner.
    """
    assert not guard._python_c_payload_is_provably_inert(payload)


def test_weaponized_annotation_eval_command_denies_end_to_end(monkeypatch):
    """The verdict, not just the certification: the full ord-encoded
    composite reaches DENY through ``check()``. It denies via part 16's
    opaque-argument refusal -- ``make_dataclass``/``get_type_hints`` are
    execution sinks as of part 17, and the comprehension-built argument does
    not resolve -- which is the two halves of the fix meeting.
    """
    base._denies(
        monkeypatch,
        "python3 -c \"import typing, dataclasses\n"
        "s = ''.join([chr(c) for c in [%s]])\n"
        "C = dataclasses.make_dataclass('C', [('a', s)])\n"
        "typing.get_type_hints(C)\"" % _ORD_ENCODED_PROGRAM,
    )


def test_annotation_eval_roots_are_absent_from_the_import_allowlist():
    """Asserted on the CONSTANT rather than through a payload, so a future
    re-add of ``typing``/``dataclasses`` fails loudly here with the reason
    attached instead of silently re-opening the bypass. ``string`` and
    ``functools`` are pinned for the same reason: reflection through
    ``string.Formatter`` and callable application through ``functools.
    reduce`` are the two surfaces the review flagged as unexamined.
    """
    for root in ("typing", "dataclasses", "string", "functools"):
        assert root not in guard._INERT_PAYLOAD_IMPORT_ROOTS, root


def test_annotation_eval_family_reaches_part16_sink_sets_by_derivation():
    """Single-sourcing, mechanically: the family is added to part 14's
    attribute set ONLY, and part 16's sink sets must inherit it through the
    subtraction they are already derived by -- so a sibling name added later
    teaches both parts in one edit.
    """
    assert guard._ANNOTATION_EVAL_ATTRIBUTE_NAMES <= guard._NON_INERT_ATTRIBUTE_NAMES
    assert guard._ANNOTATION_EVAL_ATTRIBUTE_NAMES <= guard._EXECUTION_SINK_ATTRIBUTE_NAMES
    assert (
        guard._ANNOTATION_EVAL_ATTRIBUTE_NAMES
        <= guard._OPAQUE_PROGRAM_SINK_ATTRIBUTE_NAMES
    )


#: Callables absent from `_INERT_SAFE_CALLABLE_NAMES`. None is required to be
#: dangerous -- that is the point of the property being tested.
_UNLISTED_CALLABLE_PAYLOADS = [
    ("ast-literal-eval", "import ast; ast.literal_eval(open('%s').read())" % HELPER),
    ("re-compile", "import re; re.compile('x')"),
    ("collections-counter", "from collections import Counter; Counter('abc')"),
    ("textwrap-dedent", "import textwrap; textwrap.dedent('x')"),
    ("itertools-chain", "import itertools; itertools.chain([], [])"),
    ("datetime-strptime", "import datetime; datetime.datetime.strptime('x', 'y')"),
    ("unicodedata-normalize", "import unicodedata; unicodedata.normalize('NFC', 'x')"),
    ("base64-b64decode", "import base64; base64.b64decode('eA==')"),
    ("math-floor", "import math; math.floor(1.5)"),
    ("decimal-decimal", "import decimal; decimal.Decimal('1')"),
    ("unknown-bare-name", "frobnicate('%s')" % HELPER),
    ("unknown-attribute-call", "import json; json.loads('{}').frobnicate()"),
    ("unknown-attribute-read", "import ast; t = ast.parse('x'); print(t.body)"),
]


@pytest.mark.parametrize(
    "label,payload",
    _UNLISTED_CALLABLE_PAYLOADS,
    ids=[c[0] for c in _UNLISTED_CALLABLE_PAYLOADS],
)
def test_call_outside_the_safe_callable_allowlist_is_never_inert(label, payload):
    """THE ALLOWLIST-NOT-DENYLIST PROPERTY, pinned directly rather than
    through the shapes that happened to exploit its absence.

    Every payload here imports an allowlisted root and calls something
    harmless-looking that is simply not enumerated. Under the pre-part-17
    denylist shape all of them certified inert; under an allowlist all of
    them must not, because "unrecognised" and "dangerous" are the same
    answer. A future edit that re-inverts the direction fails HERE, on a
    corpus of boring calls, instead of failing in production on the one
    dangerous call nobody listed.
    """
    assert not guard._python_c_payload_is_provably_inert(payload)


def test_a_name_read_is_cleared_only_by_one_of_the_four_routes():
    """PART 21 -- the closure leg at the predicate, because it is the claim a
    docstring made for four parts without the code behind it.

    `_inert_load_name_is_cleared` is the enforcement: a Name READ is cleared
    only as an allowlisted callable, a payload-bound value, an import used as
    a receiver or call target, or an ``except`` type. The rows below are the
    four YES answers and the one NO, at the payload level so a future edit
    that loosens any of them fails on a shape rather than on a signature.
    """
    cleared = [
        "print(len(open('%s').read()))" % HELPER,  # allowlisted callable name
        "t = open('%s').read()\nprint(t)" % HELPER,  # payload-bound value
        "import json; print(json.dumps({}))",  # import as receiver
        "from json import loads; print(loads('{}'))",  # import as call target
        "try:\n    pass\nexcept KeyError:\n    pass\n",  # except type
    ]
    for payload in cleared:
        assert guard._python_c_payload_is_provably_inert(payload), payload
    assert not guard._python_c_payload_is_provably_inert("print(type)")


def test_import_binding_may_not_launder_into_an_allowlisted_spelling():
    """The second half of the closure, isolated: the allowlist clears a NAME
    because of what that name denotes, so an alias pointing a cleared name at
    a different callable makes the clearance a statement about nothing. An
    import that does not RENAME is honest and stays inert.
    """
    assert guard._import_binding_launders_a_cleared_name("literal_eval", "get")
    assert guard._import_binding_launders_a_cleared_name("json", "sorted")
    assert not guard._import_binding_launders_a_cleared_name("loads", "loads")
    assert not guard._import_binding_launders_a_cleared_name("literal_eval", "z")


def test_certificate_states_its_residual_rather_than_overstating_it():
    """THE RESIDUAL, PINNED AS A FACT rather than left for the next reviewer to
    rediscover: a payload may still BIND a member of a listed root under a
    non-allowlisted local name and read allowlisted ATTRIBUTES off it. That is
    what the certifier's docstring now says, and it is bounded by the audit at
    `_INERT_PAYLOAD_IMPORT_ROOTS` -- not by the closure argument.

    The guarantee that IS enforced is the next two assertions: that member can
    never be applied, whether by call or by callback.
    """
    assert guard._python_c_payload_is_provably_inert(
        "from json import codecs as c; print(c.encode('x', 'utf-8'))"
    )
    assert not guard._python_c_payload_is_provably_inert(
        "from json import codecs as c; print(c.frobnicate('x'))"
    )
    assert not guard._python_c_payload_is_provably_inert(
        "from json import codecs as c; print(sorted(['x'], key=c))"
    )


def test_safe_callable_allowlist_never_contradicts_the_forbidden_sets():
    """The two mechanisms must not disagree about a name: anything the
    forbidden sets refuse (``replace`` as ``os.replace``, ``run``, every
    annotation-eval name) may never appear on the callable allowlist, or the
    allowlist would be quietly re-permitting a known sink.
    """
    unsafe = (
        guard._NON_INERT_ATTRIBUTE_NAMES
        | guard._NON_INERT_BUILTIN_NAMES
        | guard._NON_INERT_MODULE_NAMES
    )
    assert not (guard._INERT_SAFE_CALLABLE_NAMES & unsafe)
    assert not any(
        guard._name_is_process_creation(name)
        for name in guard._INERT_SAFE_CALLABLE_NAMES
    )


#: PART 19 (2026-08-05) -- the audited ``os`` process-creation surface, as a
#: SET rather than as whatever a prefix test happened to catch. ``posix_spawn``
#: was the confirmed live ALLOW: it starts a program and does not START WITH
#: either family root, so no leg of the guard saw a sink. Enumerated here so a
#: future editor who narrows the matching finds out here.
_OS_PROCESS_CREATION_NAMES = [
    "system",
    "popen",
    "fork",
    "forkpty",
    "startfile",
    "posix_spawn",
    "posix_spawnp",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execv",
    "execve",
    "execvp",
    "execvpe",
]


@pytest.mark.parametrize("name", _OS_PROCESS_CREATION_NAMES)
def test_every_audited_os_process_creation_name_is_refused(name):
    """Part 19's gap 2, pinned as coverage of the FAMILY rather than of the
    two spellings that were reported: every audited ``os`` process-creation
    callable is non-inert, whether it is refused by name or by the
    name-boundary family match.
    """
    assert guard._attribute_name_is_non_inert(name), name
    assert not guard._python_c_payload_is_provably_inert("import os; os.%s('x')" % name)


def test_pty_fork_and_spawn_are_refused_through_the_same_sets():
    """``pty.fork``/``pty.spawn`` need no ``pty``-specific entry: every leg
    here is identity-blind about the receiving object, so the ``os`` names
    already cover them.
    """
    assert guard._attribute_name_is_non_inert("fork")
    assert guard._name_is_process_creation("spawn")
    assert not guard._python_c_payload_is_provably_inert("import pty; pty.fork()")


def test_aliased_sink_import_is_never_inert():
    """Part 19's gap 1 at the inertness leg: an aliased import of a sink is
    the sink, and a payload naming one cannot certify inert.
    """
    for payload in (
        "from subprocess import run as r; r(['x'])",
        "from os import system as s; s('x')",
        "from os import posix_spawn as ps; ps('x', [], {})",
    ):
        assert not guard._python_c_payload_is_provably_inert(payload)


def test_nested_reconstruction_chain_terminates_and_recovers_word_boundaries():
    """The eval/exec family needs a SECOND extraction pass: one pass yields
    the nested program as a single whitespace-free token, where no
    boundary-anchored matcher can see the helper.
    """
    payload = "eval(\"__import__('os').system('%s')\")" % HELPER
    chain = list(guard._nested_reconstruction_chain(payload))
    assert chain[0] == payload
    assert chain[-1] == "os %s" % HELPER
    assert len(chain) <= guard._MAX_COMMIT_UNWRAP_DEPTH + 1
