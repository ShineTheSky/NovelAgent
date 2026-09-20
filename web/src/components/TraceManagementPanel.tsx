import { useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { fetchManagedTraces, fetchTrace, fetchTraceAgentStats, validateTrace } from '../api/client';
import type { TraceAgentStats, TraceEvidence, TraceValidation } from '../types/insights';
import { TraceDetailDialog } from './TraceDetailDialog';

const PAGE_SIZE = 25;
const errorText = (error: unknown) => error instanceof Error ? error.message : '加载 Trace 失败';

interface Filters {
  q: string;
  status: string;
  operationKind: string;
  analysisStatus: string;
  sourceAgent: string;
}

const emptyFilters: Filters = { q: '', status: '', operationKind: '', analysisStatus: '', sourceAgent: '' };

export function TraceManagementPanel({ projectId }: { projectId: string | null }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Filters>(emptyFilters);
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  const [items, setItems] = useState<TraceEvidence[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedTrace, setSelectedTrace] = useState<TraceEvidence | null>(null);
  const [validation, setValidation] = useState<TraceValidation | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [agentStats, setAgentStats] = useState<TraceAgentStats>({ sources: [], participants: [], total: 0 });
  const detailRequest = useRef(0);

  const load = async (pageOffset = offset, activeFilters = filters) => {
    if (!projectId) return;
    setBusy(true);
    setError(null);
    try {
      const page = await fetchManagedTraces(projectId, {
        q: activeFilters.q, status: activeFilters.status, operationKind: activeFilters.operationKind,
        analysisStatus: activeFilters.analysisStatus, sourceAgent: activeFilters.sourceAgent,
        limit: PAGE_SIZE, offset: pageOffset,
      });
      setItems(page.items);
      setTotal(page.total);
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setBusy(false);
    }
  };

  const loadAgentStats = async () => {
    if (!projectId) return;
    try {
      setAgentStats(await fetchTraceAgentStats(projectId));
    } catch (requestError) {
      setError(errorText(requestError));
    }
  };

  const openTrace = async (traceId: string, eventOffset = 0) => {
    const requestId = ++detailRequest.current;
    setSelectedTrace(null);
    setValidation(null);
    setDetailError(null);
    setDetailLoading(true);
    try {
      const [trace, result] = await Promise.all([fetchTrace(traceId, eventOffset), validateTrace(traceId)]);
      if (requestId !== detailRequest.current) return;
      setSelectedTrace(trace);
      setValidation(result);
    } catch (requestError) {
      if (requestId === detailRequest.current) setDetailError(errorText(requestError));
    } finally {
      if (requestId === detailRequest.current) setDetailLoading(false);
    }
  };

  const submitFilters = (event: React.FormEvent) => {
    event.preventDefault();
    const nextFilters = { ...draft };
    setOffset(0);
    setFilters(nextFilters);
    void load(0, nextFilters);
  };

  const clearFilters = () => {
    setDraft(emptyFilters);
    setFilters(emptyFilters);
    setOffset(0);
    void load(0, emptyFilters);
  };

  const closeDetail = () => {
    detailRequest.current += 1;
    setSelectedTrace(null);
    setValidation(null);
    setDetailError(null);
    setDetailLoading(false);
  };

  const pageStart = total ? offset + 1 : 0;
  const pageEnd = Math.min(offset + PAGE_SIZE, total);

  return <>
    <button type="button" onClick={() => { setOpen(true); void load(); void loadAgentStats(); }} disabled={!projectId} className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 shadow-sm hover:border-purple-200 hover:text-purple-600 disabled:opacity-40">Trace 管理</button>
    {open && createPortal(
      <div className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-labelledby="trace-management-title">
        <div className="relative flex h-[86vh] w-full max-w-6xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl">
          <header className="flex shrink-0 items-start justify-between border-b px-6 py-4">
            <div>
              <h2 id="trace-management-title" className="font-semibold text-gray-800">Trace 管理</h2>
              <p className="mt-1 text-xs text-gray-400">只读浏览、检索并校验当前项目的 Agent 执行记录。</p>
            </div>
            <button type="button" onClick={() => setOpen(false)} aria-label="关闭 Trace 管理" className="text-gray-400 hover:text-gray-700">✕</button>
          </header>

          <form onSubmit={submitFilters} className="grid shrink-0 grid-cols-1 gap-2 border-b bg-gray-50 px-5 py-3 md:grid-cols-[minmax(14rem,1fr)_11rem_11rem_12rem_10rem_auto]">
            <input value={draft.q} onChange={event => setDraft(current => ({ ...current, q: event.target.value }))} placeholder="Trace ID、Session ID 或用户输入" className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs outline-none focus:border-purple-300" />
            <select value={draft.sourceAgent} onChange={event => setDraft(current => ({ ...current, sourceAgent: event.target.value }))} className="rounded-lg border border-gray-200 bg-white px-2 py-2 text-xs text-gray-600">
              <option value="">全部来源 Agent</option>
              {agentStats.sources.map(item => <option key={item.agent} value={item.agent}>{item.agent} ({item.count})</option>)}
            </select>
            <select value={draft.status} onChange={event => setDraft(current => ({ ...current, status: event.target.value }))} className="rounded-lg border border-gray-200 bg-white px-2 py-2 text-xs text-gray-600">
              <option value="">全部运行状态</option>
              <option value="running">running</option><option value="completed">completed</option>
              <option value="failed">failed</option><option value="interrupted">interrupted</option>
              <option value="compressed">compressed</option>
            </select>
            <select value={draft.operationKind} onChange={event => setDraft(current => ({ ...current, operationKind: event.target.value }))} className="rounded-lg border border-gray-200 bg-white px-2 py-2 text-xs text-gray-600">
              <option value="">全部操作类型</option>
              <option value="conversation">conversation</option><option value="context_transition">context_transition</option>
              <option value="agent_run">agent_run</option><option value="routine">routine</option>
              <option value="tool_only">tool_only</option><option value="system">system</option>
            </select>
            <select value={draft.analysisStatus} onChange={event => setDraft(current => ({ ...current, analysisStatus: event.target.value }))} className="rounded-lg border border-gray-200 bg-white px-2 py-2 text-xs text-gray-600">
              <option value="">全部分析状态</option>
              <option value="pending">pending</option><option value="complete">complete</option><option value="skipped">skipped</option>
            </select>
            <div className="flex gap-2">
              <button type="submit" className="rounded-lg bg-purple-600 px-3 py-2 text-xs font-medium text-white hover:bg-purple-700">查询</button>
              <button type="button" onClick={clearFilters} className="rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs text-gray-500 hover:text-gray-700">清空</button>
            </div>
          </form>

          <section className="shrink-0 border-b px-5 py-3 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-gray-600">来源 Agent</span>
              {agentStats.sources.map(item => <button type="button" key={item.agent} onClick={() => { const next = { ...filters, sourceAgent: item.agent }; setDraft(next); setFilters(next); setOffset(0); void load(0, next); }} className={`rounded-full px-2 py-1 ${filters.sourceAgent === item.agent ? 'bg-purple-100 text-purple-700' : 'bg-gray-100 text-gray-600 hover:bg-purple-50'}`}>{item.agent} · {item.count}</button>)}
              {!agentStats.sources.length && <span className="text-gray-400">暂无统计</span>}
            </div>
            {!!agentStats.participants.length && <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className="font-medium text-gray-500">参与 Agent</span>
              {agentStats.participants.map(item => <span key={item.agent} className="rounded-full bg-blue-50 px-2 py-1 text-blue-600">{item.agent} · {item.count}</span>)}
            </div>}
          </section>

          <main className="min-h-0 flex-1 overflow-y-auto p-5">
            {error && <div role="alert" className="mb-3 rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-600">{error}</div>}
            {busy && !items.length && <div className="py-20 text-center text-sm text-gray-400">正在加载 Trace…</div>}
            <div className="space-y-2">
              {items.map(trace => <button type="button" key={trace.trace_id} onClick={() => void openTrace(trace.trace_id)} className="grid w-full grid-cols-1 gap-3 rounded-xl border border-gray-100 bg-white p-4 text-left shadow-sm transition hover:border-purple-200 hover:bg-purple-50/20 md:grid-cols-[minmax(0,1fr)_10rem_8rem_9rem_8rem]">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate font-mono text-xs font-medium text-purple-700">{trace.trace_id}</span>
                    {trace.turn_no !== null && trace.turn_no !== undefined && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">Turn {trace.turn_no}</span>}
                    {!!trace.next_trace_id && <span className="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] text-blue-600">有后继</span>}
                    {!!trace.previous_trace_id && <span className="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] text-blue-600">有前驱</span>}
                  </div>
                  <p className="mt-2 truncate text-xs text-gray-500">{trace.user_message || '（无用户输入）'}</p>
                  <p className="mt-1 truncate font-mono text-[10px] text-gray-400">Session {trace.session_id}</p>
                </div>
                <TraceCell label="来源 Agent" value={trace.source_agent || 'unknown'} />
                <TraceCell label="操作类型" value={trace.operation_kind} />
                <TraceCell label="状态" value={trace.status} tone={trace.status === 'failed' ? 'red' : trace.status === 'running' ? 'amber' : 'green'} />
                <div className="text-xs text-gray-500">
                  <div className="text-[10px] text-gray-400">事件 / Token</div>
                  <div className="mt-1">{trace.event_count ?? 0} / {trace.token_count ?? 0}</div>
                  <div className="mt-1 text-[10px] text-gray-400">{formatDateTime(trace.started_at)}</div>
                </div>
              </button>)}
            </div>
            {!busy && !items.length && !error && <div className="py-20 text-center text-sm text-gray-400">没有符合条件的 Trace。</div>}
          </main>

          <footer className="flex shrink-0 items-center justify-between border-t bg-gray-50 px-5 py-3 text-xs text-gray-500">
            <span>共 {total} 条，当前 {pageStart}–{pageEnd}</span>
            <div className="flex gap-2">
              <button type="button" disabled={offset === 0 || busy} onClick={() => { const next = Math.max(0, offset - PAGE_SIZE); setOffset(next); void load(next); }} className="rounded-md border bg-white px-3 py-1.5 disabled:opacity-40">上一页</button>
              <button type="button" disabled={offset + PAGE_SIZE >= total || busy} onClick={() => { const next = offset + PAGE_SIZE; setOffset(next); void load(next); }} className="rounded-md border bg-white px-3 py-1.5 disabled:opacity-40">下一页</button>
              <button type="button" onClick={() => { void load(); void loadAgentStats(); }} disabled={busy} className="rounded-md border bg-white px-3 py-1.5 text-purple-600 disabled:opacity-40">{busy ? '刷新中…' : '刷新'}</button>
            </div>
          </footer>

          {(selectedTrace || detailLoading || detailError) && <TraceDetailDialog
            trace={selectedTrace} loading={detailLoading} error={detailError} validation={validation}
            onOpenTrace={traceId => void openTrace(traceId)}
            onEventPage={eventOffset => selectedTrace && void openTrace(selectedTrace.trace_id, eventOffset)}
            onClose={closeDetail}
          />}
        </div>
      </div>,
      document.body,
    )}
  </>;
}

function TraceCell({ label, value, tone }: { label: string; value: string; tone?: 'red' | 'amber' | 'green' }) {
  const color = tone === 'red' ? 'text-red-600' : tone === 'amber' ? 'text-amber-600' : tone === 'green' ? 'text-emerald-600' : 'text-gray-600';
  return <div className="min-w-0 text-xs">
    <div className="text-[10px] text-gray-400">{label}</div>
    <div title={value} className={`mt-1 truncate ${color}`}>{value || '-'}</div>
  </div>;
}

function formatDateTime(value?: string) {
  return value ? value.slice(0, 16).replace('T', ' ') : '-';
}
