from python.harness.server import _prompt_implies_untaken_action


def test_ignores_the_confirmed_live_false_positive():
    """Regression test for a real, live production bug: a plain casual
    question triggered the "you stated intent to search/edit" auto-continue
    directive three times in a row. Root cause: the check ran against the
    fully assembled prompt (goal + wiki context + memory + astrology
    influence), not the user's actual message -- injected context happened
    to contain action words unrelated to what was actually said."""
    assert _prompt_implies_untaken_action("Ever hear of the term sexting?") is False


def test_word_boundary_avoids_incidental_substring_hits():
    assert _prompt_implies_untaken_action("I love turnips") is False
    assert _prompt_implies_untaken_action("I am addicted to coffee") is False
    assert _prompt_implies_untaken_action("this feels fixated") is False


def test_still_detects_real_action_requests():
    assert _prompt_implies_untaken_action("please search the web for cats") is True
    assert _prompt_implies_untaken_action("can you fix this bug") is True
    assert _prompt_implies_untaken_action("build a new feature for me") is True
    assert _prompt_implies_untaken_action("find that file and remove it") is True


def test_case_insensitive():
    assert _prompt_implies_untaken_action("SEARCH the web please") is True
