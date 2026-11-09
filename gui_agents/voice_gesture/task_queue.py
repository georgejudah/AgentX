"""
Task queue with multi-task voice parsing.

Accepts natural-language input that may contain multiple chained tasks
("first reply to Sarah, then open my calendar, then order lunch") and
splits it into a sequential queue using the same LLM already running.

Usage:
    tq = TaskQueue(engine_params)
    tq.enqueue("open Chrome and check my email")          # → 1 task
    tq.enqueue("first do X, then do Y, then do Z")        # → 3 tasks
    while not tq.empty():
        task, idx, total = tq.next()
        print(f"Running task {idx}/{total}: {task}")
"""

import json
import logging
import queue
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Heuristic fast-path: detect multi-task phrasing without an LLM call
# ---------------------------------------------------------------------------

_SPLIT_PATTERNS = [
    r"\bfirst\b.+?\bthen\b",
    r"\band then\b",
    r"\bafter that\b",
    r"\bafterward[s]?\b",
    r"\bnext[,\s]",
    r"\bfinally\b",
    r"\bsecond(?:ly)?\b",
    r"\bthird(?:ly)?\b",
    r"\bstep \d",
    r"\btask \d",
]
_SPLIT_RE = re.compile("|".join(_SPLIT_PATTERNS), re.IGNORECASE)

_SPLIT_PROMPT = """\
You are a task parser. Split the user's instruction into individual, \
self-contained tasks. If it is a single task return it as-is.

Return ONLY a JSON array of strings, one element per task, no other text.
Example input:  "first open Chrome, then search for flights to NYC, then book the cheapest one"
Example output: ["open Chrome", "search for flights to NYC", "book the cheapest flight"]

Input: {instruction}"""


def _heuristic_is_multi(text: str) -> bool:
    return bool(_SPLIT_RE.search(text))


def _llm_split(text: str, engine_params: dict) -> list[str]:
    """Ask the LLM to split the instruction into tasks. Falls back to [text]."""
    try:
        from gui_agents.s3.core.mllm import LMMAgent

        agent = LMMAgent(engine_params=engine_params)
        prompt = _SPLIT_PROMPT.format(instruction=text)
        messages = [{"role": "user", "content": prompt}]
        response = agent.generate(messages, temperature=0.0, max_new_tokens=512)
        # Extract JSON array from response
        match = re.search(r"\[.*?\]", response, re.DOTALL)
        if match:
            tasks = json.loads(match.group(0))
            if isinstance(tasks, list) and all(isinstance(t, str) for t in tasks):
                tasks = [t.strip() for t in tasks if t.strip()]
                if tasks:
                    return tasks
    except Exception as e:
        logger.warning(f"LLM task split failed ({e}), treating as single task.")
    return [text]


# ---------------------------------------------------------------------------
# TaskQueue
# ---------------------------------------------------------------------------

class TaskQueue:
    """
    A FIFO queue of plain-string tasks.  Call enqueue() with any user
    instruction — multi-task or single — and it fills the queue.
    next() pops the front item and returns (task, current_index, total).
    """

    def __init__(self, engine_params: Optional[dict] = None):
        self._engine_params = engine_params
        self._q: queue.Queue = queue.Queue()
        self._total = 0
        self._done = 0

    def enqueue(self, instruction: str) -> int:
        """
        Parse instruction into 1-N tasks and push onto the queue.
        Returns the number of tasks added.
        """
        instruction = instruction.strip()
        if not instruction:
            return 0

        if _heuristic_is_multi(instruction) and self._engine_params:
            tasks = _llm_split(instruction, self._engine_params)
            if len(tasks) > 1:
                logger.info(f"Multi-task parsed: {tasks}")
                print(f"\n📋  {len(tasks)} tasks queued:")
                for i, t in enumerate(tasks, 1):
                    print(f"     {i}. {t}")
                print()
        else:
            tasks = [instruction]

        for t in tasks:
            self._q.put(t)
        self._total += len(tasks)
        return len(tasks)

    def next(self) -> tuple[str, int, int]:
        """
        Pop and return (task_text, 1-based_index, total).
        Raises queue.Empty if queue is empty.
        """
        task = self._q.get_nowait()
        self._done += 1
        return task, self._done, self._total

    def empty(self) -> bool:
        return self._q.empty()

    def size(self) -> int:
        return self._q.qsize()

    def clear(self):
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._total = 0
        self._done = 0
