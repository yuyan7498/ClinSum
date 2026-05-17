"""
Ollama HTTP wrapper used by archiver and worker / supervisor agents.

Why /api/chat (not /api/generate):
  Modern Ollama models (gemma4, gpt-oss) emit reasoning into a separate
  `thinking` field on the chat response. /api/generate with `prompt=` either
  loses the content or returns empty. /api/chat with role-based messages
  exposes both content and thinking, which lets us:
    - gemma4:  pass think=False  → reasoning suppressed, fast extraction
    - gpt-oss: omit think        → keeps short native reasoning, then content

Each LLMConfig declares disable_thinking; clients set the flag accordingly.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterator, Optional

import requests


@dataclass
class LLMConfig:
    host: str
    model: str
    temperature: float = 0.2
    top_p: float = 0.9
    num_predict: int = 4096
    num_ctx: int = 16384
    disable_thinking: bool = False  # gemma4 → True; gpt-oss → False
    timeout: int = 600

    # Back-compat: callers may still pass think_supported.
    @property
    def think_supported(self) -> bool:
        return not self.disable_thinking


class OllamaClient:
    """Stateless chat client. Each call builds a fresh request."""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    # ── core chat call ───────────────────────────────────────────────

    def chat(self, system: str, user: str,
             temperature: Optional[float] = None,
             num_predict: Optional[int] = None,
             stream: bool = False):
        body = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": stream,
            "options": {
                "temperature": self.cfg.temperature if temperature is None else temperature,
                "top_p": self.cfg.top_p,
                "num_predict": self.cfg.num_predict if num_predict is None else num_predict,
                "num_ctx": self.cfg.num_ctx,
            },
        }
        if self.cfg.disable_thinking:
            body["think"] = False
        return body

    # ── non-streaming completion (returns plain string) ──────────────

    def complete(self, system: str, user: str,
                 temperature: Optional[float] = None,
                 num_predict: Optional[int] = None) -> str:
        body = self.chat(system, user, temperature, num_predict, stream=False)
        r = requests.post(f"{self.cfg.host}/api/chat", json=body,
                          timeout=self.cfg.timeout)
        r.raise_for_status()
        data = r.json()
        msg = data.get("message", {}) or {}
        content = (msg.get("content") or "").strip()
        # Fallback: if content empty but thinking has output (rare misconfig)
        if not content:
            content = (msg.get("thinking") or "").strip()
        return content

    # ── streaming completion (final assembly only) ───────────────────

    def stream_complete(self, system: str, user: str) -> Iterator[dict]:
        body = self.chat(system, user, stream=True)
        r = requests.post(f"{self.cfg.host}/api/chat", json=body,
                          stream=True, timeout=(15, self.cfg.timeout))
        r.raise_for_status()

        emitted = ""
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = chunk.get("message", {}) or {}
            piece = msg.get("content", "") or ""
            if piece:
                new_total = emitted + piece
                delta = new_total[len(emitted):]
                if delta:
                    yield {"delta": delta}
                emitted = new_total
            if chunk.get("done"):
                yield {
                    "done": True,
                    "eval_count": chunk.get("eval_count"),
                    "eval_duration": chunk.get("eval_duration"),
                    "final_text": emitted,
                }
                return

    # ── JSON-mode helper ─────────────────────────────────────────────

    _JSON_FENCE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*```")

    def complete_json(self, system: str, user: str,
                      temperature: Optional[float] = None,
                      num_predict: Optional[int] = None) -> object:
        sys_with_hint = (
            system + "\n\n只輸出 JSON，不要任何解釋或前後文字。可以用 ```json``` 包起來。"
        )
        text = self.complete(sys_with_hint, user,
                             temperature=temperature, num_predict=num_predict)
        return self._parse_json(text)

    @classmethod
    def _parse_json(cls, text: str) -> object:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty LLM response")
        m = cls._JSON_FENCE.search(text)
        if m:
            return json.loads(m.group(1))
        # Bare JSON — find first { or [
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            if start < 0:
                continue
            depth = 0
            for i in range(start, len(text)):
                c = text[i]
                if c == opener:
                    depth += 1
                elif c == closer:
                    depth -= 1
                    if depth == 0:
                        return json.loads(text[start:i + 1])
        raise ValueError(f"no JSON in response: {text[:200]}")
