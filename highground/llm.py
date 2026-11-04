"""Thin helpers around the Mistral SDK. mistral-large-4 is a reasoning model: message.content is a list of ThinkChunk/TextChunk, not a str."""

from highground.config import settings

REASONING = {"reasoning_effort": "low"}  # large-4 rejects "none"; low keeps a briefing at about 2-3 s


def client(timeout_ms: int = 20000):
    from mistralai.client import Mistral

    return Mistral(api_key=settings().mistral_api_key, timeout_ms=timeout_ms)


def text(content) -> str:
    """Visible answer text only (thinking chunks dropped)."""
    if content is None or isinstance(content, str):
        return content or ""
    out = []
    for c in content:
        kind = getattr(c, "type", None) or (c.get("type") if isinstance(c, dict) else None)
        if kind == "text":
            out.append(getattr(c, "text", None) or (c.get("text") if isinstance(c, dict) else "") or "")
    return "".join(out).strip()


def extra(model: str) -> dict:
    """Request options for reasoning models (large-4 / magistral); empty for the others."""
    return REASONING if model.startswith(("mistral-large-4", "magistral")) else {}
