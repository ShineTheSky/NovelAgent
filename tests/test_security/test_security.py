import os
import pytest
import tempfile
from pathlib import Path
from novelagent.security.bash_classifier import BashClassifier, BashRisk
from novelagent.security.path_validator import PathValidator
from novelagent.security.permission_checker import PermissionChecker
from novelagent.tools.base import ToolContext, PermissionResult
from novelagent.tools.read import ReadTool
from novelagent.tools.write import WriteTool
from novelagent.tools.bash import BashTool


class TestBashClassifier:
    def setup_method(self):
        self.c = BashClassifier()

    def test_allow_commands(self):
        for cmd in ['wc -w file', 'cat file', 'ls -la', 'find . -name x', 'head -5 file', 'echo hello']:
            assert self.c.classify(cmd) == BashRisk.ALLOW, f'{cmd} should be ALLOW'

    def test_ask_commands(self):
        for cmd in ['sed s/a/b/ file', 'awk {print} file', 'tee file', 'rm file', 'mv a b']:
            assert self.c.classify(cmd) == BashRisk.ASK, f'{cmd} should be ASK'

    def test_ask_redirect(self):
        assert self.c.classify('echo hello > file.txt') == BashRisk.ASK

    def test_blocked_commands(self):
        for cmd in ['sudo rm -rf /', 'shutdown now', 'dd if=/dev/zero', 'kill 1234', 'chmod 777 file',
                     'apt install x', 'pip install x', 'gcc test.c', 'eval $CMD', 'nc -l 1234']:
            assert self.c.classify(cmd) == BashRisk.BLOCKED, f'{cmd} should be BLOCKED'

    def test_empty_command(self):
        assert self.c.classify('') == BashRisk.BLOCKED

    def test_pipe_allowed(self):
        assert self.c.classify('cat file | grep x') == BashRisk.ALLOW

    def test_git_destructive_blocked(self):
        assert self.c.classify('git push --force origin main') == BashRisk.BLOCKED
        assert self.c.classify('git reset --hard HEAD') == BashRisk.BLOCKED


class TestPathValidator:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        (Path(self.tmp) / 'chapters').mkdir(exist_ok=True)
        (Path(self.tmp) / 'chapters' / 'ch01.md').write_text('# test')
        self.v = PathValidator(os.path.realpath(self.tmp))

    def test_valid_path(self):
        result = self.v.validate('chapters/ch01.md')
        assert result == True, f"Path validation failed for chapters/ch01.md in {self.tmp}"

    def test_system_path_blocked(self):
        assert self.v.validate('/etc/passwd') == False

    def test_windows_system_path_blocked(self):
        assert self.v.validate('C:\\Windows\\System32\\cmd.exe') == False

    def test_path_escape_blocked(self):
        assert self.v.validate('../../../etc/passwd') == False

    def test_protected_dir(self):
        assert self.v.is_protected('.env') == True
        assert self.v.is_protected('.git/config') == True
        assert self.v.is_protected('some/.memory/memory.md') == True

    def test_not_protected(self):
        assert self.v.is_protected('chapters/ch01.md') == False


class TestPermissionChecker:
    def setup_method(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        (Path(self.tmp) / 'chapters').mkdir(exist_ok=True)
        (Path(self.tmp) / 'chapters' / 'ch01.md').write_text('# test')
        self.checker = PermissionChecker(self.tmp)

    def test_read_allowed(self):
        ctx = ToolContext(session_id='s1', project_id='p1', working_dir=self.tmp)
        r = self.checker.check(ReadTool(), {'path': 'chapters/ch01.md'}, ctx)
        assert r == PermissionResult.ALLOW

    def test_read_blocked_system_path(self):
        ctx = ToolContext(session_id='s1', project_id='p1', working_dir=self.tmp)
        r = self.checker.check(ReadTool(), {'path': '/etc/passwd'}, ctx)
        assert r == PermissionResult.BLOCK

    def test_bash_blocked(self):
        ctx = ToolContext(session_id='s1', project_id='p1', working_dir=self.tmp)
        r = self.checker.check(BashTool(), {'command': 'sudo rm -rf /'}, ctx)
        assert r == PermissionResult.BLOCK

    def test_bash_ask(self):
        ctx = ToolContext(session_id='s1', project_id='p1', working_dir=self.tmp)
        r = self.checker.check(BashTool(), {'command': 'rm file.txt'}, ctx)
        assert r == PermissionResult.ASK

    def test_bash_allow(self):
        ctx = ToolContext(session_id='s1', project_id='p1', working_dir=self.tmp)
        r = self.checker.check(BashTool(), {'command': 'wc -w file'}, ctx)
        assert r == PermissionResult.ALLOW

    def test_write_ask_default(self):
        ctx = ToolContext(session_id='s1', project_id='p1', working_dir=self.tmp)
        r = self.checker.check(WriteTool(), {'path': 'new.md', 'content': 'x'}, ctx)
        assert r == PermissionResult.ASK
