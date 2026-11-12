# S6 fix wave report (T-123)

1. Activate ability_index: mtgo_convert.activated_effects splits oracle text into activated
   abilities; the logged "( ... )" text is matched; unique match sends ability_index; a card
   with at most one ability keeps the default; multi-ability without a unique match, or no
   oracle text in time, is Unmapped ("cannot tell which ability of X was activated").
   Real-engine test pins Liliana index 1 = the sacrifice ability.
2. Graveyard cast: emit uses the owner's graveyard copy (created in the setup graveyard if
   unknown, never hand) and sends from_zone "graveyard"; replay shares the _cast_zone rule.
3. Caps: MAX_LOG_CHARS=1,000,000 in manager.py (SimRefused before the slot is taken);
   unsupported list capped at 200 entries (unsupported_omitted count) with 300 chars per text.
4. Exactly 2 players: require_two_players raises ValueError (surfaced as the tool error).

Evidence: tests/test_sim_*.py -> 248 passed in 242.39s; mise run lint -> All checks passed!
Fuzzy Scryfall cache finding left deferred.
