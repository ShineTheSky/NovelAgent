import pytest
import tempfile
from pathlib import Path
from novelagent.memory.file_store import FileStore
from novelagent.memory.index_manager import IndexManager


class TestFileStore:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.fs = FileStore(self.tmp)

    def test_write_and_read(self):
        self.fs.write('test.md', 'hello world')
        content = self.fs.read('test.md')
        assert 'hello world' in content

    def test_auto_frontmatter(self):
        self.fs.write('characters/张三.md', '# 张三\n性格内向')
        content = self.fs.read('characters/张三.md')
        assert content.startswith('---')
        assert 'created:' in content
        assert 'updated:' in content
        assert 'status:' in content

    def test_frontmatter_preserve_created(self):
        self.fs.write('test.md', 'v1')
        first = self.fs.read('test.md')
        self.fs.write('test.md', 'v2')
        second = self.fs.read('test.md')
        # created time should be preserved
        import yaml
        fm1 = yaml.safe_load(first.split('---')[1])
        fm2 = yaml.safe_load(second.split('---')[1])
        assert fm1['created'] == fm2['created']

    def test_scan_frontmatter(self):
        self.fs.write('characters/张三.md', '---\ntype: character\ntags: [主角]\nsummary: test\n---\nbody')
        self.fs.write('plots/ch01.md', '---\ntype: plot\ntags: [ch1]\nsummary: chapter 1\n---\nbody')
        entries = self.fs.scan_frontmatter()
        assert len(entries) >= 2
        types = {e.get('type') for e in entries}
        assert 'character' in types
        assert 'plot' in types

    def test_ignore_memory_md(self):
        self.fs.write('memory.md', '# index')
        entries = self.fs.scan_frontmatter()
        assert not any('memory.md' in e.get('_path', '') for e in entries)


class TestIndexManager:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.fs = FileStore(self.tmp)
        self.im = IndexManager(self.fs)

    def test_rebuild_empty(self):
        content = self.im.rebuild()
        assert '记忆索引' in content
        assert '暂无记忆' in content

    def test_rebuild_with_entries(self):
        self.fs.write('characters/张三.md', '---\ntype: character\ntags: [主角]\nsummary: 张三\n---\nbody')
        content = self.im.rebuild()
        assert '张三' in content
        assert 'character' in content
        assert '.memory/characters/张三.md' in content

    def test_get_content_after_rebuild(self):
        self.im.rebuild()
        content = self.im.get_content()
        assert '记忆索引' in content

    def test_max_entries(self):
        # Create more than 200 entries
        for i in range(250):
            self.fs.write(f'plots/ch{i:03d}.md',
                          f'---\ntype: plot\ntags: [ch{i}]\nsummary: chapter {i}\nupdated: 2026-01-{min(i+1, 28):02d}\n---\nbody')
        content = self.im.rebuild()
        # Should only show 200 entries (lines with file paths)
        lines = [l for l in content.split('\n') if '.memory/' in l]
        assert len(lines) <= 200


class TestMemoryManager:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        from novelagent.memory.memory_manager import MemoryManager
        self.mm = MemoryManager(self.tmp)

    def test_init_project_memory(self):
        self.mm.init_project_memory()
        memory_dir = Path(self.tmp) / '.memory'
        assert memory_dir.exists()
        assert (memory_dir / 'user').exists()
        assert (memory_dir / 'feedback').exists()
        assert (memory_dir / 'reference').exists()
        assert (memory_dir / 'summary').exists()
        assert (memory_dir / 'memory.md').exists()

    def test_rebuild_index(self):
        self.mm.init_project_memory()
        content = self.mm.rebuild_index()
        assert '记忆索引' in content

    def test_get_index_content(self):
        self.mm.init_project_memory()
        content = self.mm.get_index_content()
        assert '记忆索引' in content
