"""Claude provider for the design-time agents, via the official anthropic SDK.

Current Claude models always think and do not accept temperature, so determinism comes from the on-disk cache
in llm_client, and depth is set with effort ("low" suits classification/profiling). Thinking tokens count
towards max_tokens, so the cap is kept well above the size of the JSON answer.
"""
import json
import os
from pathlib import Path

import anthropic

MIN_MAX_TOKENS = 16_000
# Models that accept the server-side refusal fallback; a declined request is re-run on Anthropic's recommended model.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}


class ProviderRejected(Exception):
    """Credentials, permission or quota problem: retrying cannot fix it. Raised by every provider."""


def extract_json(text: str) -> dict:
    """Parse the JSON object, tolerating a ```json fence around it."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def _has_credentials() -> bool:
    """The SDK also accepts an `ant auth login` profile, so an unset ANTHROPIC_API_KEY alone is not proof of none."""
    return any(os.getenv(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")) \
        or (Path.home() / ".config" / "anthropic").is_dir()


def complete(model: str, system: str, user: str, max_tokens: int) -> tuple[dict, int, int]:
    """Return (JSON payload, input tokens, output tokens)."""
    if not _has_credentials():
        raise ProviderRejected("no Claude credentials: set ANTHROPIC_API_KEY in .env or run `ant auth login`")
    extra ={"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in FALLBACK_MODELS else {}
    try:
        client = anthropic.Anthropic(max_retries=3, timeout=180)  # the SDK retries 429/5xx/connection errors
        resp = client.beta.messages.create(
            model=model, max_tokens=max(max_tokens, MIN_MAX_TOKENS), system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": os.getenv("CLAUDE_EFFORT", "low")},
            **extra,
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.RateLimitError,
            anthropic.CredentialsError) as exc:
        raise ProviderRejected(f"{type(exc).__name__}: {exc}") from None
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request ({getattr(resp.stop_details, 'category', None)})")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude hit max_tokens before finishing the JSON; raise max_output_tokens")
    text = "".join(b.text for b in resp.content if b.type == "text")
    return extract_json(text), resp.usage.input_tokens, resp.usage.output_tokens
