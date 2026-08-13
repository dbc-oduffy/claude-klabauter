"""
coordinator_core.ops.diagnostics_probes — three write-free `diagnostics.*` probe ops.

Purpose: give `coordinator/bin/lib/cc_invoke.py`'s failure ladder a target that can be
fired at a live, dirty, shared working tree without any chance of damaging it. Before
these ops existed, proving a transport fix end-to-end meant calling a REAL, MUTATING op
(337c31a1e was reproduced against `ceremony.wsc_tail` on a tree carrying 460+ dirty files
and live peers) and inspecting the tree afterwards for damage. That returned clean, but
by luck rather than by construction.

Ladder rungs these three reach, one op per rung:

  - ``diagnostics.always_succeeds``      — rc=0, a result envelope. The positive control:
                                           it proves the harness itself is wired, so a
                                           green failure-case is not green-by-accident.
  - ``diagnostics.always_refuses``       — rc=1 with a JSON-RPC ERROR envelope on
                                           **stdout**. This is precisely the rung
                                           `_raise_on_process_failure` was blind to until
                                           337c31a1e, because it read only stderr;
                                           `_op_error_detail` now recovers that envelope.
                                           It recovers the ENVELOPE, not this handler's own
                                           words: `dispatch_message` flattens a
                                           non-``structurally_wedged`` exception to
                                           "Internal error: DiagnosticsRefusal", so the
                                           message is dropped from stdout and survives only
                                           in stderr's traceback. The structural-pin branch
                                           below passes its text through intact — the
                                           asymmetry is the engine's, not a defect here.
  - ``diagnostics.always_structural_pin`` — rc=2 via ``STRUCTURAL_PIN_ERROR``, so
                                           ``cc_invoke`` raises ``StructuralPinError``.

Stderr is NOT empty on either raising probe, and an earlier draft of this docstring said
it was. ``coordinator_core.ipc`` logs the escaping handler exception with a full traceback
through `dispatch_message`, so both raising probes emit on BOTH channels. Rung selection is
unaffected — the traceback carries no ImportError token, so the top stderr sniff does not
fire and precedence holds — but an assertion demanding empty stderr will be red. Only
``no.such.op`` has genuinely empty stderr, because no handler ran and nothing was logged.
The engine's channel split is by ORIGIN, not severity; "op errors go to stdout" was never
"and nowhere else."

Write-free BY CONSTRUCTION, and that property is the whole deliverable: this module
imports nothing but ``register_op``, opens no file, spawns no process, invokes no git,
and touches no socket — in the handlers or in anything they call. It is kept to one
screen so a reviewer can confirm that by reading rather than by trusting a test.

Negative-spec — these handlers must NEVER acquire "useful" behaviour. No echoing a param
into a file, no touching git, no reading repo state, no env-var/debug gating (a probe
reachable only under a flag is unreachable by the peer EM debugging a live transport
failure at 2am, which is the entire use case). The moment one of them does real work,
AC2 stops being verifiable by reading and this family stops being safe to fire at a live
tree — which is the only reason it exists.

Spec backlink: pln-a-safe-target-for-transport-fa-7ea067 § C1
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op


class DiagnosticsRefusal(RuntimeError):
    """An op-level refusal with no underlying fault — the ordinary-error rung.

    Deliberately does NOT carry ``structurally_wedged``, so `dispatch_message` maps it to
    the generic ``INTERNAL_ERROR`` code and `invoke.main` prints the error envelope to
    stdout and exits 1. That rc=1/error-on-stdout shape is the thing under test; do not
    "improve" this into a structural pin. Stderr also carries ipc's traceback of this
    exception — see the module docstring; do not assert it empty.
    """


class DiagnosticsStructuralPin(RuntimeError):
    """A synthetic structural pin — the rc=2 rung.

    ``structurally_wedged`` is the duck-type marker `coordinator_core.ipc
    ._handler_exception_error` reads to select ``STRUCTURAL_PIN_ERROR`` (-32001) over
    ``INTERNAL_ERROR``, which `invoke.__main__._exit_code_for_response` turns into exit
    code 2 and `cc_invoke` turns into ``StructuralPinError``. Set here directly rather
    than by importing `ops.emit.validate.ContractPinError`, so this module keeps its
    import surface empty of anything that touches disk.
    """

    structurally_wedged = True


@register_op("diagnostics.always_succeeds")
def _always_succeeds(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Rung: success (rc=0, result envelope on stdout). The positive control.

    Returns a fixed literal. `params` and `repo_root` are accepted for handler-signature
    parity and deliberately ignored — echoing a caller value back is the first step
    toward this probe having behaviour worth trusting, and it must not have any.
    """
    return {"probe": "always_succeeds", "ok": True}


@register_op("diagnostics.always_refuses")
def _always_refuses(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Rung: op-level refusal (rc=1, JSON-RPC error envelope on STDOUT; stderr non-empty).

    Raising is the only way an op produces an error envelope — `dispatch_message` builds
    one from the escaping exception; a handler cannot return one. The refusal is
    unconditional: no param, env var, or repo state can make it succeed.
    """
    raise DiagnosticsRefusal(
        "diagnostics.always_refuses: unconditional probe refusal — this op exists to "
        "exercise the rc=1 error-on-stdout rung and never succeeds"
    )


@register_op("diagnostics.always_structural_pin")
def _always_structural_pin(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Rung: structural pin (rc=2, STRUCTURAL_PIN_ERROR, cc_invoke StructuralPinError).

    Unconditional, same as its sibling above. The exception message is preserved verbatim
    into the error envelope (that is the structural-pin branch's contract, so the message
    can state remediation), which is what makes this rung distinguishable end-to-end from
    the generic rc=1 one.
    """
    raise DiagnosticsStructuralPin(
        "diagnostics.always_structural_pin: unconditional synthetic structural pin — "
        "nothing is actually wedged; this op exists to exercise the rc=2 rung"
    )
