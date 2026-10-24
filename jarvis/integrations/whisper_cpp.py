"""Offline speech recognition using whisper.cpp.

Runs the native whisper-cli.exe against a 16 kHz mono WAV, which is exactly what
AudioRecorder already produces, so no resampling or format conversion is needed.

Zero Python packages: a native binary plus a GGML model file, both kept outside the
repository. Everything runs locally on the CPU - no network, no API, no cost.

The interface matches SpeechRecognizer (is_available / transcribe_wav), so either
engine drops into Backend unchanged.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from jarvis.integrations.process import run_contained

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_BINARY = ROOT / "tools" / "whisper" / "Release" / "whisper-cli.exe"
DEFAULT_MODEL = ROOT / "models" / "ggml-base.en.bin"

# JARVIS_WHISPER_BIN / JARVIS_WHISPER_MODEL override the defaults without code changes.
ENV_BINARY = "JARVIS_WHISPER_BIN"
ENV_MODEL = "JARVIS_WHISPER_MODEL"

# whisper emits bracketed non-speech annotations such as [BLANK_AUDIO] or (wind blowing).
_NOISE = re.compile(r"[\[(][^\])]*[\])]")


class WhisperCppRecognizer:
    """Transcribe a WAV with whisper.cpp. Never raises: failures return ("", 0.0)."""

    def __init__(self, binary: Path | str | None = None, model: Path | str | None = None,
                 runner=run_contained, threads: int = 4):
        self.binary = Path(binary or os.environ.get(ENV_BINARY) or DEFAULT_BINARY)
        self.model = Path(model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL)
        self._runner = runner
        self.threads = threads

    def is_available(self) -> bool:
        """Both the binary and the model must be present; neither ships in the repository."""
        return sys.platform == "win32" and self.binary.is_file() and self.model.is_file()

    def transcribe_wav(self, wav_path: Path | str, timeout: float = 30.0) -> tuple[str, float]:
        wav = Path(wav_path).resolve()
        if not wav.is_file() or wav.stat().st_size < 100:
            return "", 0.0
        if not self.is_available():
            return "", 0.0

        argv = [str(self.binary), "-m", str(self.model), "-f", str(wav),
                "-l", "en", "-nt", "-np", "-t", str(self.threads)]
        try:
            res = self._runner(argv, timeout=timeout)
        except Exception:
            return "", 0.0
        if getattr(res, "returncode", 1) != 0:
            return "", 0.0

        text = _NOISE.sub(" ", res.stdout or "")
        text = " ".join(text.split()).strip()
        if not text:
            return "", 0.0
        # whisper.cpp reports no per-utterance confidence. A fixed value is returned so the
        # tuple stays compatible; Backend only logs it and nothing gates on it.
        return text, 0.9


class CascadingRecognizer:
    """Try the primary engine, fall back to the secondary one.

    Used to keep Windows SAPI as a safety net: if whisper.cpp is missing, times out,
    crashes or returns nothing, recognition still happens through the original path.
    """

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.last_engine = ""

    def is_available(self) -> bool:
        return bool(self._ok(self.primary) or self._ok(self.fallback))

    @staticmethod
    def _ok(engine) -> bool:
        try:
            return bool(engine is not None and engine.is_available())
        except Exception:
            return False

    def transcribe_wav(self, wav_path: Path | str, timeout: float = 30.0) -> tuple[str, float]:
        if self._ok(self.primary):
            try:
                text, conf = self.primary.transcribe_wav(wav_path, timeout=timeout)
                if text:
                    self.last_engine = type(self.primary).__name__
                    return text, conf
            except Exception:
                pass    # fall through to the secondary engine
        if self._ok(self.fallback):
            try:
                text, conf = self.fallback.transcribe_wav(wav_path, timeout=timeout)
                self.last_engine = type(self.fallback).__name__
                return text, conf
            except Exception:
                pass
        self.last_engine = ""
        return "", 0.0
