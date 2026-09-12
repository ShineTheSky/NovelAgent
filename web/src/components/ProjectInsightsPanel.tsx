import { useCallback, useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { downgradeTraceMemory, fetchProjectRules, fetchProjectTraces, fetchTrace, fetchTraceMemories, fetchTraceMemory } from '../api/client';
import type { TraceEvent, TraceEvidence, TraceMemory } from '../types/insights';

type InsightTab = 'traces' | 'memories' | 'rules';

const kindLabel: Record<string, string> = {
  preference: '用户偏好',
  issue: '问题规律',
  project_fact: '项目记忆',
};

function errorText(error: unknown) {
  return error instanceof Error ? error.message : '加载项目洞察失败';
}

function formatDate(value: string) {
  return value ? value.replace('T', ' ').slice(0, 16) : '';
}

interface ProjectInsightsPanelProps {
  projectId: string | null;
}

export function ProjectInsightsPanel({ projectId }: ProjectInsightsPanelProps) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<InsightTab>('traces');
  const [traces, setTraces] = useState<TraceEvidence[]>([]);
  const [rules, setRules] = useState<TraceMemory[]>([]);
  const [memories, setMemories] = useState<TraceMemory[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedMemoryId, setExpandedMemoryId] = useState<string | null>(null);
  const [memoryContents, setMemoryContents] = useState<Record<string, string>>({});
  const [contentLoadingId, setContentLoadingId] = useState<string | null>(null);
  const [expandedTraceId, setExpandedTraceId] = useState<string | null>(null);
  const [traceDetails, setTraceDetails] = useState<Record<string, TraceEvidence>>({});
  const [traceLoadingId, setTraceLoadingId] = useState<string | null>(null);
  const [downgradeTarget, setDowngradeTarget] = useState<TraceMemory | null>(null);
  const [downgrading, setDowngrading] = useState(false);

  const load = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    setError(null);
    try {
      const [nextTraces, nextRules, nextMemories] = await Promise.all([
        fetchProjectTraces(projectId),
        fetchProjectRules(projectId),
        fetchTraceMemories(projectId),
      ]);
      setTraces(nextTraces);
      setRules(nextRules);
      setMemories(nextMemories);
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  const showPanel = () => {
    if (!projectId) return;
    setOpen(true);
    setTab('traces');
  };

  useEffect(() => {
    if (open) void load();
  }, [load, open]);

  const toggleMemory = async (memory: TraceMemory) => {
    if (expandedMemoryId === memory.memory_id) {
      setExpandedMemoryId(null);
      return;
    }
    setExpandedMemoryId(memory.memory_id);
    if (memoryContents[memory.memory_id] !== undefined || !projectId) return;
    setContentLoadingId(memory.memory_id);
    try {
      const detail = await fetchTraceMemory(projectId, memory.memory_id);
      setMemoryContents(current => ({ ...current, [memory.memory_id]: detail.content ?? memory.claim }));
    } catch (requestError) {
      setMemoryContents(current => ({ ...current, [memory.memory_id]: `⚠️ ${errorText(requestError)}` }));
    } finally {
      setContentLoadingId(null);
    }
  };

  const activeRules = useMemo(() => rules.filter(rule => rule.status === 'rule'), [rules]);

  const toggleTrace = async (trace: TraceEvidence) => {
    if (expandedTraceId === trace.trace_id) {
      setExpandedTraceId(null);
      return;
    }
    setExpandedTraceId(trace.trace_id);
    if (traceDetails[trace.trace_id]) return;
    setTraceLoadingId(trace.trace_id);
    try {
      const detail = await fetchTrace(trace.trace_id);
      setTraceDetails(current => ({ ...current, [trace.trace_id]: detail }));
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setTraceLoadingId(null);
    }
  };

  const confirmDowngrade = async () => {
    if (!projectId || !downgradeTarget) return;
    setDowngrading(true);
    setError(null);
    try {
      await downgradeTraceMemory(projectId, downgradeTarget.memory_id);
      setExpandedMemoryId(null);
      setDowngradeTarget(null);
      await load();
    } catch (requestError) {
      setError(errorText(requestError));
    } finally {
      setDowngrading(false);
    }
  };

  const downgradeDescription = downgradeTarget?.status === 'rule'
    ? '该规则会降为 Memory。当前正在运行的 Agent 已构建的 Prompt 不会改动；下一次新 Agent 请求或新会话构建 Prompt、或上下文压缩重建 memory.md 后，才会按新的层级生效。'
    : '该 Memory 会降低权重并降回 Trace 层，从下一次 memory.md 重建起移出快照。原始证据会保留，之后出现相似 Trace 时仍可重新激活并提高权重。当前对话上下文不会改动。';

  return (
    <>
      <button
        type="button"
        onClick={showPanel}
        disabled={!projectId}
        className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs text-gray-600 shadow-sm hover:border-purple-200 hover:text-purple-600 transition-colors disabled:cursor-not-allowed disabled:opacity-40"
        aria-label="打开项目洞察与记忆"
      >项目洞察</button>
      {open && createPortal(
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" aria-labelledby="project-insights-title">
          <div className="relative w-full max-w-3xl max-h-[85vh] overflow-hidden rounded-2xl bg-white shadow-2xl mx-4 flex flex-col">
            <div className="px-6 pt-6 pb-4 pr-14 border-b border-gray-100 shrink-0">
              <div>
                <h2 id="project-insights-title" className="font-semibold text-gray-800">项目洞察与记忆</h2>
                <p className="mt-1 text-xs text-gray-400">Trace 保留证据；强信号可形成 Memory；相似证据会强化 Memory，达标后成为项目 Rule。</p>
              </div>
            </div>
            <button type="button" onClick={() => setOpen(false)} aria-label="关闭项目洞察" className="absolute right-4 top-4 z-10 flex h-8 w-8 items-center justify-center rounded-lg text-gray-400 hover:bg-gray-100 hover:text-gray-700">✕</button>

            <div className="px-6 pt-4 shrink-0 flex items-center justify-between gap-3">
              <div className="flex gap-1 rounded-xl bg-gray-100 p-1" role="tablist" aria-label="项目洞察分类">
                <button type="button" role="tab" aria-selected={tab === 'traces'} onClick={() => setTab('traces')} className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${tab === 'traces' ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>Trace 证据 {traces.length ? `(${traces.length})` : ''}</button>
                <button type="button" role="tab" aria-selected={tab === 'memories'} onClick={() => setTab('memories')} className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${tab === 'memories' ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>原子记忆 {memories.length ? `(${memories.length})` : ''}</button>
                <button type="button" role="tab" aria-selected={tab === 'rules'} onClick={() => setTab('rules')} className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${tab === 'rules' ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>项目规则 {activeRules.length ? `(${activeRules.length})` : ''}</button>
              </div>
              <button type="button" onClick={() => void load()} disabled={loading} className="text-xs text-purple-600 hover:text-purple-700 disabled:opacity-40">{loading ? '刷新中…' : '刷新'}</button>
            </div>

            <div className="flex-1 overflow-y-auto px-6 py-4">
              {loading && <div className="py-14 text-center text-sm text-gray-400">正在读取项目洞察…</div>}
              {!loading && error && <div role="alert" className="rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-600">{error}</div>}

              {!loading && !error && tab === 'rules' && (
                <div className="space-y-3" role="tabpanel">
                  {activeRules.map(rule => <article key={rule.memory_id} className="rounded-xl border border-emerald-100 bg-emerald-50/40 p-4">
                    <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]">
                      <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-emerald-700">生效中</span>
                      <span className="text-gray-500">{kindLabel[rule.kind] ?? rule.kind}</span>
                      {rule.subtype && <span className="text-gray-400">{rule.subtype}</span>}
                      <span className="ml-auto text-gray-400">重要性 {Math.round(rule.importance)}</span>
                      <button type="button" onClick={() => setDowngradeTarget(rule)} className="rounded-md border border-amber-200 bg-white px-2 py-0.5 text-amber-700 hover:bg-amber-50">降为记忆</button>
                    </div>
                    <p className="text-sm leading-6 text-gray-700">{rule.claim}</p>
                    {rule.when_text && rule.then_text && <p className="mt-2 rounded-lg bg-white/70 px-3 py-2 text-xs leading-5 text-gray-600">当 {rule.when_text}：{rule.then_text}</p>}
                    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-400">
                      <span>支持 {rule.support_count} 次</span><span>冲突 {rule.contradiction_count} 次</span><span>范围：{rule.scope}</span>
                      {rule.target_agents.length > 0 && <span>适用：{rule.target_agents.join('、')}</span>}
                      <span>强化于 {formatDate(rule.last_reinforced_at)}</span>
                    </div>
                  </article>)}
                  {activeRules.length === 0 && <EmptyState text="暂无生效的项目规则。强烈且稳定的反馈会先成为记忆；被多次相似证据支持后才会提升为规则。" />}
                </div>
              )}

              {!loading && !error && tab === 'traces' && (
                <div className="space-y-3" role="tabpanel">
                  {traces.map(trace => {
                    const detail = traceDetails[trace.trace_id];
                    return <article key={trace.trace_id} className="rounded-xl border border-gray-100 bg-white p-4 shadow-sm">
                      <button type="button" onClick={() => void toggleTrace(trace)} className="w-full text-left">
                        <div className="flex flex-wrap items-center gap-2 text-[11px] text-gray-400">
                          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-600">{trace.source === 'historical' ? '历史导入' : '实时采集'}</span>
                          <span>{operationLabel(trace.operation_kind)}</span>
                          <span>{trace.event_count} 个事件</span><span>{formatDate(trace.started_at)}</span>
                          <span className={trace.analysis_status === 'skipped' ? 'text-gray-400' : trace.is_classified ? 'text-emerald-600' : 'text-amber-600'}>{analysisLabel(trace.analysis_status, trace.is_classified)}</span>
                          <span className="ml-auto">{expandedTraceId === trace.trace_id ? '收起' : '查看证据'}</span>
                        </div>
                        <p className="mt-2 line-clamp-2 text-sm leading-6 text-gray-700">{trace.user_message || '（无用户文本）'}</p>
                        <div className="mt-2 flex flex-wrap gap-1 text-[11px]">
                          {trace.has_error && <span className="rounded bg-rose-50 px-1.5 py-0.5 text-rose-600">错误</span>}
                          {trace.has_correction && <span className="rounded bg-amber-50 px-1.5 py-0.5 text-amber-700">修正</span>}
                          {trace.has_confirmation && <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-emerald-700">肯定</span>}
                          {trace.has_feedback && <span className="rounded bg-violet-50 px-1.5 py-0.5 text-violet-700">反馈</span>}
                        </div>
                      </button>
                      {expandedTraceId === trace.trace_id && <div className="mt-3 border-t border-gray-100 pt-3">
                        {traceLoadingId === trace.trace_id && <div className="text-xs text-gray-400">正在读取证据事件…</div>}
                        {detail?.classification?.items.map(item => <div key={`${item.event_id}-${item.type}`} className="mb-2 rounded-lg border border-violet-100 bg-violet-50/50 px-3 py-2 text-xs leading-5 text-violet-800"><span className="mr-2 font-medium">{classificationLabel(item.type)}</span>{item.summary}</div>)}
                        {detail?.events?.map(event => <div key={event.event_id} className="mb-2 rounded-lg bg-gray-50 px-3 py-2 text-xs leading-5 text-gray-600"><span className="mr-2 text-gray-400">{event.sequence_no}. {event.actor} / {event.event_type}</span>{eventText(event)}</div>)}
                        {detail && !detail.events?.length && <div className="text-xs text-gray-400">该 Trace 尚未包含事件。</div>}
                      </div>}
                    </article>;
                  })}
                  {traces.length === 0 && <EmptyState text="暂无 Trace 证据。每五轮会话或主 Agent 主动触发后会形成一条可回溯的证据记录。" />}
                </div>
              )}

              {!loading && !error && tab === 'memories' && (
                <div className="space-y-3" role="tabpanel">
                  {memories.map(memory => (
                    <article key={memory.memory_id} className="rounded-xl border border-gray-100 bg-white p-4 shadow-sm">
                      <div className="flex flex-wrap items-center gap-2 mb-2 text-[11px]">
                        <span className="rounded-full bg-violet-100 text-violet-700 px-2 py-0.5">{kindLabel[memory.kind] ?? memory.kind}</span>
                        {memory.subtype && <span className="text-gray-500">{memory.subtype}</span>}
                        <span className="ml-auto text-gray-400">置信度 {Math.round(memory.confidence * 100)}%</span>
                        <button type="button" onClick={() => setDowngradeTarget(memory)} className="rounded-md border border-amber-200 bg-white px-2 py-0.5 text-amber-700 hover:bg-amber-50">降回 Trace</button>
                      </div>
                      <button type="button" onClick={() => void toggleMemory(memory)} className="w-full text-left text-sm leading-6 text-gray-700 hover:text-purple-700">
                        {memory.claim}
                      </button>
                      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-400">
                        <span>范围：{memory.scope}</span>
                        <span>重要性 {Math.round(memory.importance)}</span>
                        <span>支持 {memory.support_count} 次</span>
                        {memory.contradiction_count > 0 && <span>冲突 {memory.contradiction_count} 次</span>}
                        <span title={memory.trace_id}>Trace：{memory.trace_id.slice(0, 14)}…</span>
                        <span>事件 {memory.source_event_ids.length} 个</span>
                        <span>{formatDate(memory.created_at)}</span>
                      </div>
                      {expandedMemoryId === memory.memory_id && (
                        <div className="mt-3 border-t border-gray-100 pt-3">
                          {contentLoadingId === memory.memory_id
                            ? <div className="text-xs text-gray-400">正在读取记忆正文…</div>
                            : <pre className="whitespace-pre-wrap break-words rounded-lg bg-gray-50 p-3 text-xs leading-5 text-gray-600 font-sans">{memoryContents[memory.memory_id] ?? memory.claim}</pre>}
                          {memory.file_path && <div className="mt-2 text-[11px] text-gray-400">{memory.file_path}</div>}
                        </div>
                      )}
                    </article>
                  ))}
                  {memories.length === 0 && <EmptyState text="还没有提取到可长期复用的原子记忆。普通聊天与一次性指令不会被保存。" />}
                </div>
              )}
            </div>
            {downgradeTarget && <div className="absolute inset-0 z-20 grid place-items-center bg-black/30 p-6" role="alertdialog" aria-modal="true" aria-labelledby="downgrade-memory-title">
              <div className="w-full max-w-md rounded-2xl bg-white p-5 shadow-2xl">
                <h3 id="downgrade-memory-title" className="font-semibold text-gray-800">确认降低记忆层级？</h3>
                <p className="mt-3 text-sm leading-6 text-gray-600">{downgradeTarget.claim}</p>
                <p className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">{downgradeDescription}</p>
                <div className="mt-5 flex justify-end gap-2">
                  <button type="button" onClick={() => setDowngradeTarget(null)} disabled={downgrading} className="rounded-lg px-3 py-2 text-sm text-gray-600 hover:bg-gray-100 disabled:opacity-50">取消</button>
                  <button type="button" onClick={() => void confirmDowngrade()} disabled={downgrading} className="rounded-lg bg-amber-600 px-3 py-2 text-sm text-white hover:bg-amber-700 disabled:opacity-50">{downgrading ? '处理中…' : '确认降权'}</button>
                </div>
              </div>
            </div>}
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}

function EmptyState({ text }: { text: string }) {
  return <div className="rounded-xl border border-dashed border-gray-200 bg-gray-50 px-5 py-12 text-center text-sm leading-6 text-gray-400">{text}</div>;
}

function eventText(event: TraceEvent) {
  const payload = event.payload;
  const content = payload.content ?? payload.message ?? payload.data ?? payload.answers;
  const text = typeof content === 'string' ? content : JSON.stringify(content ?? payload);
  return text.slice(0, 1000);
}

function classificationLabel(type: string) {
  return ({ error: '错误', correction: '修正', confirmation: '肯定', feedback: '反馈' } as Record<string, string>)[type] ?? type;
}

function operationLabel(kind: string) {
  return ({ conversation: '对话', routine: '常规执行', tool_only: '纯工具', system: '系统' } as Record<string, string>)[kind] ?? kind;
}

function analysisLabel(status: string, isClassified?: boolean) {
  if (status === 'skipped') return '不参与记忆分析';
  return isClassified || status === 'complete' ? '已分类' : '待分析';
}
