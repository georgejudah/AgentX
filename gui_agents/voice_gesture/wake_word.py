"""
Always-on wake word listener for hands-free agent activation.

Listens continuously on the microphone. When the wake phrase is heard
(default: "hey agent") it fires a callback so the agent loop can prompt
for the next task — without the user ever touching the keyboard.

Two backends, tried in order:
  1. pvporcupine  — ultra-low CPU (~1%), works offline, very accurate.
                    Needs a free Picovoice access key (picovoice.ai/console).
                    Set PICOVOICE_ACCESS_KEY env var or pass access_key=.
  2. vosk         — fully offline, no account needed, downloads a ~50 MB model
                    on first run.  Slightly more CPU than porcupine.
  3. fallback     — SpeechRecognition + Google STT. Requires internet. Least
                    efficient but zero extra setup.

Usage:
    def on_wake():
        print("Wake word heard!")

    ww = WakeWordListener(on_wake=on_wake, phrase="hey agent")
    ww.start()   # non-blocking background thread
    ...
    ww.stop()
"""

import logging
import os
import queue
import struct
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PHRASE = "hey agent"


# ---------------------------------------------------------------------------
# Backend: pvporcupine
# ---------------------------------------------------------------------------

def _try_porcupine(phrase: str, access_key: str, on_wake: Callable, stop_event: threading.Event):
    try:
        import pvporcupine
        import pyaudio
    except ImportError:
        return False

    key = access_key or os.environ.get("PICOVOICE_ACCESS_KEY", "")
    if not key:
        logger.debug("pvporcupine available but no PICOVOICE_ACCESS_KEY — skipping.")
        return False

    # Map common phrases to built-in keywords; fall back to "hey google" lookalike
    BUILTIN = {
        "hey agent": "hey google",
        "hey siri": "hey siri",
        "alexa": "alexa",
        "computer": "computer",
        "jarvis": "jarvis",
        "ok google": "ok google",
        "hey google": "hey google",
    }
    keyword = BUILTIN.get(phrase.lower().strip(), "hey google")

    try:
        porcupine = pvporcupine.create(access_key=key, keywords=[keyword])
    except Exception as e:
        logger.warning(f"porcupine init failed: {e}")
        return False

    pa = pyaudio.PyAudio()
    stream = pa.open(
        rate=porcupine.sample_rate,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=porcupine.frame_length,
    )

    logger.info(f"[wake-word] porcupine listening for '{keyword}' (phrase='{phrase}')")
    print(f"🎤  Wake word active — say \"{phrase}\" to start the agent.")

    try:
        while not stop_event.is_set():
            pcm = stream.read(porcupine.frame_length, exception_on_overflow=False)
            pcm_unpacked = struct.unpack_from("h" * porcupine.frame_length, pcm)
            idx = porcupine.process(pcm_unpacked)
            if idx >= 0:
                logger.info("Wake word detected (porcupine).")
                on_wake()
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()
        porcupine.delete()

    return True


# ---------------------------------------------------------------------------
# Backend: vosk
# ---------------------------------------------------------------------------

def _try_vosk(phrase: str, on_wake: Callable, stop_event: threading.Event):
    try:
        import vosk
        import pyaudio
    except ImportError:
        return False

    model_path = os.environ.get("VOSK_MODEL_PATH", "")
    if not model_path:
        # Auto-download small English model on first run
        import urllib.request, zipfile, pathlib

        model_dir = pathlib.Path.home() / ".cache" / "agentx" / "vosk-model-small-en"
        if not model_dir.exists():
            url = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
            zip_path = model_dir.parent / "vosk-model-small-en.zip"
            model_dir.parent.mkdir(parents=True, exist_ok=True)
            print("📥  Downloading Vosk small English model (~50 MB)…")
            urllib.request.urlretrieve(url, zip_path)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(model_dir.parent)
            # Find extracted dir
            extracted = next(model_dir.parent.glob("vosk-model-small-en-us*"))
            extracted.rename(model_dir)
            zip_path.unlink()
            print("✅  Vosk model ready.")
        model_path = str(model_dir)

    try:
        vosk.SetLogLevel(-1)
        model = vosk.Model(model_path)
    except Exception as e:
        logger.warning(f"vosk model load failed: {e}")
        return False

    pa = pyaudio.PyAudio()
    sample_rate = 16000
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=sample_rate,
        input=True,
        frames_per_buffer=8000,
    )

    rec = vosk.KaldiRecognizer(model, sample_rate)
    tokens = set(phrase.lower().split())

    logger.info(f"[wake-word] vosk listening for '{phrase}'")
    print(f"🎤  Wake word active — say \"{phrase}\" to start the agent.")

    try:
        while not stop_event.is_set():
            data = stream.read(4000, exception_on_overflow=False)
            if rec.AcceptWaveform(data):
                import json
                result = json.loads(rec.Result()).get("text", "").lower()
                if result and tokens.issubset(set(result.split())):
                    logger.info(f"Wake phrase detected (vosk): '{result}'")
                    on_wake()
            else:
                partial = json.loads(rec.PartialResult()).get("partial", "").lower()
                if partial and tokens.issubset(set(partial.split())):
                    logger.info(f"Wake phrase detected in partial (vosk): '{partial}'")
                    rec.Reset()
                    on_wake()
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

    return True


# ---------------------------------------------------------------------------
# Backend: SpeechRecognition fallback
# ---------------------------------------------------------------------------

def _sr_fallback(phrase: str, on_wake: Callable, stop_event: threading.Event):
    try:
        import speech_recognition as sr
    except ImportError:
        logger.error("No wake-word backend available. Install pvporcupine, vosk, or SpeechRecognition.")
        return

    recognizer = sr.Recognizer()
    recognizer.energy_threshold = 300
    recognizer.dynamic_energy_threshold = True
    recognizer.pause_threshold = 0.6

    tokens = set(phrase.lower().split())
    print(f"🎤  Wake word active (SR fallback) — say \"{phrase}\" to start the agent.")

    while not stop_event.is_set():
        try:
            with sr.Microphone() as source:
                try:
                    audio = recognizer.listen(source, timeout=5, phrase_time_limit=5)
                except sr.WaitTimeoutError:
                    continue
            try:
                text = recognizer.recognize_google(audio).lower()
            except (sr.UnknownValueError, sr.RequestError):
                continue
            if tokens.issubset(set(text.split())):
                logger.info(f"Wake phrase detected (SR): '{text}'")
                on_wake()
        except Exception as e:
            logger.debug(f"SR wake loop error: {e}")


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class WakeWordListener:
    """
    Always-on microphone listener that fires on_wake() when the wake phrase
    is detected. Tries backends in order: porcupine → vosk → SR fallback.

    Args:
        on_wake: Callable fired (in background thread) when wake phrase heard.
        phrase:  The phrase to listen for, e.g. "hey agent".
        access_key: Picovoice access key (or set PICOVOICE_ACCESS_KEY env var).
    """

    def __init__(
        self,
        on_wake: Callable,
        phrase: str = _DEFAULT_PHRASE,
        access_key: str = "",
    ):
        self._on_wake = on_wake
        self._phrase = phrase
        self._access_key = access_key
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="wake-word"
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        # Try best backends first
        if _try_porcupine(self._phrase, self._access_key, self._on_wake, self._stop_event):
            return
        if _try_vosk(self._phrase, self._on_wake, self._stop_event):
            return
        _sr_fallback(self._phrase, self._on_wake, self._stop_event)
