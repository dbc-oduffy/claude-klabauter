"""P-B replay: a null fire-time t-shirt (staged resume seeds tshirt:null) must keep Kira in the composed wave."""

from coordinator_core.ops.dispatch_emit import run_truth_contract as rtc
from coordinator_core.ops.review_mint.tests import test_execute_review as t


def test_null_fire_time_size_admits_kira_in_the_composed_wave():
    block = t._wave_block(t._ratio_review(kira_min="L"), executor_rows=4, tshirt_js="_gate?.tshirt")
    assert '((_gate?.tshirt) == null || ["L", "XL", "XXL"].includes(_gate?.tshirt))' in block
    assert "coordinator:overengineering-reviewer" in block


def test_unknown_compile_time_size_admits_kira_in_the_composed_wave():
    block = t._wave_block(t._ratio_review(kira_min="L"), executor_rows=51)
    assert "coordinator:overengineering-reviewer" in block


def test_known_small_size_still_excludes_kira():
    block = t._wave_block(t._ratio_review(kira_min="L"), executor_rows=4, tshirt_js="_gate?.tshirt")
    assert '["L", "XL", "XXL"].includes(_gate?.tshirt)' in block
    assert "M" not in ("L", "XL", "XXL")


def test_mandatory_stages_fail_safe_on_unknown_size_and_standard_label():
    assert rtc.STAGE_KIRA in rtc.mandatory_stages(rtc.ARM_M_PLUS, None)
    assert rtc.mandatory_stages(rtc.ARM_M_PLUS, "standard") == rtc.MANDATORY_STAGES
