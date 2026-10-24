"""whisper.cpp recognizer and the SAPI fallback cascade.

The runner is mocked throughout: no model, no binary and no audio are needed, so these
tests are fast and run on a machine that has never downloaded a model.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import jarvis.integrations.whisper_cpp as whisper_cpp
from jarvis.integrations.whisper_cpp import CascadingRecognizer, WhisperCppRecognizer


def result(stdout="", returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class FakeEngine:
    """Minimal stand-in with the recognizer interface."""

    def __init__(self, text="", conf=0.0, available=True, raises=False):
        self.text, self.conf, self.available, self.raises = text, conf, available, raises
        self.calls = 0

    def is_available(self):
        return self.available

    def transcribe_wav(self, wav_path, timeout=30.0):
        self.calls += 1
        if self.raises:
            raise RuntimeError("engine exploded")
        return self.text, self.conf


class WhisperCppTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wav = Path(self._tmp.name) / "a.wav"
        self.wav.write_bytes(b"x" * 500)
        self.bin = Path(self._tmp.name) / "whisper-cli.exe"
        self.bin.write_bytes(b"MZ")
        self.model = Path(self._tmp.name) / "m.bin"
        self.model.write_bytes(b"ggml")

    def tearDown(self):
        self._tmp.cleanup()

    def _rec(self, runner, **kw):
        return WhisperCppRecognizer(binary=self.bin, model=self.model, runner=runner, **kw)

    def test_unavailable_without_binary_or_model(self):
        self.assertFalse(WhisperCppRecognizer(binary=self.bin, model="nope.bin").is_available())
        self.assertFalse(WhisperCppRecognizer(binary="nope.exe", model=self.model).is_available())
        self.assertTrue(self._rec(mock.Mock()).is_available())

    def test_transcribes_and_builds_expected_argv(self):
        runner = mock.MagicMock(return_value=result("  Open notepad please.  "))
        text, conf = self._rec(runner, threads=3).transcribe_wav(self.wav)
        self.assertEqual(text, "Open notepad please.")
        self.assertGreater(conf, 0.0)
        argv = runner.call_args[0][0]
        self.assertEqual(argv[0], str(self.bin))
        self.assertIn("-m", argv)
        self.assertIn(str(self.model), argv)
        self.assertIn(str(self.wav.resolve()), argv)
        self.assertIn("-nt", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "3")
        self.assertEqual(argv[argv.index("-l") + 1], "en")

    def test_non_speech_annotations_are_stripped(self):
        runner = mock.MagicMock(return_value=result("[BLANK_AUDIO] hello there (wind blowing)"))
        text, _ = self._rec(runner).transcribe_wav(self.wav)
        self.assertEqual(text, "hello there")

    def test_blank_audio_only_is_empty(self):
        runner = mock.MagicMock(return_value=result("[BLANK_AUDIO]"))
        self.assertEqual(self._rec(runner).transcribe_wav(self.wav), ("", 0.0))

    def test_failure_modes_return_empty(self):
        self.assertEqual(self._rec(mock.MagicMock(return_value=result("hi", 1)))
                         .transcribe_wav(self.wav), ("", 0.0))
        self.assertEqual(self._rec(mock.MagicMock(side_effect=OSError("boom")))
                         .transcribe_wav(self.wav), ("", 0.0))
        self.assertEqual(self._rec(mock.MagicMock()).transcribe_wav(self.wav.parent / "missing.wav"),
                         ("", 0.0))
        tiny = self.wav.parent / "tiny.wav"
        tiny.write_bytes(b"x" * 10)
        self.assertEqual(self._rec(mock.MagicMock()).transcribe_wav(tiny), ("", 0.0))

    def test_environment_overrides_defaults(self):
        with mock.patch.dict("os.environ", {"JARVIS_WHISPER_BIN": str(self.bin),
                                            "JARVIS_WHISPER_MODEL": str(self.model)}):
            rec = WhisperCppRecognizer()
        self.assertEqual(rec.binary, self.bin)
        self.assertEqual(rec.model, self.model)


class ModelSelectionTests(unittest.TestCase):
    """small.en is active, with base.en as the on-disk fallback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _resolve(self):
        with mock.patch.object(whisper_cpp, "MODEL_DIR", self.dir),              mock.patch.object(whisper_cpp, "DEFAULT_MODEL", self.dir / "ggml-small.en.bin"):
            return whisper_cpp.resolve_model()

    def test_prefers_small_when_both_present(self):
        (self.dir / "ggml-small.en.bin").write_bytes(b"ggml")
        (self.dir / "ggml-base.en.bin").write_bytes(b"ggml")
        self.assertEqual(self._resolve().name, "ggml-small.en.bin")

    def test_falls_back_to_base_when_small_missing(self):
        (self.dir / "ggml-base.en.bin").write_bytes(b"ggml")
        self.assertEqual(self._resolve().name, "ggml-base.en.bin")

    def test_no_model_reports_unavailable_so_sapi_takes_over(self):
        resolved = self._resolve()
        self.assertEqual(resolved.name, "ggml-small.en.bin")
        self.assertFalse(resolved.is_file())
        rec = WhisperCppRecognizer(binary=self.dir / "nope.exe", model=resolved)
        self.assertFalse(rec.is_available())


class CascadingRecognizerTests(unittest.TestCase):
    def test_primary_result_is_used_and_fallback_untouched(self):
        primary, fallback = FakeEngine("whisper text", 0.9), FakeEngine("sapi text", 0.3)
        casc = CascadingRecognizer(primary, fallback)
        self.assertEqual(casc.transcribe_wav("a.wav"), ("whisper text", 0.9))
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(casc.last_engine, "FakeEngine")

    def test_empty_primary_falls_back(self):
        primary, fallback = FakeEngine("", 0.0), FakeEngine("sapi text", 0.3)
        casc = CascadingRecognizer(primary, fallback)
        self.assertEqual(casc.transcribe_wav("a.wav"), ("sapi text", 0.3))
        self.assertEqual((primary.calls, fallback.calls), (1, 1))

    def test_raising_primary_falls_back(self):
        primary, fallback = FakeEngine(raises=True), FakeEngine("sapi text", 0.3)
        self.assertEqual(CascadingRecognizer(primary, fallback).transcribe_wav("a.wav"),
                         ("sapi text", 0.3))

    def test_unavailable_primary_is_not_called(self):
        primary, fallback = FakeEngine("x", 0.9, available=False), FakeEngine("sapi text", 0.3)
        self.assertEqual(CascadingRecognizer(primary, fallback).transcribe_wav("a.wav"),
                         ("sapi text", 0.3))
        self.assertEqual(primary.calls, 0)

    def test_both_unavailable_returns_empty(self):
        casc = CascadingRecognizer(FakeEngine(available=False), FakeEngine(available=False))
        self.assertFalse(casc.is_available())
        self.assertEqual(casc.transcribe_wav("a.wav"), ("", 0.0))
        self.assertEqual(casc.last_engine, "")

    def test_available_when_either_engine_is(self):
        self.assertTrue(CascadingRecognizer(FakeEngine(available=False), FakeEngine()).is_available())
        self.assertTrue(CascadingRecognizer(FakeEngine(), FakeEngine(available=False)).is_available())


if __name__ == "__main__":
    unittest.main()
