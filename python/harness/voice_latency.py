"""
python/harness/voice_latency.py -- how long a spoken turn took, measured, logged to D:, and shown in the status bar.

Three numbers answer "does it feel like a conversation", all in milliseconds from the voice runtime's own clock:

* ``wake_to_first_word``   the wake word is detected -> the first sound of her answer starts playing
* ``stt_latency``          you stop speaking -> the final transcript exists
* ``time_to_first_audio``  you stop speaking -> the first piece of her answer has been synthesised

and the parts they are made of (``stt_compute``, ``model_first_text``, ``first_sentence_tts``, ``first_audio_to_playback``). A follow-up
that needed no wake word has no ``wake_to_first_word``; a number whose two marks were not both seen is left out rather than zero.

Each turn is appended as one JSON line to ``voice-latency.jsonl`` in ``TALA_VOICE_LOG_DIR`` (else ``<user data>/logs/voice``). The line
holds numbers and the names of the models in use, and nothing else: never a transcript, never audio. A log directory on C: is refused,
and a log that cannot be written is a warning, never an error in the voice loop. The file is rotated at ``MAX_BYTES``.
"""
from __future__ import annotations

import json
import logging
import ntpath
import os
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FILE_NAME = "voice-latency.jsonl"
MAX_BYTES = 2_000_000
#: What an entry may carry. Anything else a caller passes (text, audio) is dropped, not written.
_ALLOWED = ("turn_id", "ms", "whisper_model", "wake_model", "tts_voice")


def measure(timeline: dict[str, float]) -> dict[str, int]:
    """The turn's latencies in whole milliseconds, from the runtime's marks. A number needs both of its marks, in order."""
    def span(start: str, end: str) -> int | None:
        if start in timeline and end in timeline and timeline[end] >= timeline[start]:
            return round((timeline[end] - timeline[start]) * 1000)
        return None

    found = {
        "wake_to_first_word": span("wake", "playback_start"),
        "stt_latency": span("speech_end", "stt_done"),
        "time_to_first_audio": span("speech_end", "first_audio"),
        "stt_compute": span("stt_start", "stt_done"),
        "model_first_text": span("turn_begin", "first_text"),
        "first_sentence_tts": round(timeline["first_sentence_tts"] * 1000) if timeline.get("first_sentence_tts") is not None else None,
        "first_audio_to_playback": span("first_audio", "playback_start"),
    }
    return {key: value for key, value in found.items() if value is not None}


def medians(entries: list[dict[str, Any]]) -> dict[str, int]:
    """The median of each number over the turns that have it, plus how many turns there were."""
    values: dict[str, list[int]] = {}
    for entry in entries:
        for key, value in (entry.get("ms") or {}).items():
            if isinstance(value, (int, float)):
                values.setdefault(key, []).append(int(value))
    out = {key: round(statistics.median(series)) for key, series in values.items()}
    out["turns"] = len(entries)
    return out


def _on_c_drive(text: str) -> bool:
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return ntpath.splitdrive(ntpath.normpath(text))[0].upper() == "C:" if text else False


def log_dir() -> Path | None:
    """Where the latency log goes, or None when that place is on C: (nothing is written there)."""
    configured = (os.environ.get("TALA_VOICE_LOG_DIR") or "").strip()
    if configured:
        return None if _on_c_drive(configured) else Path(configured)
    from python.harness import voice_settings

    root = voice_settings.user_data_dir() / "logs" / "voice"
    return None if _on_c_drive(str(root)) else root


def append(entry: dict[str, Any]) -> Path | None:
    """Write one turn to the log. Returns the file, or None if nothing was written (refused location, or an error: never raised)."""
    directory = log_dir()
    if directory is None:
        log.warning("voice: the latency log directory is on C: or unset; not writing it (set TALA_VOICE_LOG_DIR to a folder on D:)")
        return None
    clean: dict[str, Any] = {"at": datetime.now().astimezone().isoformat(timespec="milliseconds")}
    for key in _ALLOWED:
        if key not in entry or entry[key] is None:
            continue
        clean[key] = {k: int(v) for k, v in entry[key].items() if isinstance(v, (int, float))} if key == "ms" else str(entry[key])
    path = directory / FILE_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            os.replace(path, directory / (FILE_NAME + ".1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(clean) + "\n")
    except OSError as exc:
        log.warning("voice: could not write the latency log: %s", exc)
        return None
    return path


def read_recent(count: int) -> list[dict[str, Any]]:
    """The last ``count`` turns in the log, oldest first."""
    directory = log_dir()
    if directory is None or count <= 0:
        return []
    try:
        lines = (directory / FILE_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-count:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
