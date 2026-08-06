"""
LLM Gateway -- the ONLY place in the codebase that talks to an LLM.

Design constraints (from product spec):
  * LOCAL LLMS ONLY. This wraps Ollama's local HTTP API
    (default http://localhost:11434). There is NO hosted/cloud LLM tier --
    every LLM_TASK below routes to the same local Ollama daemon. The only
    thing that varies per task is which *locally pulled* model name is used
    (configurable via OLLAMA_MODEL_* env vars, all defaulting to "qwen2.5"),
    e.g. you could point OLLAMA_MODEL_HYPOTHESIS at a larger local model and
    OLLAMA_MODEL_CLASSIFICATION at a smaller/faster one if you have multiple
    models pulled. There is no fallback to any hosted API -- if Ollama is
    unreachable, calls fail loudly rather than silently escalating to a
    cloud model.
  * Every prompt is redacted BEFORE it leaves this process (see `redact`)
    to mask secrets/PII per spec section 17.
  * Guardrail prompts: hypothesis-generation prompts explicitly instruct the
    model to only reason over the evidence provided, to cite evidence_ids,
    to surface contradicting evidence, to say "insufficient evidence" rather
    than invent a cause, and to output confidence as a ranking heuristic
    (not a certainty claim) -- see agents/hypothesis_agent.py, which builds
    on top of this gateway.
"""
import json
import re
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from .config import get_settings

logger = logging.getLogger("rootcause.llm_gateway")

settings = get_settings()


# ---------------------------------------------------------------------------
# Redaction (spec section 17): mask secrets/PII before anything reaches the
# model. Regex-based, intentionally conservative (over-redact rather than
# leak). Runs on every prompt sent through this gateway.
# ---------------------------------------------------------------------------

_REDACTION_PATTERNS = [
    # API keys / tokens (generic, plus common vendor prefixes)
    (re.compile(r"\b(sk|pk|ghp|glpat|xox[baprs])-?[A-Za-z0-9_\-]{10,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\b[A-Za-z0-9_\-]*(api[_\-]?key|apikey|access[_\-]?token|secret)[A-Za-z0-9_\-]*\s*[:=]\s*\S+", re.IGNORECASE), "[REDACTED_SECRET]"),
    # Bearer tokens / Authorization headers
    (re.compile(r"(?i)\bauthorization\s*:\s*Bearer\s+\S+"), "Authorization: [REDACTED_TOKEN]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.]{10,}"), "Bearer [REDACTED_TOKEN]"),
    # Passwords
    (re.compile(r"(?i)\bpassword\s*[:=]\s*\S+"), "password=[REDACTED]"),
    (re.compile(r"(?i)\bpasswd\s*[:=]\s*\S+"), "passwd=[REDACTED]"),
    # DB connection strings (postgres://user:pass@host:port/db, mysql://, mongodb://, redis://)
    (re.compile(r"(?i)\b(postgres(ql)?|mysql|mongodb|redis|amqp)://[^\s\"']+"), "[REDACTED_CONNECTION_STRING]"),
    # Emails
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    # IPv4 addresses
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[REDACTED_IP]"),
    # Internal hostnames (heuristic: *.internal, *.local, *.corp, *.svc.cluster.local)
    (re.compile(r"\b[a-zA-Z0-9\-]+\.(internal|local|corp|svc\.cluster\.local)\b"), "[REDACTED_HOSTNAME]"),
    # Generic long hex/base64-looking secrets (JWTs, hashes, etc.)
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), "[REDACTED_JWT]"),
]


def redact(text: str) -> str:
    """Mask API keys, passwords, tokens, emails, IPs, internal hostnames,
    and DB connection strings. Always applied before text is sent to the
    local LLM."""
    if not text:
        return text
    redacted = text
    for pattern, replacement in _REDACTION_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


# ---------------------------------------------------------------------------
# Task -> model routing. All tasks hit the same local Ollama server; only the
# model name differs (and only if the operator pulled multiple models).
# ---------------------------------------------------------------------------

class LLMTask:
    SUMMARY = "summary"
    REDACTION_REVIEW = "redaction_review"
    CLASSIFICATION = "classification"
    HYPOTHESIS = "hypothesis"
    POSTMORTEM = "postmortem"


def _model_for_task(task: str) -> str:
    return {
        LLMTask.SUMMARY: settings.ollama_model_summary,
        LLMTask.REDACTION_REVIEW: settings.ollama_model_redaction,
        LLMTask.CLASSIFICATION: settings.ollama_model_classification,
        LLMTask.HYPOTHESIS: settings.ollama_model_hypothesis,
        LLMTask.POSTMORTEM: settings.ollama_model_postmortem,
    }.get(task, settings.ollama_model_summary)


@dataclass
class LLMResponse:
    text: str
    model: str
    task: str


class OllamaUnavailableError(RuntimeError):
    pass


class LLMGateway:
    """Thin wrapper around Ollama's local HTTP API. No cloud/hosted calls."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 60.0):
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.timeout = timeout

    def generate(
        self,
        task: str,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool = False,
        model: Optional[str] = None,
    ) -> LLMResponse:
        """
        Sends a redacted prompt to the local Ollama /api/generate endpoint.
        Raises OllamaUnavailableError if the local server can't be reached --
        callers (agents) are expected to degrade gracefully (e.g. fall back
        to a templated, non-LLM summary) rather than silently invent content.
        """
        model_name = model or _model_for_task(task)
        safe_system = redact(system_prompt)
        safe_user = redact(user_prompt)

        payload = {
            "model": model_name,
            "system": safe_system,
            "prompt": safe_user,
            "stream": False,
        }
        if json_mode:
            payload["format"] = "json"

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(f"{self.base_url}/api/generate", json=payload)
                resp.raise_for_status()
                data = resp.json()
                return LLMResponse(text=data.get("response", ""), model=model_name, task=task)
        except Exception as exc:  # noqa: BLE001 - want to catch conn errors, timeouts, HTTP errors uniformly
            logger.warning("Ollama call failed (task=%s, model=%s): %s", task, model_name, exc)
            raise OllamaUnavailableError(str(exc)) from exc

    def generate_json(self, task: str, system_prompt: str, user_prompt: str, model: Optional[str] = None) -> dict:
        """Convenience wrapper that requests JSON-mode output and parses it,
        with a best-effort fallback to extracting the first {...} block if
        the model wraps JSON in prose despite instructions."""
        response = self.generate(task, system_prompt, user_prompt, json_mode=True, model=model)
        try:
            return json.loads(response.text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", response.text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            logger.warning("Could not parse JSON from Ollama response for task=%s", task)
            return {}


_gateway_singleton: Optional[LLMGateway] = None


def get_llm_gateway() -> LLMGateway:
    global _gateway_singleton
    if _gateway_singleton is None:
        _gateway_singleton = LLMGateway()
    return _gateway_singleton
