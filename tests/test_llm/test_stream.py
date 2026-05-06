from novelagent.llm.stream import parse_sse_line


def test_anthropic_text_delta():
    r = parse_sse_line('data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}')
    assert r is not None
    assert r.type == 'text_delta'
    assert r.content == 'Hello'


def test_anthropic_tool_use():
    r = parse_sse_line('data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"t1","name":"Grep","input":{"pattern":"test"}}}')
    assert r is not None
    assert r.type == 'tool_use'
    assert r.tool_name == 'Grep'
    assert r.tool_input == {'pattern': 'test'}


def test_openai_text_delta():
    r = parse_sse_line('data: {"id":"1","object":"chat.completion.chunk","choices":[{"delta":{"content":"Hi"},"index":0}]}')
    assert r is not None
    assert r.type == 'text_delta'
    assert r.content == 'Hi'


def test_done_marker():
    r = parse_sse_line('data: [DONE]')
    assert r is not None
    assert r.type == 'done'


def test_invalid_json():
    r = parse_sse_line('data: not json')
    assert r is None


def test_non_data_line():
    r = parse_sse_line('event: done')
    assert r is None


def test_openai_finish_reason():
    r = parse_sse_line('data: {"id":"1","choices":[{"finish_reason":"stop","index":0}]}')
    assert r is not None
    assert r.type == 'done'
    assert r.finish_reason == 'stop'
