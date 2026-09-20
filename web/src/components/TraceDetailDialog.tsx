import { useMemo } from 'react';
import type { TraceEvent, TraceEvidence, TraceValidation } from '../types/insights';

interface TraceDetailDialogProps {
  trace: TraceEvidence | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
  validation?: TraceValidation | null;
  onOpenTrace?: (traceId: string) => void;
  onEventPage?: (offset: number) => void;
}

export function TraceDetailDialog({ trace, loading, error, onClose, validation, onOpenTrace, onEventPage }: TraceDetailDialogProps) {
  const events = useMemo(() => compactTraceEvents(trace?.events ?? []), [trace?.events]);
  const loadedEvents = events.length;
  const rawLoadedEvents = trace?.events?.length ?? 0;
  const eventOffset = trace?.event_offset ?? 0;
  const totalEvents = trace?.event_count ?? loadedEvents;

  return (
    <div className="absolute inset-0 z-30 flex flex-col bg-white" role="dialog" aria-modal="true" aria-labelledby="trace-detail-title">
      <header className="flex shrink-0 items-start gap-4 border-b border-gray-100 px-6 py-5">
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium uppercase tracking-wide text-purple-600">Trace 详情</p>
          <h3 id="trace-detail-title" className="mt-1 break-all font-mono text-sm font-semibold text-gray-800">
            {trace?.trace_id ?? '正在读取…'}
          </h3>
        </div>
        <button type="button" onClick={onClose} aria-label="关闭 Trace 详情" className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-700">✕</button>
      </header>

      <main className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
        {loading && <div className="py-16 text-center text-sm text-gray-400">正在读取 Trace 完整内容…</div>}
        {!loading && error && <div role="alert" className="rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-600">{error}</div>}
        {!loading && !error && trace && <div className="space-y-5">
          <section className="grid grid-cols-2 gap-3 rounded-xl bg-gray-50 p-4 text-xs text-gray-600 md:grid-cols-4">
            <Metadata label="Session" value={trace.session_id} mono />
            <Metadata label="Project" value={trace.project_id} mono />
            <Metadata label="状态" value={trace.status} />
            <Metadata label="来源" value={trace.source === 'historical' ? '历史导入' : '实时采集'} />
            <Metadata label="操作类型" value={trace.operation_kind} />
            <Metadata label="来源 Agent" value={trace.source_agent} mono />
            <Metadata label="Agent Position" value={trace.agent_position} mono />
            <Metadata label="分析状态" value={trace.analysis_status} />
            <Metadata label="Token" value={String(trace.token_count ?? 0)} />
            <Metadata label="事件" value={`${rawLoadedEvents}${totalEvents > rawLoadedEvents ? ` / ${totalEvents}` : ''}`} />
            <Metadata label="开始" value={formatDateTime(trace.started_at)} />
            <Metadata label="结束" value={formatDateTime(trace.finished_at)} />
            {trace.turn_no !== undefined && trace.turn_no !== null && <Metadata label="会话轮次" value={String(trace.turn_no)} />}
            {trace.previous_trace_id && <TraceLink label="上一 Trace" traceId={trace.previous_trace_id} onOpenTrace={onOpenTrace} />}
            {trace.next_trace_id && <TraceLink label="下一 Trace" traceId={trace.next_trace_id} onOpenTrace={onOpenTrace} />}
          </section>

          {validation && <section className={`rounded-xl border p-4 ${validation.valid ? 'border-emerald-100 bg-emerald-50/50' : 'border-red-100 bg-red-50/50'}`}>
            <div className="mb-3 flex items-center justify-between gap-3">
              <h4 className="text-sm font-semibold text-gray-700">完整性校验</h4>
              <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${validation.valid ? 'bg-emerald-100 text-emerald-700' : 'bg-red-100 text-red-700'}`}>{validation.valid ? '链路正常' : '发现异常'}</span>
            </div>
            <div className="space-y-2">
              {validation.checks.map(check => <div key={check.key} className="flex items-start gap-2 text-xs leading-5 text-gray-600">
                <span className={check.status === 'pass' ? 'text-emerald-600' : check.status === 'warning' ? 'text-amber-600' : 'text-red-600'}>{check.status === 'pass' ? '✓' : check.status === 'warning' ? '!' : '✕'}</span>
                <span>{check.message}</span>
              </div>)}
            </div>
            {validation.chain_trace_ids.length > 1 && <div className="mt-3 flex flex-wrap items-center gap-1 text-[11px]">
              <span className="mr-1 text-gray-400">链路</span>
              {validation.chain_trace_ids.map((traceId, index) => <span key={traceId} className="flex items-center gap-1">
                {index > 0 && <span className="text-gray-300">→</span>}
                <button type="button" onClick={() => onOpenTrace?.(traceId)} disabled={!onOpenTrace || traceId === trace.trace_id} title={traceId} className="max-w-44 truncate rounded bg-white px-1.5 py-0.5 font-mono text-purple-600 disabled:text-gray-500">{traceId}</button>
              </span>)}
            </div>}
          </section>}

          <TextSection title="用户输入" content={trace.user_message} empty="（无用户输入）" />
          <TextSection title="最终回答" content={trace.final_answer} empty="（无最终回答）" />

          {trace.classification?.items.length ? <section>
            <h4 className="mb-2 text-sm font-semibold text-gray-700">后台判别</h4>
            <div className="space-y-2">
              {trace.classification.items.map(item => <div key={`${item.event_id}-${item.type}`} className="rounded-lg border border-violet-100 bg-violet-50/50 px-3 py-2 text-xs leading-5 text-violet-800">
                <span className="mr-2 font-medium">{classificationLabel(item.type)}</span>
                {item.summary}
                <span className="ml-2 text-violet-400">{Math.round(item.confidence * 100)}%</span>
              </div>)}
            </div>
          </section> : null}

          <section>
            <div className="mb-2 flex items-center justify-between gap-3">
              <h4 className="text-sm font-semibold text-gray-700">事件记录</h4>
              <span className="text-[11px] text-gray-400">已加载 {eventOffset + (rawLoadedEvents ? 1 : 0)}–{eventOffset + rawLoadedEvents} / {totalEvents}</span>
            </div>
            {totalEvents > rawLoadedEvents && <p className="mb-3 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">该 Trace 共 {totalEvents} 个事件，当前显示从第 {eventOffset + 1} 条开始的 {rawLoadedEvents} 条。</p>}
            <div className="space-y-3">
              {events.map(event => <article key={event.event_id} className="rounded-xl border border-gray-100 bg-white p-4 shadow-sm">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-400">
                  <span className="font-medium text-gray-600">#{event.sequence_no}</span>
                  <span>{event.actor}</span>
                  <span className="rounded bg-gray-100 px-1.5 py-0.5 font-mono text-gray-600">{event.event_type}</span>
                  {event.duration_ms !== undefined && event.duration_ms !== null && <span>{Math.round(event.duration_ms)} ms</span>}
                  <span className="ml-auto">{formatDateTime(event.created_at)}</span>
                </div>
                <div className="mt-3 space-y-3">
                  {Object.entries(event.payload ?? {}).map(([key, value]) => <div key={key}>
                    <div className="mb-1 text-[11px] font-medium text-gray-400">{payloadLabel(key)}</div>
                    <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-gray-50 p-3 font-mono text-xs leading-5 text-gray-700">{formatPayloadValue(value)}</pre>
                  </div>)}
                  {Object.keys(event.payload ?? {}).length === 0 && <div className="text-xs text-gray-400">（空 payload）</div>}
                </div>
              </article>)}
              {!events.length && <div className="rounded-xl border border-dashed border-gray-200 py-10 text-center text-sm text-gray-400">该 Trace 没有事件。</div>}
            </div>
            {onEventPage && totalEvents > rawLoadedEvents && <div className="mt-4 flex justify-end gap-2">
              <button type="button" disabled={eventOffset === 0} onClick={() => onEventPage(Math.max(0, eventOffset - 500))} className="rounded-md border px-3 py-1.5 text-xs text-gray-600 disabled:opacity-40">上一批事件</button>
              <button type="button" disabled={eventOffset + rawLoadedEvents >= totalEvents} onClick={() => onEventPage(eventOffset + rawLoadedEvents)} className="rounded-md border px-3 py-1.5 text-xs text-purple-600 disabled:opacity-40">下一批事件</button>
            </div>}
          </section>
        </div>}
      </main>
    </div>
  );
}

function TraceLink({ label, traceId, onOpenTrace }: { label: string; traceId: string; onOpenTrace?: (traceId: string) => void }) {
  return <div className="min-w-0">
    <div className="text-[11px] text-gray-400">{label}</div>
    {onOpenTrace
      ? <button type="button" onClick={() => onOpenTrace(traceId)} title={traceId} className="mt-1 break-all text-left font-mono text-[11px] text-purple-600 hover:underline">{traceId}</button>
      : <div title={traceId} className="mt-1 break-all font-mono text-[11px]">{traceId}</div>}
  </div>;
}

function Metadata({ label, value, mono = false }: { label: string; value?: string; mono?: boolean }) {
  return <div className="min-w-0">
    <div className="text-[11px] text-gray-400">{label}</div>
    <div title={value} className={`mt-1 break-all ${mono ? 'font-mono text-[11px]' : ''}`}>{value || '-'}</div>
  </div>;
}

function TextSection({ title, content, empty }: { title: string; content?: string; empty: string }) {
  return <section>
    <h4 className="mb-2 text-sm font-semibold text-gray-700">{title}</h4>
    <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-gray-50 p-4 font-sans text-sm leading-6 text-gray-700">{content || empty}</pre>
  </section>;
}

function formatPayloadValue(value: unknown) {
  if (typeof value === 'string') return value;
  if (value === undefined) return 'undefined';
  return JSON.stringify(value, null, 2);
}

function formatDateTime(value?: string) {
  return value ? value.replace('T', ' ').replace('Z', ' UTC') : '-';
}

function payloadLabel(key: string) {
  return ({
    content: '内容',
    delta: '内容',
    stream_chunk_count: '合并的原始片段数',
    reasoning_content: '思考内容',
    message: '消息',
    tool: '工具',
    params: '参数',
    data: '结果',
    error: '错误',
    revision_events: '修订事件',
  } as Record<string, string>)[key] ?? key;
}

function classificationLabel(type: string) {
  return ({ error: '错误', correction: '修正', confirmation: '肯定', feedback: '反馈' } as Record<string, string>)[type] ?? type;
}

function compactTraceEvents(events: TraceEvent[]) {
  const streamTypes = new Set(['subagent_thinking', 'subagent_text_delta', 'workflow_thinking', 'workflow_text_delta']);
  const compacted: TraceEvent[] = [];
  let previousSignature = '';
  for (const event of events) {
    const field = event.event_type.endsWith('_thinking') ? 'content' : event.event_type.endsWith('_text_delta') ? 'delta' : '';
    if (!field || !streamTypes.has(event.event_type)) {
      compacted.push(event);
      previousSignature = '';
      continue;
    }
    const metadata = Object.fromEntries(Object.entries(event.payload).filter(([key]) => key !== field && key !== 'stream_chunk_count'));
    const signature = JSON.stringify([event.event_type, event.actor, event.parent_event_id ?? null, metadata]);
    if (compacted.length && signature === previousSignature) {
      const previous = compacted[compacted.length - 1];
      const previousCount = Number(previous.payload.stream_chunk_count ?? 1);
      const incomingCount = Number(event.payload.stream_chunk_count ?? 1);
      previous.payload = {
        ...previous.payload,
        [field]: String(previous.payload[field] ?? '') + String(event.payload[field] ?? ''),
        stream_chunk_count: previousCount + incomingCount,
      };
    } else {
      compacted.push({ ...event, payload: { ...event.payload } });
    }
    previousSignature = signature;
  }
  return compacted;
}
