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
    reasoning_effort: str | None = None
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
        # 仅本次服务运行使用的 provider 覆盖，不落盘。
        self._runtime_provider_settings: dict[str, dict[str, object]] = {}
        dotenv_path = Path(config_path).parent.parent / ".env"
        if dotenv_path.exists():
            with open(dotenv_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        os.environ[key.strip()] = value.strip().strip("\"'")

    def set_runtime_provider_settings(self, provider: str, base_url: str, api_key: str, models: list[str]) -> None:
        """设置仅存于当前进程内存的 provider 凭据、地址和模型列表。"""
        self._runtime_provider_settings[provider] = {
            "base_url": base_url,
            "api_key": api_key,
            "models": models,
        }

    def clear_runtime_provider_settings(self, provider: str) -> None:
        self._runtime_provider_settings.pop(provider, None)

    def has_runtime_provider_settings(self, provider: str) -> bool:
        return provider in self._runtime_provider_settings

    def get_runtime_provider_base_url(self, provider: str) -> str | None:
        settings = self._runtime_provider_settings.get(provider)
        base_url = settings.get("base_url") if settings else None
        return base_url if isinstance(base_url, str) else None

    def get_runtime_provider_models(self, provider: str) -> list[str] | None:
        settings = self._runtime_provider_settings.get(provider)
        models = settings.get("models") if settings else None
        return list(models) if isinstance(models, list) else None

    def load(self) -> dict:
        with open(self.config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        positions = {name: dict(value or {}) for name, value in config.get("positions", {}).items()}
        position_override_path = Path(self.config_path).with_name("llm_overrides.yaml")
        if position_override_path.exists():
            with open(position_override_path, encoding="utf-8") as f:
                overrides = yaml.safe_load(f) or {}
            for name, override in overrides.get("positions", {}).items():
                if name in positions and isinstance(override, dict):
                    merged = {**positions[name], **override}
                    if isinstance(override.get("overrides"), dict):
                        sub_overrides = {key: dict(value or {}) for key, value in positions[name].get("overrides", {}).items()}
                        for sub_type, sub_override in override["overrides"].items():
                            if isinstance(sub_override, dict):
                                sub_overrides[sub_type] = {**sub_overrides.get(sub_type, {}), **sub_override}
                        merged["overrides"] = sub_overrides
                    positions[name] = merged

        providers = {name: dict(value or {}) for name, value in config.get("providers", {}).items()}
        provider_override_path = Path(self.config_path).with_name("llm_provider_overrides.yaml")
        if provider_override_path.exists():
            with open(provider_override_path, encoding="utf-8") as f:
                overrides = yaml.safe_load(f) or {}
            for name, override in overrides.get("providers", {}).items():
                if name in providers and isinstance(override, dict):
                    safe_override = {
                        field: override[field]
                        for field in ("base_url", "models")
                        if field in override
                        and (isinstance(override[field], str) if field == "base_url" else isinstance(override[field], list))
                    }
                    providers[name] = {**providers[name], **safe_override}

        return {**config, "positions": positions, "providers": providers}

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
        runtime_settings = self._runtime_provider_settings.get(provider_key, {})
        base_url = runtime_settings.get("base_url", provider_info.get("base_url", ""))
        api_key_env = provider_info.get("api_key_env", "")
        api_key = runtime_settings.get("api_key", os.environ.get(api_key_env, ""))
        headers = provider_info.get("headers", {})

        return LLMConfig(
            provider=provider_key,
            model=pos.get("model", ""),
            temperature=pos.get("temperature", 0.7),
            reasoning_effort=pos.get("reasoning_effort") or None,
            max_tokens=pos.get("max_tokens", 8192),
            timeout=pos.get("timeout", 60),
            base_url=base_url,
            api_key=api_key,
            headers=headers,
            retry=pos.get("retry", {"max_retries": 3, "backoff": "exponential"}),
        )
