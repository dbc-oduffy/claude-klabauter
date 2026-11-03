import asyncio

from coordinator_core.telemetry import op_row_fields


def test_attach_and_drain_roundtrip():
    token = op_row_fields.begin()
    op_row_fields.attach(legs=[{"name": "x", "ms": 60.0}], handler_thread_ms=70.0)
    out = op_row_fields.drain(token)
    assert out == {"legs": [{"name": "x", "ms": 60.0}], "handler_thread_ms": 70.0}


def test_attach_without_begin_is_noop():
    op_row_fields.attach(a=1)
    token = op_row_fields.begin()
    assert op_row_fields.drain(token) == {}


def test_drain_resets_var():
    token = op_row_fields.begin()
    op_row_fields.drain(token)
    op_row_fields.attach(late=1)
    token2 = op_row_fields.begin()
    assert op_row_fields.drain(token2) == {}


def test_attach_inside_to_thread_is_visible_to_caller():
    async def run():
        token = op_row_fields.begin()
        await asyncio.to_thread(op_row_fields.attach, legs="from-thread")
        return op_row_fields.drain(token)

    assert asyncio.run(run()) == {"legs": "from-thread"}


def test_begin_inside_to_thread_is_invisible_to_caller():
    async def run():
        token = op_row_fields.begin()

        def handler():
            inner = op_row_fields.begin()
            op_row_fields.attach(lost=True)
            op_row_fields.drain(inner)

        await asyncio.to_thread(handler)
        return op_row_fields.drain(token)

    assert asyncio.run(run()) == {}
