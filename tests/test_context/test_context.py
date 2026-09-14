import pytest
from novelagent.context.token_counter import TokenCounter
from novelagent.context.prompt_manager import PromptManager
from novelagent.context.message_manager import MessageManager, Message


class TestTokenCounter:
    def setup_method(self):
        self.c = TokenCounter()

    def test_english_count(self):
        n = self.c.count("hello world")
        assert n == 2

    def test_chinese_count(self):
        n = self.c.count("你好世界")
        assert n > 0

    def test_empty(self):
        assert self.c.count("") == 0

    def test_messages_count(self):
        msgs = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi there"}]
        n = self.c.count_messages(msgs)
        assert n > 4  # content + role overhead

    def test_needs_compression_true(self):
        # Use unique content per message to avoid any caching and ensure token count exceeds threshold
        msgs = [{"role": "user", "content": f"message number {i}. " + "unique content " * 200} for i in range(500)]
        assert self.c.needs_compression(msgs, 150000)

    def test_needs_compression_false(self):
        msgs = [{"role": "user", "content": "hello"}]
        assert not self.c.needs_compression(msgs, 150000)


class TestPromptManager:
    def setup_method(self):
        self.pm = PromptManager("prompts")

    def test_load_base_template(self):
        result = self.pm.render("base_system.j2", {
            "current_date": "2026-05-02",
            "project": {"name": "Test", "genre": "", "word_count": 0},
            "memory_md_content": "",
            "tools_description": "No tools",
        })
        assert "NovelAgent" in result
        assert "安全准则" in result
        assert "2026-05-02" in result

    def test_include_partials(self):
        result = self.pm.render("base_system.j2", {
            "current_date": "2026-05-02",
            "project": {"name": "T", "genre": "", "word_count": 0},
            "memory_md_content": "",
            "tools_description": "",
        })
        assert "安全准则" in result
        assert "记忆索引" in result

    def test_template_not_found(self):
        with pytest.raises(FileNotFoundError):
            self.pm.render("nonexistent.j2")


class TestMessageManager:
    def setup_method(self):
        self.mm = MessageManager()

    def test_append_and_get(self):
        self.mm.append(Message(role="user", content="hello"))
        msgs = self.mm.get_messages()
        assert len(msgs) == 1
        assert msgs[0].role == 'user'

    def test_clear(self):
        self.mm.append(Message(role="user", content="hi"))
        self.mm.clear()
        assert len(self.mm.get_messages()) == 0

    def test_to_dicts_roundtrip(self):
        self.mm.append(Message(role="user", content="hello"))
        self.mm.append(Message(role="assistant", content="hi", tool_calls=[{"id": "1", "name": "Grep", "input": {"pattern": "test"}}]))
        dicts = self.mm.to_dicts()
        mm2 = MessageManager()
        mm2.load_from_dicts(dicts)
        assert len(mm2.get_messages()) == 2
        assert mm2.get_messages()[1].tool_calls[0]['name'] == 'Grep'

    def test_json_roundtrip(self):
        self.mm.append(Message(role="user", content="hello"))
        json_str = self.mm.to_json()
        mm2 = MessageManager()
        mm2.load_from_json(json_str)
        assert mm2.get_messages()[0].content == 'hello'

    def test_merge_subagent_result(self):
        self.mm.merge_subagent_result("reviewer", "审阅完成，无问题")
        msgs = self.mm.get_messages()
        assert msgs[0].role == 'tool_result'
        assert msgs[0].name == 'SubAgent'
        assert '审阅完成' in msgs[0].content

    def test_get_tool_pair(self):
        self.mm.append(Message(role="assistant", content="", tool_calls=[{"id": "call_1", "name": "Grep", "input": {}}]))
        self.mm.append(Message(role="tool_result", tool_call_id="call_1", content="result"))
        use, result = self.mm.get_tool_pair("call_1")
        assert use is not None
        assert result is not None
        assert result.content == 'result'
