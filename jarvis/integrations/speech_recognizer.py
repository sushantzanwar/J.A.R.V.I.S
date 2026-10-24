"""Offline speech recognition using Windows SAPI (System.Speech.Recognition).

Provides 100% offline, zero-budget, local voice recognition with:
1. Command Choices grammar (high confidence, <500ms response for voice commands).
2. DictationGrammar fallback (free-form vocabulary).
Zero external Python packages required. Uses the contained quiet runner.
"""
import json
import sys
from pathlib import Path

from jarvis.integrations.process import run_contained

COMMAND_PHRASES = [
    "volume up", "volume down", "mute", "unmute",
    "play", "pause", "play pause", "next track", "previous track", "stop",
    "open notepad", "open calculator", "open explorer", "open terminal",
    "open settings", "open task manager",
    "system status", "battery status", "uptime", "active window",
    "lock workstation", "lock computer",
    "who are you", "what time is it", "show tasks", "help", "status"
]

PS_RECOGNIZE_SCRIPT = r"""
param([string]$wavPath)
try {
    Add-Type -AssemblyName System.Speech
    $engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine

    $commands = @(
        'volume up', 'volume down', 'mute', 'unmute',
        'play', 'pause', 'play pause', 'next track', 'previous track', 'stop',
        'open notepad', 'open calculator', 'open explorer', 'open terminal',
        'open settings', 'open task manager',
        'system status', 'battery status', 'uptime', 'active window',
        'lock workstation', 'lock computer',
        'who are you', 'what time is it', 'show tasks', 'help', 'status'
    )
    $choices = New-Object System.Speech.Recognition.Choices
    $choices.Add([string[]]$commands)
    $gb = New-Object System.Speech.Recognition.GrammarBuilder($choices)
    $cmdGrammar = New-Object System.Speech.Recognition.Grammar($gb)
    $cmdGrammar.Name = 'Commands'
    $cmdGrammar.Weight = 1.0
    $engine.LoadGrammar($cmdGrammar)

    try {
        $dictGrammar = New-Object System.Speech.Recognition.DictationGrammar
        $dictGrammar.Name = 'Dictation'
        $dictGrammar.Weight = 0.5
        $engine.LoadGrammar($dictGrammar)
    } catch {}

    $engine.SetInputToWaveFile($wavPath)
    $res = $engine.Recognize()
    if ($res -ne $null -and $res.Text) {
        $out = @{ text = $res.Text; confidence = [double]$res.Confidence }
    } else {
        $out = @{ text = ''; confidence = 0.0 }
    }
    Write-Output (ConvertTo-Json $out -Compress)
} catch {
    Write-Output '{"text":"","confidence":0.0}'
}
"""


class SpeechRecognizer:
    def __init__(self, runner=run_contained):
        self._runner = runner

    @staticmethod
    def is_available() -> bool:
        return sys.platform == "win32"

    def transcribe_wav(self, wav_path: Path | str, timeout: float = 6.0) -> tuple[str, float]:
        """Transcribe a WAV file offline using Windows SAPI SpeechRecognitionEngine.

        Returns (recognized_text, confidence_score_between_0_and_1).
        """
        wav = Path(wav_path).resolve()
        if not wav.is_file() or wav.stat().st_size < 100:
            return "", 0.0

        if not self.is_available():
            return "", 0.0

        argv = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-Command", PS_RECOGNIZE_SCRIPT,
            "-wavPath", str(wav)
        ]

        try:
            res = self._runner(argv, timeout=timeout)
            if res.returncode != 0:
                return "", 0.0
            # Scan lines in stdout from the bottom for the JSON output
            for line in reversed(res.stdout.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    data = json.loads(line)
                    text = str(data.get("text", "")).strip()
                    confidence = float(data.get("confidence", 0.0))
                    return text, confidence
            return "", 0.0
        except Exception:
            return "", 0.0
