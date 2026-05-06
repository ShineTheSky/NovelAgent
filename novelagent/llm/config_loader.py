"""llm_config.yaml 加载器"""

import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LLMConfig:
    provider: str
    model: str
    temperature: float = 0.7
    max_tokens: int = 8192
    timeout: float = 60
    base_url: str = ""
    api_key: str = ""
    headers: dict = field(default_factory=dict)
    retry: dict = field(default_factory=dict)

    @property
    def is_anthropic(self) -> bool:
        return "anthropic.com" in self.base_url


class LLMConfigLoader:
    def __init__(self, config_path: str = "config/llm_config.yaml"):
        self.config_path = config_path
        dotenv_path = Path(config_path).parent.parent / ".env"
        if dotenv_path.exists():
            with open(dotenv_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        os.environ[key.strip()] = value.strip().strip("\"'")

    def load(self) -> dict:
        with open(self.config_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def get_config(self, position: str, sub_type: str = None) -> LLMConfig:
        config = self.load()
        defaults = config.get("defaults", {})
        positions = config.get("positions", {})
        providers = config.get("providers", {})

        # 1. Merge position over defaults
        pos = {**defaults, **positions.get(position, {})}

        # 2. Sub-type override (e.g., chapter_writer)
        if sub_type and "overrides" in positions.get(position, {}):
            overrides = positions[position]["overrides"].get(sub_type, {})
            pos = {**pos, **overrides}

        # 3. Resolve provider info
        provider_key = pos.get("provider", defaults.get("provider", "anthropic"))
        provider_info = providers.get(provider_key, {})
        base_url = provider_info.get("base_url", "")
        api_key_env = provider_info.get("api_key_env", "")
        api_key = os.environ.get(api_key_env, "")
        headers = provider_info.get("headers", {})

        return LLMConfig(
            provider=provider_key,
            model=pos.get("model", ""),
            temperature=pos.get("temperature", 0.7),
            max_tokens=pos.get("max_tokens", 8192),
            timeout=pos.get("timeout", 60),
            base_url=base_url,
            api_key=api_key,
            headers=headers,
            retry=pos.get("retry", {"max_retries": 3, "backoff": "exponential"}),
        )
