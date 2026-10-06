"""Rebuildable HNSW sidecars. SQLite vectors remain the source of truth."""

import hashlib
import json
import os
import tempfile
from pathlib import Path


def load_index(directory, slot, identity):
    from usearch.index import Index
    directory = Path(directory)
    path = directory / (slot + '.usearch')
    manifest = directory / (slot + '.json')
    if not path.exists() or not manifest.exists():
        return None, 'missing'
    try:
        metadata = json.loads(manifest.read_text(encoding='utf-8'))
        if metadata['identity'] != identity:
            return None, 'stale'
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != metadata['sha256']:
            return None, 'checksum_mismatch'
        index = Index.restore(data)
        if index is None or index.size != len(identity['rows']) or index.ndim != identity['dimensions']:
            return None, 'invalid_index'
        return index, 'persistent'
    except Exception as exc:
        return None, 'unreadable: ' + str(exc)


def _replace(path, data):
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_index(directory, slot, identity, index):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data = index.save()
    manifest = json.dumps({'identity': identity, 'sha256': hashlib.sha256(data).hexdigest()}).encode()
    # Not a cross-file transaction: any torn pair is detected and rebuilt on read.
    _replace(directory / (slot + '.usearch'), data)
    _replace(directory / (slot + '.json'), manifest)
