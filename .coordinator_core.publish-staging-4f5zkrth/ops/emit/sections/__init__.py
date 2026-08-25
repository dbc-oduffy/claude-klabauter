"""Section porters — one module per cockpit entity family.

Each sections/<name>.py exposes:
    def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:  # (records, malformed)

The parity harness (tests/test_emit_parity.py) auto-discovers every module here and
gates each against the golden fixture via assert_section_parity(<name>).
"""
