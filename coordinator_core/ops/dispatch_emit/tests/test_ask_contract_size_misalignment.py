"""size_misaligned: halts only when an EM size of M or below meets an engine XL/XXL."""
import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as ac


def test_valid_sizes_exported():
    assert ac.VALID_SIZES == ("XS", "S", "M", "L", "XL", "XXL")
    assert ac.HALT_MISALIGNED not in (ac.HALT_ROOM, ac.HALT_SPLIT)


@pytest.mark.parametrize("em", ["XS", "S", "M", "m", " s "])
@pytest.mark.parametrize("engine", ["XL", "XXL", "xl"])
def test_small_em_large_engine_halts(em, engine):
    assert ac.size_misaligned(em, engine) is True


@pytest.mark.parametrize("em", ["L", "XL", "XXL"])
@pytest.mark.parametrize("engine", ["XL", "XXL"])
def test_large_em_never_halts(em, engine):
    assert ac.size_misaligned(em, engine) is False


@pytest.mark.parametrize("engine", ["XS", "S", "M", "L"])
@pytest.mark.parametrize("em", ["XS", "S", "M"])
def test_small_engine_never_halts(em, engine):
    assert ac.size_misaligned(em, engine) is False


@pytest.mark.parametrize("em", [None, "", "huge", 3])
def test_no_or_unknown_em_size_never_halts(em):
    assert ac.size_misaligned(em, "XXL") is False
