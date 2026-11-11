"""The sim-match tool's shared pieces, starting with the interval it reports rates with."""

from __future__ import annotations


def test_rates_module_matches_eval_report():
    from magic_cards_model_api.rates import beta_interval as b
    from scripts.eval_report import beta_interval as a

    assert a is b
    assert b(3, 10)["trials"] == 10
