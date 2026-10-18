import os
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    model: str = "claude-sonnet-4-5"
    max_tokens: int = 1024
    provider: str = "anthropic"
    session_id: str = field(default_factory=lambda: "zanbot-" + uuid.uuid4().hex)


def load_config(provider=None, model=None):
    provider = provider or os.environ.get("ZANBOT_PROVIDER", "anthropic")
    if provider == "chatgpt":
        model = model or os.environ.get("CHATGPT_MODEL", "").strip()
        if not model:
            raise ValueError("Choose a ChatGPT model with --model. Use --models to list available models.")
        return Config(api_key="", model=model, provider="chatgpt")
    if provider != "anthropic":
        raise ValueError("ZANBOT_PROVIDER must be anthropic or chatgpt.")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is missing. Set it in your current terminal first.")
    model = model or os.environ.get("ANTHROPIC_MODEL", Config.model).strip()
    if not model:
        raise ValueError("ANTHROPIC_MODEL must not be empty.")
    return Config(api_key=api_key, model=model)
