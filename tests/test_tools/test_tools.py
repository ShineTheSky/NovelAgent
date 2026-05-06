import pytest
from pathlib import Path
from novelagent.tools.base import ToolContext, PermissionResult
from novelagent.tools.registry import ToolRegistry, ToolNotFoundError, ToolAlreadyRegisteredError
from novelagent.tools.read import ReadTool
from novelagent.tools.write import WriteTool
from novelagent.tools.edit import EditTool
from novelagent.tools.glob import GlobTool
from novelagent.tools.grep import GrepTool
from novelagent.tools.bash import BashTool
from novelagent.tools.subagent_tool import SubAgentTool


def make_ctx(working_dir, allow_rules=None):
    return ToolContext(session_id='s1', project_id='p1', working_dir=working_dir, allow_rules=allow_rules or [])


class TestRegistry:
    def test_register_and_get(self):
        r = ToolRegistry()
        t = ReadTool()
        r.register(t)
        assert r.get('Read').name == 'Read'

    def test_not_found(self):
        r = ToolRegistry()
        with pytest.raises(ToolNotFoundError):
            r.get('NotExist')

    def test_duplicate(self):
        r = ToolRegistry()
        r.register(ReadTool())
        with pytest.raises(ToolAlreadyRegisteredError):
            r.register(ReadTool())

    def test_list_all(self):
        r = ToolRegistry()
        r.register(ReadTool())
        r.register(WriteTool())
        assert len(r.list_all()) == 2

    def test_get_schemas(self):
        r = ToolRegistry()
        r.register(ReadTool())
        schemas = r.get_schemas()
        assert schemas[0]['name'] == 'Read'

    def test_get_tools_prompt(self):
        r = ToolRegistry()
        r.register(ReadTool())
        prompt = r.get_tools_prompt()
        assert 'Read' in prompt


class TestRead:
    @pytest.mark.asyncio
    async def test_read_file(self, working_dir):
        r = await ReadTool().execute({'path': 'chapters/ch01.md'}, make_ctx(working_dir))
        assert r.success
        assert 'Chapter 1' in r.data

    @pytest.mark.asyncio
    async def test_read_with_range(self, working_dir):
        r = await ReadTool().execute({'path': 'chapters/ch01.md', 'offset': 1, 'limit': 1}, make_ctx(working_dir))
        assert r.success
        assert '# Chapter 1' in r.data

    @pytest.mark.asyncio
    async def test_file_not_found(self, working_dir):
        r = await ReadTool().execute({'path': 'nofile.txt'}, make_ctx(working_dir))
        assert not r.success

    def test_permission_in_workdir(self, working_dir):
        assert ReadTool().checkPermissions({'path': 'chapters/ch01.md'}, make_ctx(working_dir)) == PermissionResult.ALLOW

    def test_permission_outside(self, working_dir):
        assert ReadTool().checkPermissions({'path': '/etc/passwd'}, make_ctx(working_dir)) == PermissionResult.ASK


class TestWrite:
    @pytest.mark.asyncio
    async def test_write_file(self, working_dir):
        r = await WriteTool().execute({'path': 'test.md', 'content': 'hello'}, make_ctx(working_dir))
        assert r.success
        assert (Path(working_dir) / 'test.md').read_text() == 'hello'

    @pytest.mark.asyncio
    async def test_overwrite_file(self, working_dir):
        await WriteTool().execute({'path': 'test.md', 'content': 'v1'}, make_ctx(working_dir))
        await WriteTool().execute({'path': 'test.md', 'content': 'v2'}, make_ctx(working_dir))
        assert (Path(working_dir) / 'test.md').read_text() == 'v2'

    def test_permission_allow_by_rule(self, working_dir):
        ctx = make_ctx(working_dir, [{'pattern': 'chapters/**'}])
        assert WriteTool().checkPermissions({'path': 'chapters/ch01.md', 'content': 'x'}, ctx) == PermissionResult.ALLOW

    def test_permission_ask_default(self, working_dir):
        ctx = make_ctx(working_dir)
        assert WriteTool().checkPermissions({'path': 'new_file.md', 'content': 'x'}, ctx) == PermissionResult.ASK

    def test_permission_protected(self, working_dir):
        ctx = make_ctx(working_dir)
        assert WriteTool().checkPermissions({'path': '.env', 'content': 'x'}, ctx) == PermissionResult.ASK


class TestEdit:
    @pytest.mark.asyncio
    async def test_edit_single(self, working_dir):
        await WriteTool().execute({'path': 'test.md', 'content': 'hello world'}, make_ctx(working_dir))
        r = await EditTool().execute({'path': 'test.md', 'old_string': 'hello', 'new_string': 'hi'}, make_ctx(working_dir))
        assert r.success
        assert (Path(working_dir) / 'test.md').read_text() == 'hi world'

    @pytest.mark.asyncio
    async def test_edit_replace_all(self, working_dir):
        await WriteTool().execute({'path': 'test.md', 'content': 'a a a'}, make_ctx(working_dir))
        r = await EditTool().execute({'path': 'test.md', 'old_string': 'a', 'new_string': 'b', 'replace_all': True}, make_ctx(working_dir))
        assert r.success
        assert (Path(working_dir) / 'test.md').read_text() == 'b b b'

    @pytest.mark.asyncio
    async def test_edit_not_unique(self, working_dir):
        await WriteTool().execute({'path': 'test.md', 'content': 'a a'}, make_ctx(working_dir))
        r = await EditTool().execute({'path': 'test.md', 'old_string': 'a', 'new_string': 'b'}, make_ctx(working_dir))
        assert not r.success

    @pytest.mark.asyncio
    async def test_edit_not_found(self, working_dir):
        await WriteTool().execute({'path': 'test.md', 'content': 'hello'}, make_ctx(working_dir))
        r = await EditTool().execute({'path': 'test.md', 'old_string': 'xyz', 'new_string': 'abc'}, make_ctx(working_dir))
        assert not r.success


class TestGlob:
    @pytest.mark.asyncio
    async def test_glob_md_files(self, working_dir):
        r = await GlobTool().execute({'pattern': '**/*.md'}, make_ctx(working_dir))
        assert r.success
        assert 'ch01.md' in r.data

    @pytest.mark.asyncio
    async def test_glob_no_match(self, working_dir):
        r = await GlobTool().execute({'pattern': '*.xyz'}, make_ctx(working_dir))
        assert r.success
        assert '无匹配' in r.data

    def test_permission(self, working_dir):
        assert GlobTool().checkPermissions({'pattern': '*'}, make_ctx(working_dir)) == PermissionResult.ALLOW


class TestGrep:
    @pytest.mark.asyncio
    async def test_grep_find(self, working_dir):
        r = await GrepTool().execute({'pattern': '张三', 'path': 'chapters'}, make_ctx(working_dir))
        assert r.success

    @pytest.mark.asyncio
    async def test_grep_no_match(self, working_dir):
        r = await GrepTool().execute({'pattern': '王五', 'path': 'chapters'}, make_ctx(working_dir))
        assert r.success

    def test_permission(self, working_dir):
        assert GrepTool().checkPermissions({'pattern': 'x'}, make_ctx(working_dir)) == PermissionResult.ALLOW


class TestBash:
    @pytest.mark.asyncio
    async def test_bash_echo(self, working_dir):
        r = await BashTool().execute({'command': 'echo hello', 'working_dir': working_dir}, make_ctx(working_dir))
        assert r.success
        assert 'hello' in r.data

    def test_permission_allow(self, working_dir):
        assert BashTool().checkPermissions({'command': 'wc -w test.txt'}, make_ctx(working_dir)) == PermissionResult.ALLOW

    def test_permission_ask(self, working_dir):
        assert BashTool().checkPermissions({'command': 'rm test.txt'}, make_ctx(working_dir)) == PermissionResult.ASK

    def test_permission_block(self, working_dir):
        assert BashTool().checkPermissions({'command': 'sudo rm -rf /'}, make_ctx(working_dir)) == PermissionResult.BLOCK

    def test_permission_redirect_ask(self, working_dir):
        assert BashTool().checkPermissions({'command': 'echo hi > file.txt'}, make_ctx(working_dir)) == PermissionResult.ASK


class TestSubAgent:
    def test_get_preset(self):
        st = SubAgentTool()
        preset = st.get_preset('chapter_writer')
        assert preset is not None
        assert 'description' in preset
        assert 'tools' in preset
        assert 'system_prompt' in preset

    def test_get_nonexistent_preset(self):
        st = SubAgentTool()
        assert st.get_preset('nonexistent') is None

    @pytest.mark.asyncio
    async def test_execute(self, working_dir):
        st = SubAgentTool()
        r = await st.execute({'preset': 'chapter_writer', 'task': 'test'}, make_ctx(working_dir))
        assert r.success

    def test_permission_allow(self, working_dir):
        assert SubAgentTool().checkPermissions({'preset': 'x', 'task': 'y'}, make_ctx(working_dir)) == PermissionResult.ALLOW
