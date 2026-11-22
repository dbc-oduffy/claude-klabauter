"""Pure per-path landed/left hunk count: which of a path's measured hunks a commit carries."""

from __future__ import annotations


def _boundary(ops: list, k: int, *, end: bool) -> int | None:
    """Committed-side boundary of base boundary `k`; None when `k` sits strictly inside a changed block.

    A zero-width insert at `k` lies after a start boundary and before an end boundary, so a hunk's own
    region excludes inserts abutting its start and includes inserts abutting its end.
    """
    delta = 0
    for _tag, i1, i2, j1, j2 in ops:
        if i1 < k < i2:
            return None
        if i2 < k or (i2 == k and (end or i1 < i2)):
            delta += (j2 - j1) - (i2 - i1)
    return k + delta


def split_landed_hunks(base: bytes, measured: bytes, committed: bytes) -> dict:
    """Split `measured`'s hunks over `base` into those `committed` carries and those it leaves out.

    Returns {"landed": int, "left": int, "left_hunks": ["start,count", ...]} with measured-side 1-based
    line spans. Lines keep their endings, so CRLF content compares byte-exact. Pure: no I/O, no git.
    """
    from coordinator_core.ops.dispatch_emit.hunk_provenance import _hunk_ops, _line_spans

    base_l = base.splitlines(keepends=True)
    meas_l = measured.splitlines(keepends=True)
    comm_l = committed.splitlines(keepends=True)
    m_ops = _hunk_ops(base_l, meas_l)
    if committed == measured or not m_ops:
        return {"landed": len(m_ops), "left": 0, "left_hunks": []}
    c_ops = _hunk_ops(base_l, comm_l)
    landed = 0
    left_hunks: list = []
    for _tag, i1, i2, j1, j2 in m_ops:
        repl = meas_l[j1:j2]
        start = _boundary(c_ops, i1, end=False)
        stop = _boundary(c_ops, i2, end=True)
        ok = False
        if start is not None and stop is not None and start <= stop:
            region = comm_l[start:stop]
            if not repl:
                ok = not region
            elif region == repl:
                ok = True
            elif region != base_l[i1:i2]:
                n = len(repl)
                ok = any(region[x:x + n] == repl for x in range(len(region) - n + 1))
        if ok:
            landed += 1
        else:
            rng = f"{j1 + 1}-{j2}" if j2 > j1 else str(max(j1, 1))
            left_hunks.append(_line_spans(rng))
    return {"landed": landed, "left": len(left_hunks), "left_hunks": left_hunks}
