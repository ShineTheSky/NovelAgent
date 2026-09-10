import { useEffect, useRef, useState } from 'react';
import {
  deleteRagDocument,
  importRagDocument,
  listRagDocuments,
  searchRag,
  type RagDocument,
  type RagSearchResult,
} from '../api/client';

interface RagPanelProps {
  projectId: string;
  onClose: () => void;
}

export function RagPanel({ projectId, onClose }: RagPanelProps) {
  const [documents, setDocuments] = useState<RagDocument[]>([]);
  const [results, setResults] = useState<RagSearchResult[]>([]);
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  const refresh = async () => {
    try {
      setDocuments(await listRagDocuments(projectId));
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载资料库失败');
    }
  };

  useEffect(() => { void refresh(); inputRef.current?.focus(); }, [projectId]);

  const handleImport = async (file: File) => {
    setBusy(true); setError('');
    try {
      const content = await file.text();
      await importRagDocument(projectId, file.name.replace(/\.[^.]+$/, '') || '未命名文章', content, file.name);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : '导入失败');
    } finally { setBusy(false); }
  };

  const handleSearch = async () => {
    if (!query.trim()) { setResults([]); return; }
    setBusy(true); setError('');
    try { setResults(await searchRag(projectId, query.trim())); }
    catch (err) { setError(err instanceof Error ? err.message : '检索失败'); }
    finally { setBusy(false); }
  };

  const handleDelete = async (documentId: string) => {
    setBusy(true);
    try { await deleteRagDocument(projectId, documentId); await refresh(); setResults([]); }
    catch (err) { setError(err instanceof Error ? err.message : '删除失败'); }
    finally { setBusy(false); }
  };

  return (
    <div className="fixed inset-0 z-30 flex items-start justify-end bg-black/15" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="mr-[264px] mt-14 w-[410px] max-w-[calc(100vw-24px)] rounded-xl border border-gray-200 bg-white p-4 shadow-xl">
        <div className="flex items-center justify-between">
          <div><h2 className="text-sm font-semibold text-gray-800">参考资料库</h2><p className="mt-0.5 text-[11px] text-gray-400">导入文章后，写作时会自动检索相关片段</p></div>
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
        <div className="mt-4 border-t border-gray-100 pt-3"><div className="mb-2 text-[11px] font-medium text-gray-500">已导入文章 · {documents.length}</div><div className="max-h-44 space-y-1 overflow-y-auto">
          {documents.map(document => <div key={document.document_id} className="flex items-center gap-2 rounded-md px-2 py-1.5 text-xs hover:bg-gray-50"><span className="min-w-0 flex-1 truncate text-gray-700" title={document.source_name || document.title}>{document.title}</span><span className="shrink-0 text-[10px] text-gray-400">{document.chunk_count} 块</span><button type="button" onClick={() => void handleDelete(document.document_id)} className="shrink-0 text-gray-300 hover:text-red-500" aria-label={`删除 ${document.title}`}>×</button></div>)}
          {documents.length === 0 && <div className="py-3 text-center text-xs text-gray-400">还没有导入资料</div>}
        </div></div>
      </section>
    </div>
  );
}
