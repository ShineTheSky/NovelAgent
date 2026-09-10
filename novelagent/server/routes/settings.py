"""本地 LLM 设置 API。浏览器永远无法读取已保存的 API Key。"""

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from novelagent.llm.config_loader import LLMConfigLoader

router = APIRouter(prefix="/api/settings")

SETTING_TARGETS = {
    "main_loop": ("主 Agent", "main_loop", None),
    "sub_agent": ("子 Agent（默认）", "sub_agent", None),
    "chapter_writer": ("章节写作子 Agent", "sub_agent", "chapter_writer"),
    "memory_prefetch": ("记忆预取", "memory_prefetch", None),
    "context_compression": ("上下文压缩", "context_compression", None),
    "auto_memory": ("自动记忆", "auto_memory", None),
}

class PositionUpdate(BaseModel):
    provider: str
    model: str


class LLMSettingsUpdate(BaseModel):
    positions: dict[str, PositionUpdate]


class ProviderUpdate(BaseModel):
    base_url: str
    api_key: str | None = None
    models: list[str] | None = None
    storage: Literal["persistent", "temporary"] = "persistent"


class ProviderSettingsUpdate(BaseModel):
    providers: dict[str, ProviderUpdate]


def _config_path(request: Request) -> Path:
    return Path(request.app.state.llm_config_path)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def _save_yaml(path: Path, data: dict) -> None:
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    temp_path.replace(path)


def _write_env_value(config_path: Path, key: str, value: str) -> None:
    env_path = config_path.parent.parent / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    prefix = f"{key}="
    for index, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _validate_base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="API 地址必须是有效的 http(s) URL")
    return value.strip().rstrip("/")


def _validate_model_names(value: list[str]) -> list[str]:
    """规范化用户为某个 API 配置的模型名列表。"""
    if not value or len(value) > 30:
        raise HTTPException(status_code=400, detail="请配置 1 到 30 个模型名")

    normalized: list[str] = []
    for model in value:
        name = model.strip()
        if not name or len(name) > 128 or "\n" in name or "\r" in name:
            raise HTTPException(status_code=400, detail=f"无效的模型名: {model!r}")
        if name not in normalized:
            normalized.append(name)
    return normalized


def _provider_model_names(provider: dict) -> list[str]:
    models = provider.get("models", [])
    return [model.strip() for model in models if isinstance(model, str) and model.strip()]


@router.get("/llm")
async def get_llm_settings(request: Request):
    """返回可安全展示给浏览器的 provider 和模型设置。"""
    config_path = _config_path(request)
    config = LLMConfigLoader(str(config_path)).load()
    providers = config.get("providers", {})
    positions = config.get("positions", {})
    llm_client = request.app.state.agent_loop.llm

    return {
        "positions": {
            key: {
                "label": label,
                "provider": (positions[position_key].get("overrides", {}).get(sub_type, {}) if sub_type else positions[position_key]).get("provider", config.get("defaults", {}).get("provider", "")),
                "model": (positions[position_key].get("overrides", {}).get(sub_type, {}) if sub_type else positions[position_key]).get("model", config.get("defaults", {}).get("model", "")),
            }
            for key, (label, position_key, sub_type) in SETTING_TARGETS.items()
            if position_key in positions
        },
        "providers": [
            {
                "key": key,
                "name": value.get("name", key),
                "base_url": llm_client.get_runtime_provider_base_url(key) or value.get("base_url", ""),
                "is_configured": bool(os.environ.get(value.get("api_key_env", ""))),
                "has_temporary_key": llm_client.has_runtime_provider_settings(key),
                "models": [
                    {"model": model, "label": model}
                    for model in (llm_client.get_runtime_provider_models(key) or _provider_model_names(value))
                ],
            }
            for key, value in providers.items()
        ],
    }


@router.put("/providers")
async def update_provider_settings(body: ProviderSettingsUpdate, request: Request):
    """保存 provider 设置，或仅在当前服务进程中临时保存。"""
    config_path = _config_path(request)
    config = LLMConfigLoader(str(config_path)).load()
    providers = config.get("providers", {})
    invalid_providers = set(body.providers) - set(providers)
    if invalid_providers:
        raise HTTPException(status_code=400, detail=f"不支持的 provider: {', '.join(sorted(invalid_providers))}")

    override_path = config_path.with_name("llm_provider_overrides.yaml")
    overrides = _load_yaml(override_path)
    provider_overrides = overrides.setdefault("providers", {})
    llm_client = request.app.state.agent_loop.llm
    temporary_providers = []
    has_persistent_update = False
    for key, setting in body.providers.items():
        base_url = _validate_base_url(setting.base_url)
        models = _validate_model_names(setting.models) if setting.models is not None else _provider_model_names(providers[key])
        api_key = setting.api_key.strip() if setting.api_key is not None else ""
        if setting.storage == "temporary":
            if not api_key or len(api_key) > 512 or "\n" in api_key or "\r" in api_key:
                raise HTTPException(status_code=400, detail=f"{providers[key].get('name', key)} 的 API Key 无效")
            llm_client.set_runtime_provider_settings(key, base_url, api_key, models)
            temporary_providers.append(providers[key].get("name", key))
            continue

        provider_overrides[key] = {"base_url": base_url, "models": models}
        has_persistent_update = True
        llm_client.clear_runtime_provider_settings(key)
        if setting.api_key is not None:
            if not api_key or len(api_key) > 512 or "\n" in api_key or "\r" in api_key:
                raise HTTPException(status_code=400, detail=f"{providers[key].get('name', key)} 的 API Key 无效")
            env_key = providers[key].get("api_key_env", "")
            if not env_key:
                raise HTTPException(status_code=400, detail=f"{providers[key].get('name', key)} 未配置 API Key 环境变量")
            _write_env_value(config_path, env_key, api_key)
            os.environ[env_key] = api_key

    if has_persistent_update or override_path.exists():
        _save_yaml(override_path, overrides)
    if temporary_providers:
        return {"status": "ok", "message": f"{', '.join(temporary_providers)} 的 API Key 已临时保存，仅在本次服务运行期间有效。"}
    return {"status": "ok", "message": "公司 API 设置已保存到本机，将在下一次模型调用时生效。"}


@router.put("/llm")
async def update_llm_settings(body: LLMSettingsUpdate, request: Request):
    """保存 Agent 到 provider/model 的映射，不改写主配置。"""
    config_path = _config_path(request)
    config = LLMConfigLoader(str(config_path)).load()
    providers = config.get("providers", {})

    invalid_positions = set(body.positions) - set(SETTING_TARGETS)
    if invalid_positions:
        raise HTTPException(status_code=400, detail=f"不支持的调用位置: {', '.join(sorted(invalid_positions))}")

    for setting in body.positions.values():
        if setting.provider not in providers:
            raise HTTPException(status_code=400, detail=f"不支持的 provider: {setting.provider}")
        model = setting.model.strip()
        if not model or len(model) > 128 or "\n" in model:
            raise HTTPException(status_code=400, detail=f"无效的模型名: {setting.model!r}")
        available_models = request.app.state.agent_loop.llm.get_runtime_provider_models(setting.provider) or _provider_model_names(providers[setting.provider])
        if model not in available_models:
            raise HTTPException(status_code=400, detail=f"模型 {model!r} 未在 {providers[setting.provider].get('name', setting.provider)} 的 API 配置中启用")

    overrides = _load_yaml(config_path.with_name("llm_overrides.yaml"))
    position_overrides = overrides.setdefault("positions", {})
    for key, setting in body.positions.items():
        _, position_key, sub_type = SETTING_TARGETS[key]
        target = position_overrides.setdefault(position_key, {})
        if sub_type:
            target.setdefault("overrides", {})[sub_type] = {"provider": setting.provider, "model": setting.model.strip()}
        else:
            target.update({"provider": setting.provider, "model": setting.model.strip()})

    _save_yaml(config_path.with_name("llm_overrides.yaml"), overrides)
    return {"status": "ok", "message": "Agent 模型路由已保存，将在下一次调用时生效。"}
