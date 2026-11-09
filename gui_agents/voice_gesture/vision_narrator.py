"""
Vision-based step narration using BehaviorNarrator.

Instead of converting pyautogui code to speech via regex, this module
shows the LLM the BEFORE and AFTER screenshots and asks it to describe
what visually changed. The result is richer and more accurate:

  Regex narrator:  "Clicking the screen."
  Vision narrator: "Clicked the Search button in Gmail's compose toolbar,
                    which opened the recipient suggestion dropdown."

The LLM call runs in a background thread so the agent loop is never
blocked waiting for narration. The description is spoken when ready
(typically arrives 1-3 s after the step executes).

Usage:
    vn = VisionNarrator(engine_params, voice_input)
    vn.start()

    # At the START of each step, capture the before-shot:
    vn.set_before(screenshot_bytes)

    # After agent.predict() returns and the action is executed:
    vn.narrate_step(step, code, after_screenshot_bytes)

    vn.stop()
"""

import logging
import threading
import queue
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from gui_agents.voice_gesture.voice_input import VoiceInput

logger = logging.getLogger(__name__)


class VisionNarrator:
    """
    Async wrapper around BehaviorNarrator that speaks step descriptions
    without blocking the agent execution loop.

    Args:
        engine_params: Same dict passed to AgentS3 — used to init BehaviorNarrator.
        voice: VoiceInput instance for TTS output.
        fallback_narrator: Optional Narrator instance used when vision narration
                           is still running (speaks the regex description immediately
                           while the vision description catches up).
    """

    def __init__(
        self,
        engine_params: dict,
        voice: "VoiceInput",
        fallback_narrator=None,
    ):
        self._engine_params = engine_params
        self._voice = voice
        self._fallback = fallback_narrator

        self._before_screenshot: Optional[bytes] = None
        self._work_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._narrator_instance = None  # lazy-init BehaviorNarrator

    def start(self):
        if self._worker and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._run, daemon=True, name="vision-narrator"
        )
        self._worker.start()
        logger.info("VisionNarrator started.")

    def stop(self):
        self._stop_event.set()
        self._work_queue.put(None)  # unblock worker
        if self._worker:
            self._worker.join(timeout=5)

    def set_before(self, screenshot_bytes: bytes):
        """Call at the beginning of each step, before agent.predict()."""
        self._before_screenshot = screenshot_bytes

    def narrate_step(self, step: int, code: list, after_screenshot_bytes: bytes):
        """
        Enqueue a vision-narration job for the just-executed step.
        Returns immediately; narration arrives asynchronously via TTS.
        """
        if not code or not self._before_screenshot:
            return

        action = code[0].strip()

        # Speak regex fallback immediately so there's no silent gap
        if self._fallback:
            self._fallback.announce_step(step, {}, code)

        job = {
            "step": step,
            "action": action,
            "before": self._before_screenshot,
            "after": after_screenshot_bytes,
        }
        self._work_queue.put(job)
        self._before_screenshot = None  # consumed

    def narrate_done(self):
        self._voice.speak("Task complete.")

    def narrate_fail(self):
        self._voice.speak("The task could not be completed.")

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _get_narrator(self):
        if self._narrator_instance is None:
            from gui_agents.s3.bbon.behavior_narrator import BehaviorNarrator
            self._narrator_instance = BehaviorNarrator(self._engine_params)
        return self._narrator_instance

    def _run(self):
        while not self._stop_event.is_set():
            try:
                job = self._work_queue.get(timeout=1)
            except queue.Empty:
                continue

            if job is None:
                break

            step = job["step"]
            action = job["action"]
            before = job["before"]
            after = job["after"]

            try:
                narrator = self._get_narrator()
                result = narrator.judge(
                    screenshot_num=step + 1,
                    before_img_bytes=before,
                    after_img_bytes=after,
                    pyautogui_action=action,
                )
                description = result.get("fact_answer", "").strip()
                # Strip the "Fact Caption from Screenshot N:" prefix for speech
                if ":" in description:
                    description = description.split(":", 1)[-1].strip()
                if description:
                    logger.info(f"Vision narration step {step + 1}: {description[:80]}…")
                    self._voice.speak(description)
            except Exception as e:
                logger.warning(f"VisionNarrator step {step + 1} failed: {e}")
