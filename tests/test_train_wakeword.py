"""
scripts/train_wakeword.py -- the parts that are logic rather than audio: which voices are held out, where in a clip the
classifier should fire, how noise is mixed in, when a model is good enough to install, and where files may be written.

The point of holding out whole speakers is that a model measured on voices it trained on tells you nothing: it would pass
by memorising them. The point of the bar is that the pretrained fallback stays in use unless the trained model beats it on
voices it has never heard.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "train_wakeword.py"
spec = importlib.util.spec_from_file_location("train_wakeword", SCRIPT)
tw = importlib.util.module_from_spec(spec)
sys.modules["train_wakeword"] = tw  # dataclasses look the module up by name
spec.loader.exec_module(tw)


def test_train_and_held_out_speakers_never_overlap():
    train, test = tw.split_speakers(seed=7)
    assert {s.label for s in train}.isdisjoint({s.label for s in test})


def test_whole_voices_are_held_out_not_just_clips():
    train, test = tw.split_speakers(seed=7)
    train_voices = {s.name for s in train if s.speaker_id is None}
    test_voices = {s.name for s in test if s.speaker_id is None}
    assert train_voices.isdisjoint(test_voices) and {"en_US-kusal-medium", "en_US-lessac-medium"} <= test_voices


def test_the_windows_voices_are_only_ever_test_speakers():
    train, test = tw.split_speakers(seed=7)
    assert not any(s.kind == "sapi" for s in train) and sum(s.kind == "sapi" for s in test) == 2


def test_libritts_speakers_are_split_by_id_and_the_split_is_reproducible():
    a = tw.split_speakers(seed=7)
    b = tw.split_speakers(seed=7)
    assert a == b
    ids_train = {s.speaker_id for s in a[0] if s.speaker_id is not None}
    ids_test = {s.speaker_id for s in a[1] if s.speaker_id is not None}
    assert ids_train and ids_test and ids_train.isdisjoint(ids_test)


def test_a_different_seed_holds_out_different_speakers():
    assert tw.split_speakers(seed=7) != tw.split_speakers(seed=8)


# --- where it should fire ---------------------------------------------------------------------------------------------------------


def test_positive_frames_come_after_the_phrase_has_ended_not_before():
    end = int(2.3 * tw.SR)
    first, last = tw.positive_frame_range(end)
    assert first * tw.HOP + tw.EMBED_TAIL >= end - tw.HOP, "the window has seen the whole phrase"
    assert last >= first


def test_every_window_position_is_labelled_and_only_the_end_of_the_phrase_is_positive():
    labels = tw.label_windows(n_frames=40, end_sample=int(2.3 * tw.SR))
    assert [f for f, _ in labels] == list(range(tw.WINDOW - 1, 40))
    positives = [f for f, label in labels if label == 1]
    assert positives and positives == list(range(positives[0], positives[-1] + 1))
    assert len(positives) <= 6, "a few frames after the phrase, not the whole clip"


def test_a_negative_clip_has_no_positive_windows():
    assert all(label == 0 for _f, label in tw.label_windows(40, None))


def test_a_phrase_that_ends_before_the_window_can_fill_still_gets_a_valid_range():
    first, last = tw.positive_frame_range(int(0.5 * tw.SR))
    assert first >= tw.WINDOW - 1 and last >= first


# --- noise ---------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("snr", [5, 10, 20, 30])
def test_noise_is_mixed_at_the_requested_signal_to_noise_ratio(snr):
    rng = np.random.default_rng(0)
    signal = (0.3 * np.sin(np.arange(16000) / 7.0)).astype(np.float32)
    noise = rng.standard_normal(16000).astype(np.float32)
    mixed = tw.mix_at_snr(signal, noise, snr)
    measured = 20 * np.log10(tw.rms(signal) / tw.rms(mixed - signal))
    assert abs(measured - snr) < 0.3


def test_noise_shorter_than_the_signal_is_repeated_not_dropped():
    mixed = tw.mix_at_snr(np.ones(1000, np.float32) * 0.2, np.ones(10, np.float32), 10)
    assert len(mixed) == 1000


@pytest.mark.parametrize("kind", ["white", "pink", "brown", "hum"])
def test_every_noise_kind_is_generated_at_the_requested_length(kind):
    out = tw.noise_clip(kind, 8000, np.random.default_rng(1))
    assert len(out) == 8000 and np.isfinite(out).all() and tw.rms(out) > 0


def test_a_placed_phrase_ends_inside_the_clip_with_room_after_it():
    import random

    clip, end = tw.place(np.ones(int(0.9 * tw.SR), np.float32), random.Random(3))
    assert len(clip) == int(tw.CLIP_S * tw.SR) and end <= len(clip) - int(0.5 * tw.SR)


def test_pronunciations_include_the_phrase_itself_and_alternative_spellings():
    variants = tw.pronunciations("Hey Tala")
    assert "hey tala" in variants and len(variants) >= 3
    assert tw.pronunciations("Computer") == ["computer"]


# --- the bar ---------------------------------------------------------------------------------------------------------------------------


GOOD = {"frr_clean": 0.02, "frr_snr10": 0.06, "frr_snr5": 0.2, "negative_minutes": 30.0, "far_per_hour": 0.0}


def test_a_model_that_clears_the_bar_passes():
    assert tw.meets_bar(GOOD) == (True, [])


@pytest.mark.parametrize("key,value", [("frr_clean", 0.3), ("frr_snr10", 0.5), ("frr_snr5", 0.9), ("far_per_hour", 3.0)])
def test_each_shortfall_fails_and_is_named(key, value):
    ok, why = tw.meets_bar({**GOOD, key: value})
    assert ok is False and any(key in line for line in why)


def test_too_little_negative_audio_is_not_evidence_of_few_false_accepts():
    ok, why = tw.meets_bar({**GOOD, "negative_minutes": 2.0})
    assert ok is False and "negative audio" in why[0]


def test_missing_metrics_fail_closed():
    assert tw.meets_bar({})[0] is False


# --- where it writes --------------------------------------------------------------------------------------------------------------------


def test_it_refuses_the_c_drive():
    with pytest.raises(SystemExit):
        tw.refuse_c_drive(Path(r"C:\Users\someone\models"))
    tw.refuse_c_drive(Path(r"D:\models\voice"))


def test_install_is_a_deliberate_flag_and_a_failed_model_is_never_installed():
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"--install"' in source
    assert "NOT installed: the model did not clear the bar" in source
    assert source.index("if not passed") < source.index("write_bytes(candidate.read_bytes())")


def test_the_report_states_that_it_was_measured_on_synthetic_speech_only():
    assert "synthetic speech only" in SCRIPT.read_text(encoding="utf-8")


def test_the_script_downloads_nothing():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "hf_hub_download" not in source and "urlopen" not in source and "requests." not in source


# --- trimming ---------------------------------------------------------------------------------------------------------------------


def test_silence_is_trimmed_from_both_ends_so_every_voice_ends_where_its_last_word_ends():
    word = (0.5 * np.sin(np.arange(8000) / 5.0)).astype(np.float32)
    padded_a = np.concatenate([np.zeros(2000, np.float32), word, np.zeros(12000, np.float32)])  # a long-padding voice
    padded_b = np.concatenate([np.zeros(1500, np.float32), word, np.zeros(3000, np.float32)])    # a short-padding voice
    a, b = tw.trim_silence(padded_a), tw.trim_silence(padded_b)
    assert abs(len(a) - len(b)) <= 10
    assert len(a) < len(word) + 2 * int(0.05 * tw.SR)


def test_trimming_silence_alone_leaves_it_alone_and_a_word_is_never_cut():
    assert len(tw.trim_silence(np.zeros(1000, np.float32))) == 1000
    word = (0.5 * np.sin(np.arange(8000) / 5.0)).astype(np.float32)
    assert float(np.max(np.abs(tw.trim_silence(word)))) == pytest.approx(float(np.max(np.abs(word))))


# --- choosing the threshold ---------------------------------------------------------------------------------------------------------


def at(frr_clean=0.0, frr_snr10=0.0, frr_snr5=0.1, far=0.0, minutes=25.0):
    return {"frr_clean": frr_clean, "frr_snr10": frr_snr10, "frr_snr5": frr_snr5, "far_per_hour": far, "negative_minutes": minutes}


def test_the_lowest_threshold_that_clears_the_bar_is_chosen():
    sweep = {0.5: at(far=7.0), 0.6: at(far=3.0), 0.7: at(far=0.5), 0.8: at(far=0.0, frr_snr5=0.2)}
    threshold, metrics = tw.choose_threshold(sweep)
    assert threshold == 0.7 and metrics["far_per_hour"] == 0.5


def test_a_higher_threshold_that_rejects_too_many_real_utterances_is_not_chosen():
    sweep = {0.5: at(far=7.0), 0.9: at(far=0.0, frr_clean=0.4)}
    threshold, metrics = tw.choose_threshold(sweep)
    assert tw.meets_bar(metrics)[0] is False and threshold == 0.5, "nothing clears the bar, so the default is reported"


def test_when_nothing_clears_the_bar_the_default_threshold_and_its_shortfalls_are_reported():
    threshold, metrics = tw.choose_threshold({0.5: at(far=9.0), 0.8: at(far=4.0)})
    assert threshold == 0.5 and metrics["far_per_hour"] == 9.0


def test_the_threshold_is_written_into_the_report_so_the_runtime_can_use_it():
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"threshold": threshold' in source or "'threshold': threshold" in source
