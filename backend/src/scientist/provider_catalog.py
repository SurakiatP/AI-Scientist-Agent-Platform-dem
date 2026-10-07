"""Curated provider metadata for the platform's OpenAI-compatible adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Provider:
    slug: str
    name: str
    origin: str
    path: str
    model_hint: str


_COMPATIBLE = (
    Provider("openai", "OpenAI", "https://api.openai.com", "/v1/chat/completions", "gpt-4.1-mini"),
    Provider("openrouter", "OpenRouter", "https://openrouter.ai", "/api/v1/chat/completions", "openai/gpt-4.1-mini"),
    Provider("deepseek", "DeepSeek", "https://api.deepseek.com", "/v1/chat/completions", "deepseek-chat"),
    Provider("xai", "xAI", "https://api.x.ai", "/v1/chat/completions", "grok-3-mini"),
    Provider("nvidia-nim", "NVIDIA NIM", "https://integrate.api.nvidia.com", "/v1/chat/completions", "meta/llama-3.3-70b-instruct"),
    Provider("huggingface", "Hugging Face", "https://router.huggingface.co", "/v1/chat/completions", "meta-llama/Llama-3.3-70B-Instruct"),
    Provider("kimi", "Kimi / Moonshot", "https://api.moonshot.ai", "/v1/chat/completions", "kimi-k2"),
    Provider("zai", "Z.AI / GLM", "https://api.z.ai", "/api/paas/v4/chat/completions", "glm-4.5"),
    Provider("arcee", "Arcee AI", "https://api.arcee.ai", "/api/v1/chat/completions", "trinity-large-preview"),
    Provider("gmi", "GMI Cloud", "https://api.gmi-serving.com", "/v1/chat/completions", "deepseek-ai/DeepSeek-V3.2"),
    Provider("vercel", "Vercel AI Gateway", "https://ai-gateway.vercel.sh", "/v1/chat/completions", "openai/gpt-4.1-mini"),
    Provider("alibaba-coding", "Alibaba Cloud Coding Plan", "https://coding-intl.dashscope.aliyuncs.com", "/v1/chat/completions", "qwen3-coder-plus"),
)

_UNAVAILABLE = (
    ("anthropic", "Anthropic", "https://api.anthropic.com", "native_adapter_required"),
    ("google-gemini", "Google Gemini", "https://generativelanguage.googleapis.com", "native_adapter_required"),
    ("minimax", "MiniMax", "https://api.minimax.io", "native_adapter_required"),
    ("lmstudio", "LM Studio", "http://127.0.0.1:1234", "local_isolation_required"),
    ("openai-codex", "OpenAI Codex", "https://chatgpt.com", "oauth_required"),
)

_COMPATIBLE_BY_ORIGIN = {provider.origin: provider for provider in _COMPATIBLE}
_UNAVAILABLE_BY_ORIGIN = {
    origin: reason for _, _, origin, reason in _UNAVAILABLE
} | {
    "https://api.minimaxi.com": "native_adapter_required",
}


def catalog(destinations: Mapping[str, str]) -> list[dict[str, object]]:
    """Return curated choices, binding availability only to configured UUIDs."""
    configured: dict[str, list[str]] = {}
    for provider_id, origin in destinations.items():
        try:
            canonical_id = str(UUID(provider_id))
        except (ValueError, TypeError, AttributeError):
            continue
        if canonical_id != provider_id.lower():
            continue
        configured.setdefault(origin, []).append(canonical_id)

    result = []
    for provider in _COMPATIBLE:
        ids = sorted(configured.get(provider.origin, ()))
        result.append({
            "slug": provider.slug,
            "name": provider.name,
            "origin": provider.origin,
            "provider_id": ids[0] if ids else None,
            "available": bool(ids),
            "reason": None if ids else "not_configured",
            "model_hint": provider.model_hint,
        })
    result.extend({
        "slug": slug,
        "name": name,
        "origin": origin,
        "provider_id": None,
        "available": False,
        "reason": reason,
        "model_hint": "",
    } for slug, name, origin, reason in _UNAVAILABLE)
    return result


def api_path(origin: str) -> str | None:
    provider = _COMPATIBLE_BY_ORIGIN.get(origin)
    return provider.path if provider else None


def unsupported_reason(origin: str) -> str | None:
    return _UNAVAILABLE_BY_ORIGIN.get(origin)
