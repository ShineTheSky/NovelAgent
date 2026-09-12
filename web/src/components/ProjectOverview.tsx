import { useCallback, useEffect, useState } from 'react';
import { fetchEvidence, fetchMemories, fetchMemoryPatterns } from '../api/client';
import type { Evidence, MemoryPattern, TraceMemory } from '../types/insights';
import type { ProjectInfo, SessionInfo } from '../types/chat';

interface ProjectOverviewProps {
  project: ProjectInfo;
  sessions: SessionInfo[];
  onNewSession: () => void;
  onSelectSession: (sessionId: string) => void;
}

function kindLabel(kind: string) {
  return kind === 'preference' ? '用户偏好' : kind === 'issue' ? '问题记录' : '项目记忆';
}

export function ProjectOverview({ project, sessions, onNewSession, onSelectSession }: ProjectOverviewProps) {
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [memories, setMemories] = useState<TraceMemory[]>([]);
  const [patterns, setPatterns] = useState<MemoryPattern[]>([]);

  const loadInsights = useCallback(async () => {
    try {
      const [nextEvidence, nextMemories, nextPatterns] = await Promise.all([
        fetchEvidence(project.project_id), fetchMemories(project.project_id), fetchMemoryPatterns(project.project_id),
      ]);
      setEvidence(nextEvidence);
      setMemories(nextMemories);
      setPatterns(nextPatterns);
    } catch {
      setEvidence([]);
      setMemories([]);
      setPatterns([]);
    }
  }, [project.project_id]);

  useEffect(() => { void loadInsights(); }, [loadInsights]);

  const confirmedPatterns = patterns.filter(pattern => pattern.status === 'confirmed').slice(0, 3);
  const recentMemories = memories.slice(0, 3);

  return (
    <section className="flex-1 overflow-y-auto bg-white px-8 py-10">
      <div className="mx-auto max-w-3xl">
        <div className="mb-10 flex items-start justify-between gap-6">
          <div>
            <div className="mb-2 flex items-center gap-2 text-xs text-gray-400"><span>项目</span><span>/</span><span>总览</span></div>
            <h1 className="text-2xl font-semibold tracking-tight text-gray-800">{project.name}</h1>
            <p className="mt-2 text-sm text-gray-500">{project.genre || '尚未设置题材'} · {(project.word_count ?? 0).toLocaleString()} 字</p>
          </div>
          <button type="button" onClick={onNewSession} className="rounded-lg bg-purple-600 px-3 py-2 text-sm font-medium text-white shadow-sm hover:bg-purple-700">新建会话</button>
        </div>

        <div className="mb-8 grid gap-3 sm:grid-cols-4">
          <StatCard label="会话" value={sessions.length} detail="该项目下的任务" />
          <StatCard label="待观察证据" value={evidence.length} detail="尚不足以形成结论" />
          <StatCard label="原子记忆" value={memories.length} detail="已提升，可按需预取" />
          <StatCard label="已确认模式" value={confirmedPatterns.length} detail="会注入后续上下文" />
        </div>

        <div className="grid gap-5 lg:grid-cols-2">
          <section className="rounded-xl border border-gray-100 bg-[#fafbfc] p-5">
            <div className="mb-4 flex items-center justify-between"><h2 className="text-sm font-semibold text-gray-700">已确认模式</h2><span className="text-xs text-gray-400">多条 Memory 归纳</span></div>
            <div className="space-y-3">
              {confirmedPatterns.map(pattern => <div key={pattern.pattern_id} className="rounded-lg border border-gray-100 bg-white px-3 py-2.5"><div className="mb-1 text-[11px] text-emerald-600">已确认 · 支持 {pattern.support_count} 条</div><p className="text-sm leading-5 text-gray-700">{pattern.claim}</p></div>)}
              {confirmedPatterns.length === 0 && <p className="py-4 text-sm leading-6 text-gray-400">模式需经多条 Memory 支持并审核确认后才会注入上下文。</p>}
            </div>
          </section>
          <section className="rounded-xl border border-gray-100 bg-[#fafbfc] p-5">
            <div className="mb-4 flex items-center justify-between"><h2 className="text-sm font-semibold text-gray-700">最近记忆</h2><span className="text-xs text-gray-400">可回溯到 Trace</span></div>
            <div className="space-y-3">
              {recentMemories.map(memory => <div key={memory.memory_id} className="rounded-lg border border-gray-100 bg-white px-3 py-2.5"><div className="mb-1 text-[11px] text-violet-600">{kindLabel(memory.kind)} · {memory.subtype || '通用'}</div><p className="text-sm leading-5 text-gray-700">{memory.claim}</p></div>)}
              {memories.length === 0 && <p className="py-4 text-sm leading-6 text-gray-400">暂无已提升 Memory；一次性或证据不足的结论会保留为待观察证据。</p>}
            </div>
          </section>
        </div>

        <section className="mt-5 rounded-xl border border-gray-100 p-5">
          <div className="mb-3 flex items-center justify-between"><h2 className="text-sm font-semibold text-gray-700">最近会话</h2><button type="button" onClick={onNewSession} className="text-xs text-purple-600 hover:text-purple-700">新建</button></div>
          <div className="divide-y divide-gray-100">
            {sessions.slice(0, 6).map(session => <button type="button" key={session.session_id} onClick={() => onSelectSession(session.session_id)} className="flex w-full items-center justify-between gap-3 py-2.5 text-left hover:text-purple-700"><span className="truncate text-sm text-gray-600">{session.title || '新会话'}</span><span className="shrink-0 text-xs text-gray-400">{session.updated_at?.slice(5, 16)}</span></button>)}
            {sessions.length === 0 && <p className="py-5 text-sm text-gray-400">从“新建会话”开始在此项目中工作。</p>}
          </div>
        </section>
      </div>
    </section>
  );
}

function StatCard({ label, value, detail }: { label: string; value: number; detail: string }) {
  return <div className="rounded-xl border border-gray-100 bg-white p-4 shadow-sm"><div className="text-xs text-gray-400">{label}</div><div className="mt-1 text-2xl font-semibold text-gray-800">{value}</div><div className="mt-1 text-[11px] text-gray-400">{detail}</div></div>;
}
