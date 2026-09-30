import { useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { fetchHistoryFiles, fetchHistoryUnit, fetchProjectHistory } from '../api/client';
import type { HistoryFilePage, HistoryKind, HistorySummary, HistoryUnit } from '../types/history';

const PAGE_SIZE = 25;
const errorText = (error: unknown) => error instanceof Error ? error.message : '加载 History 失败';
const labels: Record<HistoryKind, string> = { document: '文件', conversation: '对话' };

export function HistoryPanel({ projectId }: { projectId: string | null }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<'files' | 'conversation'>('files');
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [files, setFiles] = useState<HistoryFilePage['items']>([]);
  const [items, setItems] = useState<HistorySummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<HistoryUnit | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const listRequest = useRef(0);
  const detailRequest = useRef(0);

  const loadFiles = async (nextOffset = 0) => {
    if (!projectId) return;
    const requestId = ++listRequest.current;
    detailRequest.current += 1;
    setSelected(null);
    setDetailBusy(false);
    setDetailError(null);
    setBusy(true);
    setError(null);
    try {
      const page = await fetchHistoryFiles(projectId, nextOffset, PAGE_SIZE);
      if (requestId !== listRequest.current) return;
      setFiles(page.items);
      setTotal(page.total);
      setMode('files');
      setSelectedFile(null);
      setOffset(nextOffset);
    } catch (requestError) {
      if (requestId === listRequest.current) setError(errorText(requestError));
    } finally {
      if (requestId === listRequest.current) setBusy(false);
    }
  };

  const loadHistory = async (kind: HistoryKind, path = '', nextOffset = 0) => {
    if (!projectId) return;
    const requestId = ++listRequest.current;
    detailRequest.current += 1;
    setSelected(null);
    setDetailBusy(false);
    setDetailError(null);
    setBusy(true);
    setError(null);
    try {
      const page = await fetchProjectHistory(projectId, kind, nextOffset, PAGE_SIZE, path);
      if (requestId !== listRequest.current) return;
      setItems(page.items);
      setTotal(page.total);
      setMode(kind === 'conversation' ? 'conversation' : 'files');
      setSelectedFile(path || null);
      setOffset(nextOffset);
    } catch (requestError) {
      if (requestId === listRequest.current) setError(errorText(requestError));
    } finally {
      if (requestId === listRequest.current) setBusy(false);
    }
  };

  const select = async (historyId: string) => {
    if (!projectId) return;
    const requestId = ++detailRequest.current;
    setSelected(null);
    setDetailBusy(true);
    setDetailError(null);
    try {
      const unit = await fetchHistoryUnit(projectId, historyId);
      if (requestId === detailRequest.current) setSelected(unit);
    } catch (requestError) {
      if (requestId === detailRequest.current) setDetailError(errorText(requestError));
    } finally {
      if (requestId === detailRequest.current) setDetailBusy(false);
    }
  };

  return <>
    <button type="button" disabled={!projectId} onClick={() => { setOpen(true); void loadFiles(0); }} className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 shadow-sm hover:border-purple-200 hover:text-purple-600 disabled:opacity-40">History</button>
    {open && createPortal(
      <div className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-labelledby="history-title">
        <div className="flex h-[86vh] w-full max-w-6xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl">
          <header className="flex shrink-0 items-start justify-between border-b px-6 py-4">
            <div>
              <h2 id="history-title" className="font-semibold text-gray-800">项目 History</h2>
              <p className="mt-1 text-xs text-gray-400">先选文件，再查看该文件的 History 链与具体修改证据。</p>
            </div>
            <button type="button" onClick={() => { listRequest.current += 1; detailRequest.current += 1; setOpen(false); }} aria-label="关闭 History" className="text-gray-400 hover:text-gray-700">✕</button>
          </header>
          <div className="flex shrink-0 items-center gap-2 border-b bg-gray-50 px-5 py-3 text-xs">
            <button type="button" onClick={() => void loadFiles(0)} className={`rounded-lg px-3 py-1.5 ${mode === 'files' ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-purple-600'}`}>文件</button>
            <button type="button" onClick={() => void loadHistory('conversation')} className={`rounded-lg px-3 py-1.5 ${mode === 'conversation' ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-purple-600'}`}>对话</button>
            <button type="button" disabled={busy} onClick={() => void (selectedFile ? loadHistory('document', selectedFile, offset) : mode === 'files' ? loadFiles(offset) : loadHistory('conversation', '', offset))} className="ml-auto text-purple-600 disabled:opacity-40">{busy ? '加载中…' : '刷新'}</button>
          </div>
          <div className="flex min-h-0 flex-1 flex-col md:flex-row">
            <section className="flex min-h-0 w-full flex-col border-b md:w-2/5 md:border-b-0 md:border-r">
              <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-4">
                {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-xs text-red-600">{error}</p>}
                {selectedFile && <button type="button" onClick={() => void loadFiles(0)} className="mb-2 text-xs text-purple-600">← 返回文件列表</button>}
                {selectedFile && <p className="break-all text-xs font-medium text-gray-600">{selectedFile}</p>}
                {mode === 'files' && !selectedFile && files.map(file => <button key={file.path} type="button" onClick={() => void loadHistory('document', file.path)} className="w-full rounded-xl border border-gray-100 p-3 text-left text-xs hover:border-purple-200 hover:bg-gray-50">
                  <div className="break-all font-medium text-gray-700">{file.path}</div>
                  <div className="mt-2 text-gray-400">{file.history_count} 条 History · 最新 {file.updated_at?.slice(0, 16).replace('T', ' ')}</div>
                </button>)}
                {(selectedFile || mode === 'conversation') && items.map(item => <button key={item.history_id} type="button" onClick={() => void select(item.history_id)} className={`w-full rounded-xl border p-3 text-left text-xs transition ${selected?.history_id === item.history_id ? 'border-purple-300 bg-purple-50' : 'border-gray-100 hover:border-purple-200 hover:bg-gray-50'}`}>
                  <div className="flex items-center gap-2 text-[11px] text-gray-400">
                    <span className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-600">{labels[item.kind]}</span>
                    <span>{item.created_at?.slice(0, 16).replace('T', ' ')}</span>
                    {item.session_turn_no > 0 && <span>第 {item.session_turn_no} 轮</span>}
                    <span className="ml-auto">{item.status}</span>
                  </div>
                  {item.path ? <div className="mt-2 break-all font-mono text-[11px] font-medium text-purple-700">History {item.history_id}</div>
                    : <div className="mt-2 truncate font-medium text-gray-700" title={item.user_input || ''}>{item.user_input || '（无标题）'}</div>}
                  {item.path && <div className="mt-1 truncate text-gray-600" title={item.task_summary || ''}>{item.task_summary || '（未记录 Agent 任务）'}</div>}
                  {item.path && <div className="mt-1 truncate text-gray-400" title={item.user_input || ''}>{item.user_input || '（无用户输入）'}</div>}
                  {!item.path && <div className="mt-1 truncate font-mono text-[10px] text-gray-400">{item.history_id}</div>}
                  {item.path && item.previous_history_id && <div className="mt-1 truncate font-mono text-[10px] text-gray-400">上一条 {item.previous_history_id}</div>}
                </button>)}
                {!busy && !error && !(selectedFile || mode === 'conversation' ? items : files).length && <p className="py-12 text-center text-sm text-gray-400">当前项目还没有 History。</p>}
              </div>
              <footer className="flex shrink-0 items-center justify-between border-t px-4 py-3 text-xs text-gray-500">
                <span>共 {total} 条 · {total ? offset + 1 : 0}–{Math.min(offset + PAGE_SIZE, total)}</span>
                <div className="flex gap-2">
                  <button type="button" disabled={offset === 0 || busy} onClick={() => void (selectedFile ? loadHistory('document', selectedFile, offset - PAGE_SIZE) : mode === 'files' ? loadFiles(offset - PAGE_SIZE) : loadHistory('conversation', '', offset - PAGE_SIZE))} className="rounded border px-2 py-1 disabled:opacity-40">上一页</button>
                  <button type="button" disabled={offset + PAGE_SIZE >= total || busy} onClick={() => void (selectedFile ? loadHistory('document', selectedFile, offset + PAGE_SIZE) : mode === 'files' ? loadFiles(offset + PAGE_SIZE) : loadHistory('conversation', '', offset + PAGE_SIZE))} className="rounded border px-2 py-1 disabled:opacity-40">下一页</button>
                </div>
              </footer>
            </section>
            <section className="min-h-0 flex-1 overflow-y-auto p-5 text-sm">
              {detailError && <p role="alert" className="rounded-lg bg-red-50 p-3 text-xs text-red-600">{detailError}</p>}
              {detailBusy && <p className="py-12 text-center text-gray-400">正在读取 History…</p>}
              {!selected && !detailBusy && !detailError && <p className="py-12 text-center text-gray-400">选择左侧记录查看详情。</p>}
              {selected && <HistoryDetail unit={selected} onSelectHistory={select} />}
            </section>
          </div>
        </div>
      </div>,
      document.body,
    )}
  </>;
}

function HistoryDetail({ unit, onSelectHistory }: { unit: HistoryUnit; onSelectHistory: (historyId: string) => Promise<void> }) {
  const finalRevision = (unit.revisions || []).find(revision => revision?.revision_id === unit.final_revision_id);
  const intermediateCount = unit.events.filter(event => event.kind === 'revision' && event.revision_id !== unit.final_revision_id).length;
  return <div className="space-y-5">
    <div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-gray-500">
        <span>{labels[unit.kind]}</span><span>·</span><span>{unit.status}</span><span>·</span><span>记忆分析 {unit.analysis_status}</span>
      </div>
      <h3 className="mt-2 break-all text-base font-semibold text-gray-800">{unit.path || unit.events.find(event => event.kind === 'user_input')?.content.slice(0, 200) || '对话记录'}</h3>
      <p className="mt-1 break-all font-mono text-[10px] text-gray-400">History {unit.history_id}</p>
      {unit.previous_history_id && <button type="button" onClick={() => void onSelectHistory(unit.previous_history_id)} className="mt-1 break-all text-left font-mono text-[11px] text-purple-600 hover:underline">← 上一次 History {unit.previous_history_id}</button>}
      {unit.path && <p className="mt-1 break-all text-xs text-gray-500">基于 {unit.base_revision_id || '无'} · 最终 {unit.final_revision_id || '未保存完整版本'}</p>}
    </div>
    {unit.assistant_response && <section><h4 className="mb-2 text-xs font-medium text-gray-500">最终回复</h4><pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-lg bg-gray-50 p-3 text-xs leading-5 text-gray-700">{unit.assistant_response}</pre></section>}
    <section>
      <h4 className="mb-2 text-xs font-medium text-gray-500">过程证据 · {unit.events.length} 条</h4>
      <div className="space-y-2">
        {unit.events.map(event => <details key={event.event_id} className="rounded-lg border border-gray-100 px-3 py-2">
          <summary className="cursor-pointer text-xs text-gray-700">{event.kind} <span className="ml-2 text-gray-400">{event.content.slice(0, 90).replace(/\s+/g, ' ')}</span></summary>
          <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words text-xs leading-5 text-gray-600">{event.content}</pre>
        </details>)}
      </div>
    </section>
    {unit.kind === 'document' && <section>
      <h4 className="mb-2 text-xs font-medium text-gray-500">本次修改的最终正文</h4>
      {intermediateCount > 0 && <p className="mb-2 text-xs text-gray-400">本次 Agent 运行另有 {intermediateCount} 次中间写入，已保留在过程证据中。</p>}
      {!finalRevision && <p className="rounded-lg bg-amber-50 p-3 text-xs text-amber-700">{unit.final_revision_id ? '此 History 有修订引用，但最终正文无法核验；请查看过程证据。' : '本次没有产生新正文；请查看评价或失败证据。'}</p>}
      {finalRevision && <details className="mb-2 rounded-lg border border-gray-100 px-3 py-2">
        <summary className="cursor-pointer text-xs text-purple-700">最终修订 · {finalRevision.revision_id}</summary>
        <p className="mt-2 text-[11px] text-gray-400">底层修订 v{finalRevision.version_no} · 父修订 {finalRevision.parent_revision_id || '无'} · 依赖 {Object.keys(finalRevision.dependencies).length} 项</p>
        <pre className="mt-2 max-h-[32rem] overflow-auto whitespace-pre-wrap break-words rounded bg-gray-50 p-3 text-xs leading-5 text-gray-700">{finalRevision.body_md}</pre>
        {(Object.keys(finalRevision.dependencies).length > 0 || finalRevision.evidence.length > 0 || finalRevision.no_change.length > 0) &&
          <details className="mt-3 text-xs text-gray-600"><summary className="cursor-pointer">依赖版本与修订证据</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-50 p-3">{JSON.stringify({ dependencies: finalRevision.dependencies, evidence: finalRevision.evidence, no_change: finalRevision.no_change }, null, 2)}</pre></details>}
      </details>}
    </section>}
    {!!unit.previous_attempt_events?.length && <details className="text-xs text-gray-600"><summary className="cursor-pointer">上一次失败或中断的证据</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap">{JSON.stringify(unit.previous_attempt_events, null, 2)}</pre></details>}
    {!!unit.context?.length && <details className="text-xs text-gray-600"><summary className="cursor-pointer">模型上下文</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap">{JSON.stringify(unit.context, null, 2)}</pre></details>}
    {JSON.stringify(unit.selected_turns || []).length > 2 && <details className="text-xs text-gray-600"><summary className="cursor-pointer">对话回合选择</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap">{JSON.stringify(unit.selected_turns, null, 2)}</pre></details>}
    {!!unit.trace_ids?.length && <div className="break-all text-[11px] text-gray-400">来源 Trace：{unit.trace_ids.join(' · ')}</div>}
  </div>;
}
