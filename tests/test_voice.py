"""Tests for AudioRecorder, SpeechRecognizer, and Voice PTT backend integration."""
import ctypes
import os
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import jarvis.integrations.audio_recorder as audio_recorder_module
from jarvis.integrations.audio_recorder import AudioRecorder
from jarvis.integrations.speech_recognizer import PS_RECOGNIZE_SCRIPT, SpeechRecognizer
from jarvis.ui.backend import Backend
from tests.qt_helpers import gui_app, pump


class AudioRecorderUnitTests(unittest.TestCase):
    def test_compute_rms(self):
        # Silence
        self.assertEqual(AudioRecorder.compute_rms(b""), 0.0)
        self.assertEqual(AudioRecorder.compute_rms(b"\x00\x00" * 100), 0.0)

        # Non-zero signal
        pcm = b"\x00\x10" * 100
        rms = AudioRecorder.compute_rms(pcm)
        self.assertGreater(rms, 0.0)
        self.assertLessEqual(rms, 1.0)

    def test_save_wav(self):
        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "test.wav"
            # 1600 samples (0.1s at 16kHz)
            pcm = b"\x00\x00" * 1600
            saved = AudioRecorder.save_wav(wav_path, pcm, sample_rate=16000)
            self.assertTrue(saved.is_file())
            with wave.open(str(saved), "rb") as wf:
                self.assertEqual(wf.getnchannels(), 1)
                self.assertEqual(wf.getsampwidth(), 2)
                self.assertEqual(wf.getframerate(), 16000)
                self.assertEqual(wf.getnframes(), 1600)

    def test_read_chunk_preserves_nul_bytes(self):
        '''The capture bug: PCM is full of NULs, and a c_char_p read stops at the first one.'''
        pcm = bytes([0, 0, 0x34, 0x12, 0, 0, 0xFF, 0x7F]) * 8
        buf = ctypes.create_string_buffer(pcm, len(pcm))
        self.assertEqual(AudioRecorder._read_chunk(buf, len(pcm)), pcm)

    def test_read_chunk_respects_length_and_rejects_empty(self):
        buf = ctypes.create_string_buffer(bytes([1, 2, 3, 4, 5, 6]), 6)
        self.assertEqual(AudioRecorder._read_chunk(buf, 4), bytes([1, 2, 3, 4]))
        self.assertEqual(AudioRecorder._read_chunk(buf, 0), b'')
        self.assertEqual(AudioRecorder._read_chunk(buf, -1), b'')

    def test_char_pointer_field_would_lose_the_audio(self):
        '''Why _read_chunk exists: reading a c_char_p field truncates at the first NUL, so
        ctypes.string_at(hdr.lpData, n) returned heap memory instead of the recording.'''
        pcm = bytes([0, 0, 0x34, 0x12]) * 16

        class Bad(ctypes.Structure):
            _fields_ = [('lpData', ctypes.c_char_p)]

        class Good(ctypes.Structure):
            _fields_ = [('lpData', ctypes.c_void_p)]

        buf = ctypes.create_string_buffer(pcm, len(pcm))
        bad = Bad()
        bad.lpData = ctypes.cast(buf, ctypes.c_char_p)
        self.assertEqual(len(bad.lpData), 0)          # the whole recording vanishes
        good = Good()
        good.lpData = ctypes.cast(buf, ctypes.c_void_p)
        self.assertIsInstance(good.lpData, int)       # a plain address, never truncated
        self.assertEqual(ctypes.string_at(good.lpData, len(pcm)), pcm)

    def test_capture_loop_never_reads_through_the_pointer(self):
        source = Path(audio_recorder_module.__file__).read_text(encoding='utf-8')
        self.assertNotIn('string_at', source)
        self.assertIn("('lpData', ctypes.c_void_p)".replace("'", '"'), source)
        self.assertNotIn("('lpData', ctypes.c_char_p)".replace("'", '"'), source)

    def test_start_and_stop_mock(self):
        rec = AudioRecorder()
        # In mock/test environment without winmm hardware, start/stop still operates cleanly
        with mock.patch.object(AudioRecorder, "is_available", return_value=False):
            started = rec.start()
            self.assertTrue(started)
            self.assertTrue(rec.is_recording)
            # Starting again while recording returns False
            self.assertFalse(rec.start())
            pcm = rec.stop()
            self.assertFalse(rec.is_recording)
            self.assertEqual(pcm, b"")


class SpeechRecognizerUnitTests(unittest.TestCase):
    def test_missing_wav_returns_empty(self):
        rec = SpeechRecognizer()
        text, conf = rec.transcribe_wav(Path("non_existent_file.wav"))
        self.assertEqual(text, "")
        self.assertEqual(conf, 0.0)

    def test_empty_wav_returns_empty(self):
        rec = SpeechRecognizer()
        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "empty.wav"
            wav_path.write_bytes(b"")
            text, conf = rec.transcribe_wav(wav_path)
            self.assertEqual(text, "")
            self.assertEqual(conf, 0.0)

    def test_successful_runner_parse(self):
        mock_output = '{"text": "volume up", "confidence": 0.985}'
        mock_runner = mock.MagicMock(return_value=SimpleNamespace(
            returncode=0, stdout=f"Some logs\n{mock_output}\n", stderr=""
        ))
        rec = SpeechRecognizer(runner=mock_runner)
        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "dummy.wav"
            wav_path.write_bytes(b"x" * 200)
            with mock.patch.object(SpeechRecognizer, "is_available", return_value=True):
                text, conf = rec.transcribe_wav(wav_path)
                self.assertEqual(text, "volume up")
                self.assertAlmostEqual(conf, 0.985, places=3)

    def test_runner_failure_returns_empty(self):
        mock_runner = mock.MagicMock(return_value=SimpleNamespace(
            returncode=1, stdout="", stderr="Error"
        ))
        rec = SpeechRecognizer(runner=mock_runner)
        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "dummy.wav"
            wav_path.write_bytes(b"x" * 200)
            with mock.patch.object(SpeechRecognizer, "is_available", return_value=True):
                text, conf = rec.transcribe_wav(wav_path)
                self.assertEqual(text, "")
                self.assertEqual(conf, 0.0)

    def test_wav_path_travels_in_the_environment(self):
        """powershell.exe -Command appends trailing arguments instead of binding them, so a
        -wavPath parameter never reached the script and every transcription came back empty."""
        captured = {}

        def runner(argv, **kw):
            captured["argv"], captured["env"] = argv, kw.get("env")
            return SimpleNamespace(returncode=0, stdout='{"text":"status","confidence":0.9}', stderr="")

        rec = SpeechRecognizer(runner=runner)
        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "dummy.wav"
            wav_path.write_bytes(b"x" * 200)
            with mock.patch.object(SpeechRecognizer, "is_available", return_value=True):
                text, conf = rec.transcribe_wav(wav_path)

        self.assertEqual((text, conf), ("status", 0.9))
        self.assertNotIn("-wavPath", captured["argv"])
        self.assertEqual(captured["env"]["JARVIS_WAV_PATH"], str(wav_path.resolve()))
        # The child needs a usable environment, not just the one variable.
        self.assertIn("PATH", {k.upper() for k in captured["env"]})

    def test_script_reads_the_path_from_the_environment(self):
        self.assertIn("$env:JARVIS_WAV_PATH", PS_RECOGNIZE_SCRIPT)
        self.assertNotIn("param(", PS_RECOGNIZE_SCRIPT)


class VoiceBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = gui_app()

    def test_voice_ptt_flow(self):
        with tempfile.TemporaryDirectory() as td:
            tasks_dir = Path(td) / "tasks"
            tasks_dir.mkdir()

            mock_recorder = mock.MagicMock()
            mock_recorder.start.return_value = True
            # Simulate 0.2s of audio (6400 bytes)
            mock_recorder.stop.return_value = b"\x00\x00" * 3200
            mock_recorder.current_level = 0.45

            mock_recognizer = mock.MagicMock()
            mock_recognizer.transcribe_wav.return_value = ("status", 0.95)

            b = Backend(tasks_dir=tasks_dir, audio_recorder=mock_recorder, speech_recognizer=mock_recognizer)
            try:
                self.assertFalse(b.voiceRecording)
                self.assertEqual(b.voiceLevel, 0.0)

                # Start recording
                b.startVoiceRecording()
                self.assertTrue(b.voiceRecording)
                self.assertEqual(b.orbMode, "listening")

                # Toggle to stop
                b.toggleVoiceRecording()
                self.assertFalse(b.voiceRecording)

                # Wait for transcription thread to deliver on UI thread and assistant to reply
                pump(lambda: any("CPU" in m["text"] for m in b.messages), timeout=2.0)

                # Recognized text "status" should have been executed by assistant
                self.assertTrue(any("CPU" in m["text"] for m in b.messages))
            finally:
                b.shutdown()


if __name__ == "__main__":
    unittest.main()
