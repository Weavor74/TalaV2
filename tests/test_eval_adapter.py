"""scripts/eval_adapter.py: the held-out loss the gate's persona check is built on. The model-facing part is exercised live; here, the arithmetic and the arguments."""
from __future__ import annotations

import pytest

from scripts import eval_adapter as ea


def test_the_loss_is_averaged_over_tokens_not_over_examples():
    # a long example must weigh more than a short one: (10 + 2) / (5 + 1), not the mean of 2.0 and 2.0 by luck
    assert ea.mean_nll([(10.0, 5), (2.0, 1)]) == pytest.approx(2.0)
    assert ea.mean_nll([(9.0, 3), (1.0, 7)]) == pytest.approx(1.0)


def test_no_scored_tokens_is_not_a_zero_loss():
    assert ea.mean_nll([]) is None and ea.mean_nll([(0.0, 0)]) is None


def test_an_adapter_argument_is_a_name_and_a_path():
    assert ea.parse_adapter("candidate=D:/x/adapter") == ("candidate", "D:/x/adapter")
    assert ea.parse_adapter("current=C:\\a=b\\adapter") == ("current", "C:\\a=b\\adapter"), "only the first = splits"
    for bad in ("nopath", "=path", "name="):
        with pytest.raises(SystemExit):
            ea.parse_adapter(bad)


def test_the_name_base_is_reserved_for_the_plain_model():
    with pytest.raises(SystemExit):
        ea.parse_adapter("base=D:/x/adapter")


def test_the_summary_carries_what_the_gate_reads():
    summary = ea.summarise({"base": [(10.0, 5)], "candidate": [(8.0, 5), (1.0, 1)]})

    assert summary["base"] == {"nll": 2.0, "tokens": 5, "examples": 1}
    assert summary["candidate"]["nll"] == pytest.approx(1.5) and summary["candidate"]["examples"] == 2
