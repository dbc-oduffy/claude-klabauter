"""Names shared by the plan-base rebind, predecessor-work adoption and keep-open halted-credit ops."""

#: Op that rebinds a plan's falsifier.baseline_ref and register_claims.pins to a new stamp.
OP_REBIND_PLAN_BASE = "dispatch.rebind_plan_base"
#: Op that credits a halted or superseded predecessor's kept work to a fresh run of the same sizing.
OP_ADOPT_PREDECESSOR_WORK = "dispatch.adopt_predecessor_work"

#: ``dispatch.record_halted_run_landed`` param: credit the ``met_by`` rows without closing the run.
KEEP_OPEN = "keep_open"

#: Reply key: whether the op changed anything (false on an idempotent repeat).
APPLIED = "applied"
#: Reply key (rebind): the stamp the plan is now bound to.
STAMP = "stamp"
#: Reply key (rebind): the register_claims pins rewritten to the stamp.
PINS = "pins"
#: Reply key (adopt): the paths credited to the fresh run.
ADOPTED = "adopted"
#: Reply key (adopt): the paths refused, each with its reason.
REFUSED = "refused"
