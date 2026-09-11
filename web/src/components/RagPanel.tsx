import { useEffect, useRef, useState } from 'react';
import {
  deleteRagDocument,
  getRagDocumentChunks,
  importRagDocument,
  listRagDocuments,
  searchRag,
  type RagChunkPage,
  type RagDocument,
  type RagSearchResult,
} from '../api/client';

interface RagPanelProps { onClose: () => void; }

function decodeTextFile(file: File): Promise<{ content: string; encoding: string }> {
  return file.arrayBuffer().then(bytes => {
    for (const encoding of ['utf-8', 'gb18030']) {
      try {
        const content = new TextDecoder(encoding, { fatal: true }).decode(bytes);
        if (!content.includes('\ufffd')) return { content, encoding };
      } catch {
        // Try the next supported Chinese text encoding.
      }
    }
    throw new Error('无法识别文本编码，请将文件保存为 UTF-8、GBK 或 GB18030 后重试');
  });
}

export function RagPanel({ onClose }: RagPanelProps) {
  const [documents, setDocuments] = useState<RagDocument[]>([]);
  const [results, setResults] = useState<RagSearchResult[]>([]);
  const [chunkPage, setChunkPage] = useState<RagChunkPage | null>(null);
  const [selectedDocumentId, setSelectedDocumentId] = useState('');
  const [chunkOffset, setChunkOffset] = useState(0);
  const [expandedChunkId, setExpandedChunkId] = useState('');
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [chunkBusy, setChunkBusy] = useState(false);
  const [error, setError] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  const refresh = async () => {
    try {
      const nextDocuments = await listRagDocuments();
      setDocuments(nextDocuments);
      setSelectedDocumentId(current => nextDocuments.some(document => document.document_id === current) ? current : '');
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载资料库失败');
    }
  };

  useEffect(() => { void refresh(); inputRef.current?.focus(); }, []);

  useEffect(() => {
    if (!selectedDocumentId) { setChunkPage(null); return; }
    let cancelled = false;
    setChunkBusy(true);
    void getRagDocumentChunks(selectedDocumentId, chunkOffset).then(page => {
      if (!cancelled) setChunkPage(page);
    }).catch(err => {
      if (!cancelled) setError(err instanceof Error ? err.message : '加载分块失败');
    }).finally(() => {
      if (!cancelled) setChunkBusy(false);
    });
    return () => { cancelled = true; };
  }, [selectedDocumentId, chunkOffset]);

  const handleImport = async (file: File) => {
    setBusy(true); setError('');
    try {
      const { content, encoding } = await decodeTextFile(file);
      await importRagDocument(file.name.replace(/\.[^.]+$/, '') || '未命名文章', content, file.name, encoding);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : '导入失败');
    } finally { setBusy(false); }
  };

  const handleSearch = async () => {
    if (!query.trim()) { setResults([]); return; }
    setBusy(true); setError('');
    try { setResults(await searchRag(query.trim())); }
    catch (err) { setError(err instanceof Error ? err.message : '检索失败'); }
    finally { setBusy(false); }
  };

  const handleDelete = async (documentId: string) => {
    setBusy(true); setError('');
    try {
      await deleteRagDocument(documentId);
      await refresh();
      setResults([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败');
    } finally { setBusy(false); }
  };

  const selectDocument = (documentId: string) => {
    setSelectedDocumentId(documentId);
    setChunkOffset(0);
    setExpandedChunkId('');
  };

  return (
    <div className="fixed inset-0 z-30 flex items-start justify-end bg-black/15" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="mr-[264px] mt-14 w-[780px] max-w-[calc(100vw-24px)] rounded-xl border border-gray-200 bg-white p-4 shadow-xl">
        <div className="flex items-center justify-between">
          <div><h2 className="text-sm font-semibold text-gray-800">全局参考资料库</h2><p className="mt-0.5 text-[11px] text-gray-400">资料由所有项目共享，写作时会自动检索相关片段</p></div>
          <button type="button" onClick={onClose} className="rounded px-2 text-lg text-gray-400 hover:bg-gray-100">×</button>
        </div>
        <label className="mt-4 flex cursor-pointer items-center justify-center rounded-lg border border-dashed border-purple-300 bg-purple-50 px-3 py-3 text-xs text-purple-700 hover:bg-purple-100">
          <input type="file" accept=".txt,.md,.markdown,.text" className="hidden" disabled={busy} onChange={event => { const file = event.target.files?.[0]; if (file) void handleImport(file); event.target.value = ''; }} />
          {busy ? '处理中…' : '选择 TXT / Markdown 文章导入'}
        </label>
        <div className="mt-3 flex gap-2">
          <input ref={inputRef} value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void handleSearch(); }} placeholder="搜索资料库中的相似内容" className="min-w-0 flex-1 rounded-md border border-gray-200 px-2.5 py-2 text-xs outline-none focus:border-purple-400" />
          <button type="button" onClick={() => void handleSearch()} disabled={busy || !query.trim()} className="rounded-md bg-gray-800 px-3 text-xs text-white disabled:opacity-40">检索</button>
        </div>
        {error && <div className="mt-2 rounded-md bg-red-50 px-2 py-1.5 text-xs text-red-600">{error}</div>}
        {results.length > 0 && <div className="mt-3 space-y-2 border-t border-gray-100 pt-3"><div className="text-[11px] font-medium text-gray-500">检索结果</div>{results.map(result => <article key={result.chunk_id} className="rounded-md bg-gray-50 p-2.5 text-xs text-gray-600"><div className="mb-1 font-medium text-gray-700">{result.title}</div><div className="line-clamp-4 whitespace-pre-wrap leading-5">{result.content}</div></article>)}</div>}
        <div className="mt-4 grid min-h-[18rem] grid-cols-[minmax(0,0.9fr)_minmax(0,1.6fr)] gap-3 border-t border-gray-100 pt-3">
          <div className="min-w-0 border-r border-gray-100 pr-3">
            <div className="mb-2 text-[11px] font-medium text-gray-500">已导入资料 · {documents.length}</div>
            <div className="max-h-[22rem] space-y-1 overflow-y-auto">
              {documents.map(document => <div key={document.document_id} className={`flex items-center gap-1 rounded-md px-2 py-2 text-xs ${selectedDocumentId === document.document_id ? 'bg-purple-50' : 'hover:bg-gray-50'}`}><button type="button" onClick={() => selectDocument(document.document_id)} className="min-w-0 flex-1 text-left"><span className="block truncate text-gray-700" title={document.source_name || document.title}>{document.title}</span><span className="mt-0.5 block text-[10px] text-gray-400">{document.chunk_count} 块 · {document.character_count.toLocaleString()} 字</span></button>{document.is_corrupted && <span className="shrink-0 text-[10px] text-red-500">需重导</span>}<button type="button" onClick={() => void handleDelete(document.document_id)} className="shrink-0 text-gray-300 hover:text-red-500" aria-label={`删除 ${document.title}`}>×</button></div>)}
              {documents.length === 0 && <div className="py-3 text-center text-xs text-gray-400">还没有导入资料</div>}
            </div>
          </div>
          <div className="min-w-0">
            <div className="mb-2 flex items-center justify-between gap-2"><div className="min-w-0 truncate text-[11px] font-medium text-gray-500">{chunkPage ? `${chunkPage.title} · 分块浏览` : '选择左侧资料查看分块'}</div>{chunkPage && <div className="flex shrink-0 items-center gap-1"><button type="button" onClick={() => { setChunkOffset(Math.max(0, chunkPage.offset - chunkPage.limit)); setExpandedChunkId(''); }} disabled={chunkBusy || chunkPage.offset === 0} className="rounded border border-gray-200 px-1.5 py-0.5 text-[10px] text-gray-500 disabled:opacity-40">上一页</button><span className="text-[10px] text-gray-400">{chunkPage.offset + 1}-{Math.min(chunkPage.offset + chunkPage.chunks.length, chunkPage.total)} / {chunkPage.total}</span><button type="button" onClick={() => { setChunkOffset(chunkPage.offset + chunkPage.limit); setExpandedChunkId(''); }} disabled={chunkBusy || chunkPage.offset + chunkPage.limit >= chunkPage.total} className="rounded border border-gray-200 px-1.5 py-0.5 text-[10px] text-gray-500 disabled:opacity-40">下一页</button></div>}</div>
            <div className="max-h-[22rem] space-y-1 overflow-y-auto">
              {chunkBusy && <div className="py-8 text-center text-xs text-gray-400">加载分块中…</div>}
              {!chunkBusy && chunkPage?.chunks.map(chunk => <button key={chunk.chunk_id} type="button" onClick={() => setExpandedChunkId(current => current === chunk.chunk_id ? '' : chunk.chunk_id)} className="block w-full rounded-md bg-gray-50 px-2.5 py-2 text-left hover:bg-purple-50"><div className="mb-1 flex justify-between text-[10px] text-gray-400"><span>第 {chunk.chunk_index + 1} 块</span><span>{chunk.character_count} 字</span></div><div className={`whitespace-pre-wrap text-xs leading-5 text-gray-600 ${expandedChunkId === chunk.chunk_id ? '' : 'line-clamp-3'}`}>{chunk.content}</div></button>)}
              {!chunkBusy && !chunkPage && <div className="py-8 text-center text-xs text-gray-400">点击资料名称查看其分块</div>}
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
