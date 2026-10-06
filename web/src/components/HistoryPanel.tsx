import { useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { fetchHistoryFiles, fetchHistoryUnit, fetchProjectHistory } from '../api/client';
import type { HistoryFilePage, HistoryKind, HistorySummary, HistoryUnit } from '../types/history';

const PAGE_SIZE = 25;
const errorText = (error: unknown) => error instanceof Error ? error.message : '加载 History 失败';
const labels: Record<HistoryKind, string> = { document: '文件', conversation: '对话', requirement: '需求', review: '审阅' };

export function HistoryPanel({ projectId }: { projectId: string | null }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<'files' | 'conversation' | 'requirement' | 'review'>('files');
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
      setMode(kind === 'document' ? 'files' : kind);
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
            <button type="button" onClick={() => void loadHistory('requirement')} className={`rounded-lg px-3 py-1.5 ${mode === 'requirement' ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-purple-600'}`}>需求</button>
            <button type="button" onClick={() => void loadHistory('review')} className={`rounded-lg px-3 py-1.5 ${mode === 'review' ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-purple-600'}`}>审阅</button>
            <button type="button" disabled={busy} onClick={() => void (selectedFile ? loadHistory('document', selectedFile, offset) : mode === 'files' ? loadFiles(offset) : loadHistory(mode, '', offset))} className="ml-auto text-purple-600 disabled:opacity-40">{busy ? '加载中…' : '刷新'}</button>
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
                {(selectedFile || mode !== 'files') && items.map(item => <button key={item.history_id} type="button" onClick={() => void select(item.history_id)} className={`w-full rounded-xl border p-3 text-left text-xs transition ${selected?.history_id === item.history_id ? 'border-purple-300 bg-purple-50' : 'border-gray-100 hover:border-purple-200 hover:bg-gray-50'}`}>
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
                {!busy && !error && !(selectedFile || mode !== 'files' ? items : files).length && <p className="py-12 text-center text-sm text-gray-400">当前项目还没有 History。</p>}
              </div>
              <footer className="flex shrink-0 items-center justify-between border-t px-4 py-3 text-xs text-gray-500">
                <span>共 {total} 条 · {total ? offset + 1 : 0}–{Math.min(offset + PAGE_SIZE, total)}</span>
                <div className="flex gap-2">
                  <button type="button" disabled={offset === 0 || busy} onClick={() => void (selectedFile ? loadHistory('document', selectedFile, offset - PAGE_SIZE) : mode === 'files' ? loadFiles(offset - PAGE_SIZE) : loadHistory(mode, '', offset - PAGE_SIZE))} className="rounded border px-2 py-1 disabled:opacity-40">上一页</button>
                  <button type="button" disabled={offset + PAGE_SIZE >= total || busy} onClick={() => void (selectedFile ? loadHistory('document', selectedFile, offset + PAGE_SIZE) : mode === 'files' ? loadFiles(offset + PAGE_SIZE) : loadHistory(mode, '', offset + PAGE_SIZE))} className="rounded border px-2 py-1 disabled:opacity-40">下一页</button>
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

export function HistoryDetail({ unit, onSelectHistory }: { unit: HistoryUnit; onSelectHistory: (historyId: string) => void | Promise<void> }) {
  const finalRevision = (unit.revisions || []).find(revision => revision?.revision_id === unit.final_revision_id);
  const intermediateCount = unit.events.filter(event => event.kind === 'revision' && event.revision_id !== unit.final_revision_id).length;
  const reviewRetelling = unit.kind === 'review' && unit.events.some(event => event.kind === 'review_retelling');
  const requirementId = unit.canonical_requirement_history_id || unit.requirement_history_id;
  return <div className="space-y-5">
    <div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-gray-500">
        <span>{labels[unit.kind]}</span><span>·</span><span>{unit.status}</span><span>·</span><span>记忆分析 {unit.analysis_status}</span>
      </div>
      <h3 className="mt-2 break-all text-base font-semibold text-gray-800">{unit.path || unit.events.find(event => event.kind === 'user_input')?.content.slice(0, 200) || '对话记录'}</h3>
      <p className="mt-1 break-all font-mono text-[10px] text-gray-400">History {unit.history_id}</p>
      {reviewRetelling && <p className="mt-2 rounded-lg bg-amber-50 p-3 text-xs text-amber-700">此节点保存主 Agent 的审阅转述，不是审阅 Agent 的原始报告；原始审阅报告和被审版本未核验。不作为一次新的独立审阅支持。</p>}
      {requirementId && requirementId !== unit.history_id && <button type="button" onClick={() => void onSelectHistory(requirementId)} className="mt-1 break-all text-left font-mono text-[11px] text-purple-600 hover:underline">需求 History {requirementId}</button>}
      {unit.previous_history_id && <button type="button" onClick={() => void onSelectHistory(unit.previous_history_id)} className="mt-1 break-all text-left font-mono text-[11px] text-purple-600 hover:underline">← 上一次 History {unit.previous_history_id}</button>}
      {unit.review_history_id && <button type="button" onClick={() => void onSelectHistory(unit.review_history_id!)} className="mt-1 block break-all text-left text-xs text-purple-600 hover:underline">来源审阅 History {unit.review_history_id}</button>}
      {unit.target_history_id && <button type="button" onClick={() => void onSelectHistory(unit.target_history_id!)} className="mt-1 block break-all text-left text-xs text-purple-600 hover:underline">被审文件 History {unit.target_history_id}</button>}
      {unit.path && <p className="mt-1 break-all text-xs text-gray-500">基于 {unit.base_revision_id || '无'} · 最终 {unit.final_revision_id || '未保存完整版本'}</p>}
    </div>
    {!!unit.attempts?.length && <section><h4 className="mb-2 text-xs font-medium text-gray-500">同一需求的中断与续接 · {unit.attempts.length} 次</h4><p className="mb-2 text-xs text-gray-500">合并需求身份，保留每次执行的原始事件与文件版本。下方证据按执行顺序展示。</p>{unit.attempts.map((attempt, index) => <p key={attempt.history_id} className="break-all py-1 font-mono text-[11px] text-gray-600">{index + 1}. {attempt.history_id} · {attempt.status}{attempt.reason ? ` · ${attempt.reason}` : ''}</p>)}</section>}
    {!!unit.affected_files?.length && <section><h4 className="mb-2 text-xs font-medium text-gray-500">落实到文件 · {unit.affected_files.length} 条 History</h4><div className="space-y-2">{unit.affected_files.map(file => <button key={file.history_id} type="button" onClick={() => void onSelectHistory(file.history_id)} className="block w-full break-all rounded-lg border border-gray-100 p-3 text-left text-xs text-purple-700 hover:bg-purple-50">{file.path} · {file.status}<span className="mt-1 block font-mono text-[10px]">{file.history_id}</span></button>)}</div></section>}
    {!!unit.reviews?.length && <section><h4 className="mb-2 text-xs text-gray-500">独立审阅节点（可直接作为记忆来源）</h4>{unit.reviews.map(review => <button key={review.history_id} type="button" onClick={() => void onSelectHistory(review.history_id)} className="block break-all py-2 text-left text-xs text-purple-700">{review.path} · {review.status} · {review.history_id}</button>)}</section>}
    {!!unit.result_histories?.length && <section><h4 className="mb-2 text-xs text-gray-500">审阅后的落实 History</h4>{unit.result_histories.map(file => <button key={file.history_id} type="button" onClick={() => void onSelectHistory(file.history_id)} className="block break-all py-2 text-left text-xs text-purple-700">{file.path} · {file.status} · {file.history_id}</button>)}</section>}
    {unit.assistant_response && <section><h4 className="mb-2 text-xs font-medium text-gray-500">最终回复</h4><pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-lg bg-gray-50 p-3 text-xs leading-5 text-gray-700">{unit.assistant_response}</pre></section>}
    {!!unit.body_versions?.length && <section>
      <h4 className="mb-2 text-xs font-medium text-gray-500">正文版本引用</h4>
      <p className="mb-2 text-xs text-gray-400">Read／Write 过程只显示版本引用。正文按版本按需读取；无法定位的记录标为 unresolved，原始证据仍保留。</p>
      {unit.body_versions.map(version => <p key={version.revision_id} className="break-all py-1 font-mono text-[11px] text-gray-600">
        {version.path} · {version.revision_id} · {version.status === 'missing' ? '正文版本缺失' : `存档可读取 · ${version.total_lines} 行`}
      </p>)}
    </section>}
    <section>
      <h4 className="mb-2 text-xs font-medium text-gray-500">过程证据 · {unit.events.length} 条</h4>
      <div className="space-y-2">
        {unit.events.map(event => <details key={event.event_id} className="rounded-lg border border-gray-100 px-3 py-2">
          <summary className="cursor-pointer text-xs text-gray-700">{event.kind}{event.inherited_requirement ? ' · 引用的需求' : ''}{event.inherited_review ? ' · 引用的审阅' : event.inherited_process ? ' · 引用的过程' : ''} <span className="ml-2 text-gray-400">{event.content.slice(0, 90).replace(/\s+/g, ' ')}</span></summary>
          <p className="mt-2 break-all font-mono text-[10px] text-gray-400">{event.event_id}</p>
          {event.inherited_process && event.evidence_history_id && <button type="button" onClick={() => void onSelectHistory(event.evidence_history_id!)} className="mt-1 break-all text-left font-mono text-[11px] text-purple-600 hover:underline">过程所属 History {event.evidence_history_id}</button>}
          {event.order_status && <p className="mt-1 text-[11px] text-gray-500">{event.order_status === 'unverified' ? '顺序未核验' : event.occurred_at ? `发生于 ${new Date(event.occurred_at).toLocaleString()}` : '已按原始事件顺序还原 · 时间缺失'}</p>}
          {(event.scope?.path || event.scope?.run_id) && <p className="mt-1 break-all text-[11px] text-gray-500">作用范围：{event.scope.path || event.scope.preset || event.scope.run_id}</p>}
          {event.scope?.evidence_scope === 'run' && <p className="mt-1 text-[11px] text-gray-500">本次 Agent 运行的共享上下文，不代表专门针对此文件。</p>}
          <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words text-xs leading-5 text-gray-600">{event.content}</pre>
        </details>)}
      </div>
    </section>
    {!!unit.archived_events?.length && <details className="text-xs text-gray-600"><summary className="cursor-pointer">原始聚合／重复证据 · {unit.archived_events.length} 条（保留原 ID，不作为新的过程事件）</summary><div className="mt-2 space-y-2">{unit.archived_events.map(event => <details key={event.event_id} className="rounded-lg border border-gray-100 p-3"><summary className="cursor-pointer">{event.kind} · {event.order_status === 'duplicate' ? '重复记录' : '已拆分为顺序证据'}</summary><p className="mt-2 break-all font-mono text-[10px]">{event.event_id}</p><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words">{event.content}</pre></details>)}</div></details>}
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
    {unit.kind === 'review' && <section><h4 className="mb-2 text-xs text-gray-500">被审阅的正文版本 · {unit.base_revision_id || '未定位'}</h4>{unit.revisions?.map(revision => revision && <details key={revision.revision_id} className="text-xs"><summary className="cursor-pointer">查看被审正文（审阅不创建新版本）</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap">{revision.body_md}</pre></details>)}</section>}
    {!!unit.previous_attempt_events?.length && <details className="text-xs text-gray-600"><summary className="cursor-pointer">上一次失败或中断的证据</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap">{JSON.stringify(unit.previous_attempt_events, null, 2)}</pre></details>}
    {!!unit.context?.length && <details className="text-xs text-gray-600"><summary className="cursor-pointer">模型上下文</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap">{JSON.stringify(unit.context, null, 2)}</pre></details>}
    {JSON.stringify(unit.selected_turns || []).length > 2 && <details className="text-xs text-gray-600"><summary className="cursor-pointer">对话回合选择</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap">{JSON.stringify(unit.selected_turns, null, 2)}</pre></details>}
    {!!unit.trace_ids?.length && <div className="break-all text-[11px] text-gray-400">来源 Trace：{unit.trace_ids.join(' · ')}</div>}
  </div>;
}
