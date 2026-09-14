import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    model: str = "claude-sonnet-4-5"
    max_tokens: int = 1024


def load_config():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is missing. Set it in your current terminal first.")
    model = os.environ.get("ANTHROPIC_MODEL", Config.model).strip()
    if not model:
        raise ValueError("ANTHROPIC_MODEL must not be empty.")
    return Config(api_key=api_key, model=model)
