"""Local, free LLM calls via Ollama (same engine as jobAce/cv_matcher.py), single-shot JSON mode.

Requires Ollama running locally with the configured model pulled:
    ollama pull qwen2.5:7b

Pinned to 7b, not 14b -- the GPU this runs on can't comfortably fit the 14b model,
and a model squeezed past its card's VRAM (heavy CPU offload / aggressive
quantization) degrades output quality in ways that are easy to miss: still
syntactically valid JSON, but semantically unreliable content inside it (see the
notes-field hallucination guard in classifier.classify_email).
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from . import config

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 3


def call_json(prompt: str, timeout: int | None = None, model: str | None = None) -> dict:
    payload = {
        "model": model or config.OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_ctx": 4096},
        "keep_alive": config.OLLAMA_KEEP_ALIVE,
    }
    data = json.dumps(payload).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(
                f"{config.OLLAMA_HOST}/api/chat",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout or config.OLLAMA_TIMEOUT_SECONDS) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            content = result.get("message", {}).get("content", "")
            return json.loads(content)
        except urllib.error.URLError as exc:
            last_error = RuntimeError(
                f"Couldn't reach Ollama at {config.OLLAMA_HOST} ({exc}). "
                f"Is it running? Try: ollama serve  (and: ollama pull {config.OLLAMA_MODEL})"
            )
        except json.JSONDecodeError as exc:
            last_error = RuntimeError(f"Ollama returned non-JSON content: {exc}")
        logger.warning("Ollama call attempt %d/%d failed: %s", attempt, _MAX_ATTEMPTS, last_error)
        if attempt < _MAX_ATTEMPTS:
            import time
            time.sleep(_RETRY_DELAY_SECONDS)
    raise last_error
