import os
from novelagent.llm.config_loader import LLMConfigLoader


def test_load_providers():
    loader = LLMConfigLoader()
    cfg = loader.load()
    assert 'anthropic' in cfg['providers']
    assert 'deepseek' in cfg['providers']
    assert cfg['providers']['anthropic']['base_url'] == 'https://api.anthropic.com/v1'


def test_load_positions():
    loader = LLMConfigLoader()
    cfg = loader.load()
    pos = cfg['positions']
    for key in ['main_loop', 'sub_agent', 'memory_prefetch', 'context_compression', 'auto_memory']:
        assert key in pos


def test_defaults_merge():
    loader = LLMConfigLoader()
    cfg = loader.get_config('sub_agent')
    assert cfg.temperature == 0.6
    assert cfg.retry == {'max_retries': 3, 'backoff': 'exponential'}


def test_position_override():
    loader = LLMConfigLoader()
    cfg = loader.get_config('main_loop')
    assert cfg.max_tokens == 16384
    assert cfg.timeout == 120


def test_subagent_override():
    loader = LLMConfigLoader()
    cfg = loader.get_config('sub_agent', 'reviewer')
    assert cfg.temperature == 0.3
    assert cfg.model == 'deepseek-chat'


def test_subagent_provider_override():
    loader = LLMConfigLoader()
    cfg = loader.get_config('sub_agent', 'chapter_writer')
    assert cfg.provider == 'anthropic'
    assert cfg.model == 'claude-sonnet-4-6'


def test_api_key_from_env():
    # Set env var after loader creation to override .env
    loader = LLMConfigLoader()
    os.environ['ANTHROPIC_API_KEY'] = 'override-key-xyz'
    cfg = loader.get_config('main_loop')
    assert cfg.api_key == 'override-key-xyz'


def test_missing_api_key():
    # Key from .env or empty — just verify it's a string
    loader = LLMConfigLoader()
    cfg = loader.get_config('main_loop')
    assert isinstance(cfg.api_key, str)
