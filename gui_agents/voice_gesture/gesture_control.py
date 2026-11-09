"""
Webcam gesture control for hands-free agent interaction.

Recognized gestures and their mapped commands:

  Open palm  (all 5 fingers extended)   →  PAUSE_AGENT
  Closed fist (all fingers folded)       →  RESUME_AGENT
  Thumbs up   (thumb up, rest folded)    →  APPROVE_ACTION
  Thumbs down (thumb down, rest folded)  →  CANCEL_TASK
  Peace / V   (index + middle up only)   →  SCREENSHOT
  Point up    (index finger only up)     →  NEXT_STEP

Usage:
    gc = GestureControl(camera_index=0, show_preview=True)
    gc.start()
    # In agent loop:
    cmd = gc.get_command(timeout=0)  # non-blocking poll
    gc.stop()
"""

import enum
import logging
import queue
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy imports
_cv2 = None
_mp = None


def _import_cv2():
    global _cv2
    if _cv2 is None:
        try:
            import cv2
            _cv2 = cv2
        except ImportError:
            raise ImportError(
                "opencv-python is required for gesture mode. "
                "Install with: pip install opencv-python"
            )
    return _cv2


def _import_mp():
    global _mp
    if _mp is None:
        try:
            import mediapipe as mp
            _mp = mp
        except ImportError:
            raise ImportError(
                "mediapipe is required for gesture mode. "
                "Install with: pip install mediapipe"
            )
    return _mp


class GestureCommand(enum.Enum):
    PAUSE_AGENT = "pause"
    RESUME_AGENT = "resume"
    APPROVE_ACTION = "approve"
    CANCEL_TASK = "cancel"
    SCREENSHOT = "screenshot"
    NEXT_STEP = "next"


# Human-readable labels shown on the preview window
_COMMAND_LABELS = {
    GestureCommand.PAUSE_AGENT: "PAUSE  ✋",
    GestureCommand.RESUME_AGENT: "RESUME ✊",
    GestureCommand.APPROVE_ACTION: "APPROVE 👍",
    GestureCommand.CANCEL_TASK: "CANCEL 👎",
    GestureCommand.SCREENSHOT: "SCREENSHOT ✌️",
    GestureCommand.NEXT_STEP: "NEXT ☝️",
}


def _fingers_extended(landmarks, handedness: str = "Right") -> list[bool]:
    """
    Return a 5-element list [thumb, index, middle, ring, pinky] where True = extended.
    Uses y-axis comparison for index-pinky and angle for thumb (flipped for left hand).
    """
    lm = landmarks.landmark

    # Finger tip and PIP (proximal interphalangeal) landmark indices
    # Thumb: 4=tip, 3=IP, 2=MCP
    # Index: 8=tip, 6=PIP
    # Middle: 12=tip, 10=PIP
    # Ring: 16=tip, 14=PIP
    # Pinky: 20=tip, 18=PIP

    fingers = []

    # Thumb — compare x-axis (flipped for left hand)
    if handedness == "Right":
        fingers.append(lm[4].x < lm[3].x)
    else:
        fingers.append(lm[4].x > lm[3].x)

    # Index → Pinky — compare y-axis (tip above PIP)
    for tip, pip in [(8, 6), (12, 10), (16, 14), (20, 18)]:
        fingers.append(lm[tip].y < lm[pip].y)

    return fingers  # [thumb, index, middle, ring, pinky]


def _thumb_direction(landmarks) -> str:
    """Return 'up', 'down', or 'neutral' based on thumb tip vs wrist y."""
    lm = landmarks.landmark
    wrist_y = lm[0].y
    thumb_tip_y = lm[4].y
    # Positive y = lower on screen
    if thumb_tip_y < wrist_y - 0.1:
        return "up"
    if thumb_tip_y > wrist_y + 0.1:
        return "down"
    return "neutral"


def _classify_gesture(landmarks, handedness: str = "Right") -> Optional[GestureCommand]:
    fingers = _fingers_extended(landmarks, handedness)
    thumb, index, middle, ring, pinky = fingers
    n_extended = sum(fingers)

    if n_extended == 5:
        return GestureCommand.PAUSE_AGENT

    if n_extended == 0:
        return GestureCommand.RESUME_AGENT

    if not index and not middle and not ring and not pinky:
        # Only thumb — check direction
        direction = _thumb_direction(landmarks)
        if direction == "up":
            return GestureCommand.APPROVE_ACTION
        if direction == "down":
            return GestureCommand.CANCEL_TASK

    if index and middle and not ring and not pinky:
        return GestureCommand.SCREENSHOT

    if index and not middle and not ring and not pinky:
        return GestureCommand.NEXT_STEP

    return None


class GestureControl:
    """
    Background thread that reads webcam frames, detects hand gestures via
    MediaPipe Hands, and posts GestureCommand values to an internal queue.

    Args:
        camera_index: OpenCV camera device index (default 0).
        show_preview: Display a live preview window with gesture label overlay.
        debounce_secs: Minimum seconds between repeated identical commands.
        confidence: Minimum detection/tracking confidence for MediaPipe Hands.
    """

    def __init__(
        self,
        camera_index: int = 0,
        show_preview: bool = True,
        debounce_secs: float = 1.5,
        confidence: float = 0.7,
    ):
        self.camera_index = camera_index
        self.show_preview = show_preview
        self.debounce_secs = debounce_secs
        self.confidence = confidence

        self._cmd_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the background gesture detection thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="gesture-ctrl")
        self._thread.start()
        logger.info("Gesture control started.")

    def stop(self):
        """Stop the detection thread and release the webcam."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    def get_command(self, timeout: float = 0) -> Optional[GestureCommand]:
        """
        Return the next pending gesture command, or None if the queue is empty.
        Set timeout > 0 to block waiting for a command.
        """
        try:
            return self._cmd_queue.get(block=timeout > 0, timeout=timeout or None)
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # Internal detection loop
    # ------------------------------------------------------------------

    def _run(self):
        cv2 = _import_cv2()
        mp = _import_mp()
        mp_hands = mp.solutions.hands
        mp_draw = mp.solutions.drawing_utils

        cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            logger.error(f"Cannot open camera index {self.camera_index}.")
            return

        last_cmd: Optional[GestureCommand] = None
        last_cmd_time: float = 0.0
        current_label: str = ""

        with mp_hands.Hands(
            max_num_hands=1,
            min_detection_confidence=self.confidence,
            min_tracking_confidence=self.confidence,
        ) as hands:
            while not self._stop_event.is_set():
                ret, frame = cap.read()
                if not ret:
                    logger.warning("Webcam frame dropped.")
                    time.sleep(0.05)
                    continue

                frame = cv2.flip(frame, 1)  # mirror so it feels natural
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                result = hands.process(rgb)

                detected: Optional[GestureCommand] = None
                if result.multi_hand_landmarks:
                    for hand_lm, hand_info in zip(
                        result.multi_hand_landmarks,
                        result.multi_handedness,
                    ):
                        handedness = hand_info.classification[0].label  # "Left" or "Right"
                        detected = _classify_gesture(hand_lm, handedness)

                        if self.show_preview:
                            mp_draw.draw_landmarks(
                                frame, hand_lm, mp_hands.HAND_CONNECTIONS
                            )
                        break  # single hand

                if detected is not None:
                    now = time.time()
                    # Debounce: ignore if same command fired recently
                    if detected != last_cmd or (now - last_cmd_time) >= self.debounce_secs:
                        self._cmd_queue.put(detected)
                        last_cmd = detected
                        last_cmd_time = now
                        current_label = _COMMAND_LABELS[detected]
                        logger.info(f"Gesture: {detected.name}")
                else:
                    # Fade label after 2 s of no gesture
                    if time.time() - last_cmd_time > 2:
                        current_label = ""

                if self.show_preview:
                    h, w = frame.shape[:2]
                    cv2.rectangle(frame, (0, 0), (w, 60), (0, 0, 0), -1)
                    cv2.putText(
                        frame,
                        current_label or "Show a gesture…",
                        (12, 42),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        1.1,
                        (0, 255, 120) if current_label else (120, 120, 120),
                        2,
                        cv2.LINE_AA,
                    )
                    cv2.imshow("Agent-S Gesture Control", frame)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        self._stop_event.set()
                        break

        cap.release()
        if self.show_preview:
            cv2.destroyAllWindows()
        logger.info("Gesture control stopped.")
