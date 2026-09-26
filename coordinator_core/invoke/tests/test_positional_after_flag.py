from coordinator_core.invoke.__main__ import _build_arg_parser


def test_params_json_survives_a_flag_placed_before_it():
    args = _build_arg_parser().parse_intermixed_args(
        ["memo.check_addressee", "--bare", '{"to":"x"}']
    )
    assert args.op == "memo.check_addressee"
    assert args.params_json == '{"to":"x"}'
    assert args.bare is True


def test_flag_after_both_positionals_still_parses():
    args = _build_arg_parser().parse_intermixed_args(
        ["memo.check_addressee", '{"to":"x"}', "--bare"]
    )
    assert args.op == "memo.check_addressee"
    assert args.params_json == '{"to":"x"}'
    assert args.bare is True


def test_op_alone_leaves_params_json_unset():
    args = _build_arg_parser().parse_intermixed_args(["coverage.gate"])
    assert args.op == "coverage.gate"
    assert args.params_json is None
