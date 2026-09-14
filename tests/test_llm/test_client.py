import os
from novelagent.llm.client import LLMClient, AuthenticationError, RateLimitError, APIError, TimeoutError
from novelagent.llm.config_loader import LLMConfigLoader


def test_build_anthropic_request():
    client = LLMClient()
    loader = LLMConfigLoader()
    config = loader.get_config('main_loop')
    msgs = [{"role": "user", "content": "hello"}]
    tools = [{"name": "Read", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]
    body = client._build_request(config, msgs, tools, True)
    assert body['model'] == 'claude-opus-4-6'
    assert 'messages' in body
    assert 'tools' in body
    assert 'stream' in body
    assert body['tools'][0]['name'] == 'Read'
    assert 'input_schema' in body['tools'][0]


def test_build_openai_compatible_request():
    client = LLMClient()
    loader = LLMConfigLoader()
    config = loader.get_config('sub_agent')
    msgs = [{"role": "user", "content": "hi"}]
    tools = [{"name": "Read", "description": "Read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]
    body = client._build_request(config, msgs, tools, True)
    assert body['tools'][0]['type'] == 'function'
    assert 'function' in body['tools'][0]
    assert body['model'] == 'deepseek-chat'


def test_build_request_with_system_message():
    client = LLMClient()
    loader = LLMConfigLoader()
    config = loader.get_config('main_loop')
    msgs = [{"role": "system", "content": "You are helpful"}, {"role": "user", "content": "hi"}]
    body = client._build_request(config, msgs, [], True)
    assert 'system' in body
    assert body['messages'] == [{"role": "user", "content": "hi"}]


def test_to_anthropic_tools():
    client = LLMClient()
    tools = [{"name": "Read", "description": "Read file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]
    result = client._to_anthropic_tools(tools)
    assert result[0]['name'] == 'Read'
    assert 'input_schema' in result[0]


def test_to_openai_tools():
    client = LLMClient()
    tools = [{"name": "Grep", "description": "Search", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}]
    result = client._to_openai_tools(tools)
    assert result[0]['type'] == 'function'
    assert result[0]['function']['name'] == 'Grep'
