## What the engine will not measure here

`kill_turn_mean`, `kill_turn_p50` and `race_win_prob` are all `not_modelled`.
The full reason is in `evidence-spec.json`; the short version is that three
separate cost reductions - delve, warp and evoke - have no goldfish primitive,
so Murktide Regent is cast at {5}{U}{U}, Quantum Riddler at {3}{U}{U} rather
than for warp {1}{U}, and Subtlety at {2}{U}{U} rather than free. Psychic
Frog's clock is a repeatable pump plus granted flying, and neither has a
primitive either - and `combat damage ignores blockers`, so flying would be
worth nothing if it did.

This was measured before it was refused. Enabled, the spec recorded
`kill_turn_mean` **10.92 (+/-0.0274)** at `max_turns: 15`, for a deck whose real
clock is a grown flying Frog around turn five. The disqualifying fact is not
that it is slow, it is that it is **ordered wrong**:

    candidate --swap-in "2 Murktide Regent" --swap-out "2 Tasigur, the Golden Fang"
    kill_turn_mean: 10.92 -> 11.02

The engine ranks a 4/5 ground body ABOVE a 3/3 flier because it models neither
delve nor evasion. A clock that is wrong in that direction would have decided
this deck's swaps backwards, so it is refused rather than recorded. The fix is
engine primitives, not a sim profile.
