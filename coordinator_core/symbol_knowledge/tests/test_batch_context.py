"""Shared batch context: one parse per file, spans with body hashes, budgeted fit, rendered block."""

from __future__ import annotations

import hashlib

from coordinator_core.symbol_knowledge import batch_context as bc

SRC = '''import os
from pkg.util import helper as h, other

CONST = 1


def alpha(x):
    """Alpha does things.

    More."""
    return h(x)


def beta(y):
    return y


class Box:
    """A box."""

    def open(self, lid):
        return lid
'''


def _repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text(SRC, encoding="utf-8")
    return tmp_path


def _t(name, path="pkg/m.py"):
    return {"qualified_name": name, "path": path}


def test_span_hash_imports_and_neighbours(tmp_path):
    root = _repo(tmp_path)
    ctx = bc.resolve(root, [_t("pkg.m.beta"), _t("pkg.m.Box.open")])["pkg/m.py"]
    span = ctx.spans["pkg.m.beta"]
    assert span.source == "def beta(y):\n    return y"
    assert span.body_hash == hashlib.sha256(span.source.encode("utf-8")).hexdigest()
    assert ctx.spans["pkg.m.Box.open"].source.strip().startswith("def open(self, lid):")
    assert "os" in ctx.imports and "pkg.util: helper, other" in ctx.imports
    assert any(n.startswith("def alpha(x)") and "Alpha does things." in n for n in ctx.neighbours)
    assert not any("def beta" in n for n in ctx.neighbours)
    assert not any("def open" in n for n in ctx.neighbours)


def test_scip_style_name_finds_the_span(tmp_path):
    root = _repo(tmp_path)
    ctx = bc.resolve(root, [_t("scip-python python pkg 0.1 `pkg.m`/alpha().")])["pkg/m.py"]
    assert next(iter(ctx.spans.values())).source.startswith("def alpha(x):")


def test_each_file_is_resolved_once_for_many_targets(tmp_path):
    root = _repo(tmp_path)
    out = bc.resolve(root, [_t("pkg.m.alpha"), _t("pkg.m.beta"), _t("pkg.m.Box")])
    assert list(out) == ["pkg/m.py"] and len(out["pkg/m.py"].spans) == 3


def test_missing_file_gives_empty_context_and_target_stays(tmp_path):
    ctx = bc.resolve(tmp_path, [_t("x.y", "nope.py")])["nope.py"]
    assert ctx.spans == {} and ctx.imports == []
    kept, deferred = bc.fit([_t("x.y", "nope.py")], {"nope.py": ctx})
    assert len(kept) == 1 and deferred == []


def test_fit_defers_oversize_symbol_whole_never_truncates(tmp_path):
    root = _repo(tmp_path)
    ctxs = bc.resolve(root, [_t("pkg.m.alpha"), _t("pkg.m.beta")])
    big = ctxs["pkg/m.py"].spans["pkg.m.alpha"]
    ctxs["pkg/m.py"].spans["pkg.m.alpha"] = bc.TargetSpan(1, 2, "x" * (bc.PER_SYMBOL_TOKEN_CAP * 4 + 4), big.body_hash)
    kept, deferred = bc.fit([_t("pkg.m.alpha"), _t("pkg.m.beta")], ctxs)
    assert [s["qualified_name"] for s in kept] == ["pkg.m.beta"]
    assert [s["qualified_name"] for s in deferred] == ["pkg.m.alpha"]


def test_fit_defers_overflow_past_batch_budget(tmp_path):
    root = _repo(tmp_path)
    syms = [_t("pkg.m.alpha"), _t("pkg.m.beta")]
    ctxs = bc.resolve(root, syms)
    first = bc.estimate_tokens(ctxs["pkg/m.py"].spans["pkg.m.alpha"].source)
    kept, deferred = bc.fit(syms, ctxs, budget=first)
    assert [s["qualified_name"] for s in kept] == ["pkg.m.alpha"]
    assert [s["qualified_name"] for s in deferred] == ["pkg.m.beta"]


def test_render_block_states_neighbour_overflow(tmp_path):
    ctx = bc.FileContext("a.py", neighbours=[f"def f{i}()" for i in range(bc.NEIGHBOUR_LIMIT + 3)])
    block = bc.render_block(ctx, ["a.f"])
    assert "(+3 more neighbours not shown)" in block and "source of a.f: unavailable" in block
