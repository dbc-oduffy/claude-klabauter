"""A fan-out slice whose reserved run already spent its token is not re-reserved, so a {sizing} refire mints a fresh run."""
from coordinator_core.hooks import submit_engine_ask
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.ask_gate import TOKEN_SPENT_FILE


def test_spent_reservation_is_detected(tmp_path):
    run_id = "ask-20261102T215153-eda465"
    rd = record_dir(tmp_path, run_id)
    rd.mkdir(parents=True)
    assert submit_engine_ask._reserved_token_spent(tmp_path, run_id) is False
    (rd / TOKEN_SPENT_FILE).write_text("{}\n", encoding="utf-8")
    assert submit_engine_ask._reserved_token_spent(tmp_path, run_id) is True


def test_unsafe_run_id_reads_not_spent(tmp_path):
    assert submit_engine_ask._reserved_token_spent(tmp_path, "../escape") is False
