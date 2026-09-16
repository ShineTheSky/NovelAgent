import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import {
  deleteRagDocument,
  getRagDocumentChunks,
  getRagEmbeddingJob,
  importRagDocument,
  listRagDocuments,
  rebuildRagEmbeddings,
  searchRag,
  type RagChunkPage,
  type RagEmbeddingJob,
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
  const [embeddingMessage, setEmbeddingMessage] = useState('');
  const [embeddingJob, setEmbeddingJob] = useState<RagEmbeddingJob | null>(null);
  const [panelOffset, setPanelOffset] = useState({ x: 0, y: 0 });
  const inputRef = useRef<HTMLInputElement>(null);
  const dragStart = useRef<{ pointerX: number; pointerY: number; offsetX: number; offsetY: number } | null>(null);
  const embeddedChunks = documents.reduce((total, document) => total + document.embedding_chunk_count, 0);
  const totalChunks = documents.reduce((total, document) => total + document.chunk_count, 0);
  const embeddingInProgress = embeddingJob?.phase === 'embedding' || (embeddingJob?.phase === undefined && (embeddingJob?.completed_chunks || 0) > 0);

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
    if (!embeddingJob || embeddingJob.status !== 'running') return;
    const poll = () => {
      void getRagEmbeddingJob(embeddingJob.job_id).then(next => {
        setEmbeddingJob(next);
        if (next.status === 'completed') {
          setEmbeddingMessage(next.total_chunks ? `已为 ${next.completed_chunks} 个分块建立向量。` : '所有分块的向量都已就绪。');
          void refresh();
        }
        if (next.status === 'failed') setError(next.error || '构建向量失败');
      }).catch(error => { setError(error instanceof Error ? error.message : '读取构建进度失败'); setEmbeddingJob(null); });
    };
    poll();
    const timer = window.setInterval(poll, 500);
    return () => window.clearInterval(timer);
  }, [embeddingJob?.job_id, embeddingJob?.status]);

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
      const imported = await importRagDocument(file.name.replace(/\.[^.]+$/, '') || '未命名文章', content, file.name, encoding);
      setEmbeddingJob(imported.embedding_job);
      setEmbeddingMessage('资料已导入，正在后台生成向量。');
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

  const handleRebuildEmbeddings = async () => {
    setError(''); setEmbeddingMessage('');
    try {
      setEmbeddingJob(await rebuildRagEmbeddings());
    } catch (err) {
      setError(err instanceof Error ? err.message : '构建向量失败');
    }
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

  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest('button')) return;
    dragStart.current = { pointerX: event.clientX, pointerY: event.clientY, offsetX: panelOffset.x, offsetY: panelOffset.y };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const dragPanel = (event: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    if (!start) return;
    setPanelOffset({ x: start.offsetX + event.clientX - start.pointerX, y: start.offsetY + event.clientY - start.pointerY });
  };

  const stopDrag = () => { dragStart.current = null; };

  return (
    <div className="fixed inset-0 z-30 flex items-start justify-end bg-black/15" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
      <section style={{ transform: `translate(${panelOffset.x}px, ${panelOffset.y}px)` }} className="mr-[264px] mt-14 w-[780px] max-w-[calc(100vw-24px)] rounded-xl border border-gray-200 bg-white p-4 shadow-xl">
        <div onPointerDown={startDrag} onPointerMove={dragPanel} onPointerUp={stopDrag} onPointerCancel={stopDrag} className="flex cursor-grab touch-none items-center justify-between active:cursor-grabbing">
          <div><h2 className="text-sm font-semibold text-gray-800">全局参考资料库</h2><p className="mt-0.5 text-[11px] text-gray-400">BM25 与本地 Emb 混合检索，资料由所有项目共享 · Emb {embeddedChunks + (embeddingJob?.status === 'running' ? embeddingJob.completed_chunks : 0)}/{totalChunks}</p></div>
          <button type="button" onClick={onClose} className="rounded px-2 text-lg text-gray-400 hover:bg-gray-100">×</button>
        </div>
        <label className="mt-4 flex cursor-pointer items-center justify-center rounded-lg border border-dashed border-purple-300 bg-purple-50 px-3 py-3 text-xs text-purple-700 hover:bg-purple-100">
          <input type="file" accept=".txt,.md,.markdown,.text" className="hidden" disabled={busy} onChange={event => { const file = event.target.files?.[0]; if (file) void handleImport(file); event.target.value = ''; }} />
          {busy ? '处理中…' : '选择 TXT / Markdown 文章导入'}
        </label>
        <div className="mt-2 flex items-center justify-between gap-3 text-[11px]">
          <span className="text-gray-400">旧资料可手动补齐 Emb；新导入资料会自动生成。</span>
          <button type="button" onClick={() => void handleRebuildEmbeddings()} disabled={busy || embeddingJob?.status === 'running'} className="shrink-0 rounded border border-gray-200 px-2 py-1 text-gray-600 hover:border-purple-300 hover:text-purple-600 disabled:opacity-40">{embeddingJob?.status === 'running' ? '构建中…' : '构建/补齐 Emb'}</button>
        </div>
        {embeddingJob?.status === 'running' && <div className="mt-2"><div className="mb-1 flex justify-between text-[11px] text-gray-500"><span>{embeddingInProgress ? '正在生成向量…' : '正在加载本地 Emb 模型…'}</span><span>{embeddingInProgress && embeddingJob.total_chunks ? `${embeddingJob.completed_chunks}/${embeddingJob.total_chunks}` : '准备中…'}</span></div><div className="h-1.5 overflow-hidden rounded-full bg-gray-100"><div className={`h-full rounded-full bg-purple-500 transition-all ${embeddingInProgress ? '' : 'animate-pulse'}`} style={{ width: `${embeddingInProgress && embeddingJob.total_chunks ? Math.max(2, Math.round(embeddingJob.completed_chunks / embeddingJob.total_chunks * 100)) : 12}%` }} /></div></div>}
        <div className="mt-3 flex gap-2">
          <input ref={inputRef} value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void handleSearch(); }} placeholder="搜索资料库中的相似内容" className="min-w-0 flex-1 rounded-md border border-gray-200 px-2.5 py-2 text-xs outline-none focus:border-purple-400" />
          <button type="button" onClick={() => void handleSearch()} disabled={busy || !query.trim()} className="rounded-md bg-gray-800 px-3 text-xs text-white disabled:opacity-40">检索</button>
        </div>
        {error && <div className="mt-2 rounded-md bg-red-50 px-2 py-1.5 text-xs text-red-600">{error}</div>}
        {embeddingMessage && <div className="mt-2 rounded-md bg-emerald-50 px-2 py-1.5 text-xs text-emerald-700">{embeddingMessage}</div>}
        {results.length > 0 && <div className="mt-3 border-t border-gray-100 pt-3"><div className="mb-2 text-[11px] font-medium text-gray-500">检索结果</div><div className="max-h-[28rem] space-y-2 overflow-y-auto pr-1">{results.map(result => <article key={result.chunk_id} className="relative rounded-md bg-gray-50 p-2.5 pr-28 text-xs text-gray-600"><div className="absolute right-2 top-2 flex flex-col items-end gap-1 text-[10px] font-medium"><span className="rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-700">RRF {formatRrfScore(result.score)}</span><span className="rounded bg-violet-100 px-1.5 py-0.5 text-violet-700">Emb {result.embedding_score === null ? '—' : formatScore(result.embedding_score)}</span><span className="rounded bg-sky-100 px-1.5 py-0.5 text-sky-700">BM25 {formatScore(result.bm25_score)}</span></div><div className="mb-1 truncate pr-2 font-medium text-gray-700">{result.title}</div><div className="whitespace-pre-wrap leading-5">{result.content}</div></article>)}</div></div>}
        {results.length === 0 && <div className="mt-4 grid min-h-[18rem] grid-cols-[minmax(0,0.9fr)_minmax(0,1.6fr)] gap-3 border-t border-gray-100 pt-3">
          <div className="min-w-0 border-r border-gray-100 pr-3">
            <div className="mb-2 text-[11px] font-medium text-gray-500">已导入资料 · {documents.length}</div>
            <div className="max-h-[22rem] space-y-1 overflow-y-auto">
              {documents.map(document => <div key={document.document_id} className={`flex items-center gap-1 rounded-md px-2 py-2 text-xs ${selectedDocumentId === document.document_id ? 'bg-purple-50' : 'hover:bg-gray-50'}`}><button type="button" onClick={() => selectDocument(document.document_id)} className="min-w-0 flex-1 text-left"><span className="flex items-center gap-1.5"><span className="block truncate text-gray-700" title={document.source_name || document.title}>{document.title}</span><span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${document.embedding_chunk_count === document.chunk_count && document.chunk_count > 0 ? 'bg-emerald-100 text-emerald-700' : 'bg-gray-100 text-gray-500'}`}>{document.embedding_chunk_count === document.chunk_count && document.chunk_count > 0 ? 'Emb 已就绪' : '待构建 Emb'}</span></span><span className="mt-0.5 block text-[10px] text-gray-400">{document.chunk_count} 块 · {document.character_count.toLocaleString()} 字 · 向量 {document.embedding_chunk_count}/{document.chunk_count}</span></button>{document.is_corrupted && <span className="shrink-0 text-[10px] text-red-500">需重导</span>}<button type="button" onClick={() => void handleDelete(document.document_id)} className="shrink-0 text-gray-300 hover:text-red-500" aria-label={`删除 ${document.title}`}>×</button></div>)}
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
        </div>}
      </section>
    </div>
  );
}

function formatRrfScore(score: number) {
  return score.toFixed(4);
}

function formatScore(score: number) {
  return Number(score.toFixed(2)).toString();
}
