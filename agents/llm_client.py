"""Shared LLM wrapper for the two agents: Gemini, OpenAI or Claude.

Guardrails live here so each agent stays small: JSON-only output, on-disk cache keyed by (model, prompt),
bounded output tokens, a dry-run mode that never touches the network, and every call (including cache hits)
recorded in lineage.agent_runs. Nothing here executes generated code or SQL.

Gemini and OpenAI share the openai SDK (Gemini serves an OpenAI-compatible endpoint, temperature 0).
Claude goes through the anthropic SDK in agents/claude_client.py.
"""
import hashlib
import json
import logging
import os
import time

from openai import AuthenticationError, OpenAI, RateLimitError

from agents.claude_client import MIN_MAX_TOKENS, ProviderRejected, complete as claude_complete
from src.db import ROOT  # importing src.db also loads .env
from src.lineage import log_agent_call

log = logging.getLogger(__name__)


CACHE_DIR = ROOT / ".agent_cache"
# USD per 1M tokens (input, output). Budgeting estimates only; check current pricing before relying on them.
# Gemini prices change often and are not listed, so Gemini cost shows as unknown (tokens are still tracked).
PRICES = {
    "gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00),
    "claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50),
}
PROVIDERS = {
    "gemini": {"key_env": "GEMINI_API_KEY", "model_env": "GEMINI_MODEL", "default_model": "gemini-3.1-flash-lite",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"},
    "openai": {"key_env": "OPENAI_API_KEY", "model_env": "OPENAI_MODEL", "default_model": "gpt-4o-mini",
               "base_url": None},
    "claude": {"key_env": "ANTHROPIC_API_KEY", "model_env": "CLAUDE_MODEL", "default_model": "claude-opus-5-5"},
}


def _provider() -> dict:
    """LLM_PROVIDER wins; otherwise the first provider whose key is configured (Gemini, Claude, OpenAI)."""
    name = os.getenv("LLM_PROVIDER") or next(
        (p for p in ("gemini", "claude", "openai") if os.getenv(PROVIDERS[p]["key_env"])), "openai")
    if name not in PROVIDERS:
        raise SystemExit(f"LLM_PROVIDER must be one of {sorted(PROVIDERS)}, got {name!r}")
    return {"name": name, **PROVIDERS[name]}


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1  # rough rule of thumb; avoids a tokenizer dependency


def _cost(model: str, prompt_tokens: int, completion_tokens: int):
    if model not in PRICES:
        return None
    price_in, price_out = PRICES[model]
    return round((prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000, 6)


def _call_openai_compatible(provider: dict, model: str, system: str, user: str, max_tokens: int):
    client =OpenAI(api_key=os.getenv(provider["key_env"]), base_url=provider["base_url"],
                    max_retries=3, timeout=90)  # the SDK retries 429/5xx with backoff
    try:
        resp = client.chat.completions.create(
            model=model, temperature=0, response_format={"type": "json_object"},
            max_completion_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
    except (AuthenticationError, RateLimitError) as exc:  # insufficient_quota is a 429 that retrying cannot fix
        raise ProviderRejected(f"{type(exc).__name__}: {exc.message}") from None
    content = resp.choices[0].message.content
    if not content:  # thinking models can spend the whole output budget on reasoning
        raise RuntimeError(f"empty response (finish_reason={resp.choices[0].finish_reason}); "
                           "raise max_output_tokens or pick a non-thinking model")
    return json.loads(content), resp.usage.prompt_tokens, resp.usage.completion_tokens


def complete_json(agent: str, system: str, user: str, *, dry_run: bool = False, use_cache: bool = True,
                  max_output_tokens: int = 3500) -> dict | None:
    """Return the model's JSON object, or None in dry-run mode."""
    provider = _provider()
    model = os.getenv(provider["model_env"]) or provider["default_model"]
    key = hashlib.sha256(f"{model}\x1f{system}\x1f{user}".encode()).hexdigest()
    cache_file = CACHE_DIR / f"{key}.json"

    cached = use_cache and cache_file.exists()
    est_in = estimate_tokens(system + user)
    if dry_run:  # checked first so a dry run never writes proposals or lineage rows, even with a warm cache
        worst_out = max(max_output_tokens, MIN_MAX_TOKENS) if provider["name"] == "claude" else max_output_tokens
        est_cost = _cost(model, est_in, worst_out)
        log.info("%s DRY RUN: provider=%s model=%s ~%d input tokens, worst-case cost $%s%s. No request sent.",
                 agent, provider["name"], model, est_in, est_cost if est_cost is not None else "unknown",
                 " (cached: a real run would cost $0)" if cached else "")
        return None

    if cached:
        log.info("%s: cache hit, no API call", agent)
        log_agent_call(agent, model, 0, 0, 0, 0, True, "SUCCESS")
        return json.loads(cache_file.read_text())
    if provider["name"] != "claude" and not os.getenv(provider["key_env"]):
        # Claude can also authenticate from an `ant auth login` profile, so its SDK reports missing credentials itself
        raise SystemExit(f"{provider['key_env']} is not set. Put it in .env (see .env.example) or use --dry-run.")

    started = time.monotonic()
    try:
        if provider["name"] == "claude":
            payload, tokens_in, tokens_out = claude_complete(model, system, user, max_output_tokens)
        else:
            payload, tokens_in, tokens_out = _call_openai_compatible(provider, model, system, user, max_output_tokens)
    except ProviderRejected as exc:
        log_agent_call(agent, model, est_in, 0, None, int((time.monotonic() - started) * 1000), False, "FAILED")
        raise SystemExit(f"{agent}: {provider['name']} rejected the request ({exc})") from None
    except Exception:
        log_agent_call(agent, model, est_in, 0, None, int((time.monotonic() - started) * 1000), False, "FAILED")
        raise

    cost = _cost(model, tokens_in, tokens_out)
    log_agent_call(agent, model, tokens_in, tokens_out, cost, int((time.monotonic() - started) * 1000), False, "SUCCESS")
    log.info("%s: %s %d in / %d out tokens, ~$%s", agent, model, tokens_in, tokens_out,
             cost if cost is not None else "unknown")
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(payload))
    return payload
