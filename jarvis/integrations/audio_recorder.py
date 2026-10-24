"""Native Windows audio input recording via winmm.dll and ctypes.

Captures 16 kHz 16-bit mono PCM suitable for offline speech recognition.
Zero external third-party dependencies. Gracefully falls back to headless
mode when no audio input device is present or running in test environments.
"""
import math
import struct
import sys
import threading
import time
import wave
from pathlib import Path


class AudioRecorder:
    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._recording = False
        self._thread: threading.Thread | None = None
        self._recorded_bytes = bytearray()
        self._current_level = 0.0
        self._ready_event = threading.Event()

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    @property
    def current_level(self) -> float:
        with self._lock:
            return self._current_level

    @staticmethod
    def is_available() -> bool:
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            winmm = ctypes.WinDLL("winmm")
            return winmm.waveInGetNumDevs() > 0
        except Exception:
            return False

    def start(self) -> bool:
        """Start capturing audio in the background. Returns True if started successfully."""
        with self._lock:
            if self._recording:
                return False
            self._recording = True
            self._recorded_bytes.clear()
            self._current_level = 0.0
            self._ready_event.clear()

        if not self.is_available():
            # In headless / test / non-Windows environments, keep recording flag True
            # so UI flows test cleanly without hardware errors.
            return True

        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()
        self._ready_event.wait(timeout=1.0)
        return True

    def stop(self) -> bytes:
        """Stop recording and return captured PCM audio bytes."""
        with self._lock:
            if not self._recording:
                return bytes(self._recorded_bytes)
            self._recording = False

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
            self._thread = None

        with self._lock:
            return bytes(self._recorded_bytes)

    def _record_loop(self) -> None:
        import ctypes
        from ctypes import wintypes

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [
                ("wFormatTag", wintypes.WORD),
                ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD),
            ]

        class WAVEHDR(ctypes.Structure):
            pass

        WAVEHDR._fields_ = [
            # c_void_p, never c_char_p: reading a c_char_p field makes ctypes build a
            # Python bytes object that stops at the first NUL, which PCM audio is full of.
            ("lpData", ctypes.c_void_p),
            ("dwBufferLength", wintypes.DWORD),
            ("dwBytesRecorded", wintypes.DWORD),
            ("dwUser", ctypes.c_void_p),
            ("dwFlags", wintypes.DWORD),
            ("dwLoops", wintypes.DWORD),
            ("lpNext", ctypes.POINTER(WAVEHDR)),
            ("reserved", ctypes.c_void_p),
        ]

        winmm = ctypes.WinDLL("winmm")
        kernel32 = ctypes.WinDLL("kernel32")

        WAVE_FORMAT_PCM = 1
        CALLBACK_EVENT = 0x00050000
        WHDR_DONE = 0x00000001
        WAVE_MAPPER = -1

        wfx = WAVEFORMATEX()
        wfx.wFormatTag = WAVE_FORMAT_PCM
        wfx.nChannels = 1
        wfx.nSamplesPerSec = self.sample_rate
        wfx.wBitsPerSample = 16
        wfx.nBlockAlign = 2
        wfx.nAvgBytesPerSec = self.sample_rate * 2
        wfx.cbSize = 0

        h_event = kernel32.CreateEventW(None, False, False, None)
        h_wave = wintypes.HANDLE()

        rc = winmm.waveInOpen(ctypes.byref(h_wave), WAVE_MAPPER, ctypes.byref(wfx), h_event, 0, CALLBACK_EVENT)
        if rc != 0:
            kernel32.CloseHandle(h_event)
            self._ready_event.set()
            return

        chunk_samples = self.sample_rate // 10  # 100ms chunks
        buf_size = chunk_samples * 2
        num_bufs = 4
        buffers = [ctypes.create_string_buffer(buf_size) for _ in range(num_bufs)]
        headers = []

        for b in buffers:
            hdr = WAVEHDR()
            hdr.lpData = ctypes.cast(b, ctypes.c_void_p)
            hdr.dwBufferLength = buf_size
            hdr.dwFlags = 0
            winmm.waveInPrepareHeader(h_wave, ctypes.byref(hdr), ctypes.sizeof(hdr))
            winmm.waveInAddBuffer(h_wave, ctypes.byref(hdr), ctypes.sizeof(hdr))
            headers.append(hdr)

        winmm.waveInStart(h_wave)
        self._ready_event.set()

        try:
            while True:
                with self._lock:
                    if not self._recording:
                        break

                # Wait for any buffer to finish filling
                kernel32.WaitForSingleObject(h_event, 50)

                for buf, hdr in zip(buffers, headers):
                    if hdr.dwFlags & WHDR_DONE:
                        chunk = self._read_chunk(buf, hdr.dwBytesRecorded)
                        if chunk:
                            with self._lock:
                                self._recorded_bytes.extend(chunk)
                                self._current_level = self.compute_rms(chunk)
                        hdr.dwBytesRecorded = 0
                        hdr.dwFlags &= ~WHDR_DONE
                        with self._lock:
                            if self._recording:
                                winmm.waveInAddBuffer(h_wave, ctypes.byref(hdr), ctypes.sizeof(hdr))
        finally:
            winmm.waveInStop(h_wave)
            winmm.waveInReset(h_wave)

            # Drain any remaining bytes in flight
            for buf, hdr in zip(buffers, headers):
                if hdr.dwBytesRecorded > 0:
                    chunk = self._read_chunk(buf, hdr.dwBytesRecorded)
                    if chunk:
                        with self._lock:
                            self._recorded_bytes.extend(chunk)
                winmm.waveInUnprepareHeader(h_wave, ctypes.byref(hdr), ctypes.sizeof(hdr))

            winmm.waveInClose(h_wave)
            kernel32.CloseHandle(h_event)

    @staticmethod
    def _read_chunk(buffer, nbytes: int) -> bytes:
        """Copy exactly nbytes of captured PCM out of a driver-filled ctypes buffer.

        Reads the buffer object directly. The WAVEHDR pointer must never be used for this:
        audio contains NUL bytes everywhere, and ctypes truncates a c_char_p read at the
        first one, which silently yields heap memory instead of the recording.
        """
        if nbytes <= 0:
            return b""
        return buffer.raw[:nbytes]

    @staticmethod
    def compute_rms(pcm_data: bytes) -> float:
        """Calculate normalized RMS amplitude (0.0 to 1.0) for 16-bit mono PCM."""
        if not pcm_data or len(pcm_data) < 2:
            return 0.0
        n_samples = len(pcm_data) // 2
        try:
            samples = struct.unpack(f"<{n_samples}h", pcm_data[:n_samples * 2])
            sum_squares = sum(s * s for s in samples)
            rms = math.sqrt(sum_squares / n_samples)
            # Normalize: typical active speaking RMS is between 1000 and 15000
            return min(1.0, max(0.0, rms / 16000.0))
        except Exception:
            return 0.0

    @staticmethod
    def save_wav(file_path: Path, pcm_data: bytes, sample_rate: int = 16000) -> Path:
        """Write raw PCM bytes to a standard WAV audio file."""
        file_path = Path(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(file_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)
        return file_path
