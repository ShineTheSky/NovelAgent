import pytest
import tempfile
import os
from pathlib import Path

@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d

@pytest.fixture
def working_dir(temp_dir):
    wd = Path(temp_dir) / "workspace"
    wd.mkdir()
    chapters = wd / "chapters"
    chapters.mkdir()
    (chapters / "ch01.md").write_text("# Chapter 1\nHello world\n张三走进了房间。", encoding="utf-8")
    (chapters / "ch02.md").write_text("# Chapter 2\n张三发现了秘密。\n李四在暗中观察。", encoding="utf-8")
    return str(wd)

@pytest.fixture(autouse=True)
def setup_llm_env():
    os.environ.setdefault('ANTHROPIC_API_KEY', 'test-key')
    os.environ.setdefault('DEEPSEEK_API_KEY', 'test-key')
