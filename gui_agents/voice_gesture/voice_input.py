"""
Voice input/output for hands-free agent control.

Usage:
    vi = VoiceInput(tts=True)
    query = vi.listen()          # blocks until speech detected, returns text
    vi.speak("Task complete.")   # optional TTS feedback
"""

import logging
import queue
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy imports so the module loads even without optional deps installed.
_sr = None
_pyttsx3 = None


def _import_sr():
    global _sr
    if _sr is None:
        try:
            import speech_recognition as sr
            _sr = sr
        except ImportError:
            raise ImportError(
                "speech_recognition is required for voice mode. "
                "Install it with: pip install SpeechRecognition pyaudio"
            )
    return _sr


def _import_pyttsx3():
    global _pyttsx3
    if _pyttsx3 is None:
        try:
            import pyttsx3
            _pyttsx3 = pyttsx3
        except ImportError:
            logger.warning("pyttsx3 not found — TTS disabled. pip install pyttsx3")
            _pyttsx3 = False
    return _pyttsx3


class VoiceInput:
    """
    Microphone-to-text using Google Web Speech API (free, needs internet)
    with Whisper (openai-whisper) as a local offline fallback.

    Args:
        tts: Enable text-to-speech feedback.
        whisper_model: Whisper model size to use if Google fails offline mode.
                       Set to None to disable Whisper fallback entirely.
        energy_threshold: Microphone energy threshold for speech detection.
        pause_threshold: Seconds of silence before considering speech done.
        language: BCP-47 language tag for recognition, e.g. "en-US".
    """

    def __init__(
        self,
        tts: bool = True,
        whisper_model: Optional[str] = "base",
        energy_threshold: int = 300,
        pause_threshold: float = 1.0,
        language: str = "en-US",
    ):
        self.tts_enabled = tts
        self.whisper_model_name = whisper_model
        self.language = language

        sr = _import_sr()
        self._recognizer = sr.Recognizer()
        self._recognizer.energy_threshold = energy_threshold
        self._recognizer.pause_threshold = pause_threshold
        self._recognizer.dynamic_energy_threshold = True

        self._whisper_model = None
        self._tts_engine = None
        self._tts_queue: queue.Queue = queue.Queue()
        self._tts_thread: Optional[threading.Thread] = None

        if tts:
            self._init_tts()

    # ------------------------------------------------------------------
    # TTS
    # ------------------------------------------------------------------

    def _init_tts(self):
        pyttsx3 = _import_pyttsx3()
        if not pyttsx3:
            self.tts_enabled = False
            return
        try:
            self._tts_engine = pyttsx3.init()
            self._tts_engine.setProperty("rate", 175)
            self._tts_engine.setProperty("volume", 0.9)
            self._tts_thread = threading.Thread(target=self._tts_worker, daemon=True)
            self._tts_thread.start()
            logger.info("TTS initialized.")
        except Exception as e:
            logger.warning(f"TTS init failed ({e}) — TTS disabled.")
            self.tts_enabled = False

    def _tts_worker(self):
        while True:
            text = self._tts_queue.get()
            if text is None:
                break
            try:
                self._tts_engine.say(text)
                self._tts_engine.runAndWait()
            except Exception as e:
                logger.warning(f"TTS error: {e}")
            self._tts_queue.task_done()

    def speak(self, text: str):
        """Queue text for speech output (non-blocking)."""
        if self.tts_enabled and self._tts_engine is not None:
            self._tts_queue.put(text)
        else:
            print(f"[Agent]: {text}")

    # ------------------------------------------------------------------
    # STT
    # ------------------------------------------------------------------

    def _init_whisper(self):
        if self._whisper_model is not None:
            return
        if self.whisper_model_name is None:
            return
        try:
            import whisper
            logger.info(f"Loading Whisper model '{self.whisper_model_name}'…")
            self._whisper_model = whisper.load_model(self.whisper_model_name)
            logger.info("Whisper ready.")
        except ImportError:
            logger.warning("openai-whisper not installed — offline fallback unavailable.")

    def listen(self, prompt: str = "Listening…") -> Optional[str]:
        """
        Block until speech is captured and return the transcribed text.
        Returns None if recognition failed.
        """
        sr = _import_sr()
        with sr.Microphone() as source:
            print(f"\n🎙️  {prompt}")
            self._recognizer.adjust_for_ambient_noise(source, duration=0.5)
            try:
                audio = self._recognizer.listen(source, timeout=10, phrase_time_limit=30)
            except sr.WaitTimeoutError:
                print("⏱  No speech detected (timeout).")
                return None

        return self._transcribe(audio)

    def _transcribe(self, audio) -> Optional[str]:
        sr = _import_sr()
        # Try Google first (fast, free, online)
        try:
            text = self._recognizer.recognize_google(audio, language=self.language)
            print(f"🗣  You said: {text}")
            return text
        except sr.UnknownValueError:
            print("❓ Could not understand audio.")
        except sr.RequestError as e:
            logger.warning(f"Google STT unavailable ({e}), trying Whisper…")

        # Whisper fallback (offline)
        self._init_whisper()
        if self._whisper_model is not None:
            import io
            import tempfile

            import whisper

            wav_bytes = audio.get_wav_data()
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_bytes)
                tmp_path = f.name
            result = self._whisper_model.transcribe(tmp_path)
            import os
            os.unlink(tmp_path)
            text = result.get("text", "").strip()
            if text:
                print(f"🗣  You said (Whisper): {text}")
                return text

        return None

    def listen_loop(self, callback, stop_event: threading.Event):
        """
        Continuously listen and call callback(text) until stop_event is set.
        Meant to run in a background thread.
        """
        while not stop_event.is_set():
            text = self.listen(prompt="Say your command (or task)…")
            if text:
                callback(text)

    def close(self):
        if self._tts_queue is not None:
            self._tts_queue.put(None)
