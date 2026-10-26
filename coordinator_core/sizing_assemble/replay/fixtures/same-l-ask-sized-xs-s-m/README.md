# same-l-ask-sized-xs-s-m

Vendored read-only from experiments (README, ask.txt, reps.json at cb1b734; test at fa1714f).

Defect #9 (R11): the identical ask returns a different `estimate.tshirt` and route per rep.

- `ask.txt`: the exact prompt given to every rep of the L rung task `ladder-l-close-out-artifacts` (coordinator arm r40-r49; byte-identical across reps, md5 e6c901583a0263263b3b1d030fcf2ade).
- `reps.json`: per-rep sizing outputs (tshirt, route, intent, cost). Seven reps: r49 XS (dispatch), r43 S (spec-dispatch), r41/r42/r44/r45/r46 M (plan). Carries no scout flags and no scope.

The scout supplies the t-shirt; `route()` is deterministic over its inputs. `test_replay_sizing_variance.py` pins both halves: the captured scout variance (strict xfail) and `route()` replayed over each captured tshirt with one shared at-or-above-floor scope.
