import { useEffect, useState } from 'react';
import { fetchHistoryUnit } from '../api/client';
import type { HistoryUnit } from '../types/history';
import { HistoryDetail } from './HistoryPanel';

export function HistoryDetailDialog({ projectId, historyId, onClose, onSelectHistory }: {
  projectId: string;
  historyId: string;
  onClose: () => void;
  onSelectHistory: (historyId: string) => void;
}) {
  const [unit, setUnit] = useState<HistoryUnit | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    fetchHistoryUnit(projectId, historyId).then(detail => {
      if (active) setUnit(detail);
    }).catch(reason => {
      if (active) setError(reason instanceof Error ? reason.message : '加载 History 失败');
    });
    return () => { active = false; };
  }, [projectId, historyId]);

  return <div className="absolute inset-0 z-40 flex flex-col bg-white" role="dialog" aria-modal="true" aria-labelledby="memory-history-title">
    <header className="flex shrink-0 items-start gap-4 border-b px-6 py-5">
      <div className="min-w-0 flex-1">
        <h3 id="memory-history-title" className="text-sm font-semibold text-gray-800">关联 History</h3>
        <p className="mt-1 break-all font-mono text-xs text-purple-700">{historyId}</p>
      </div>
      <button type="button" onClick={onClose} aria-label="关闭关联 History" className="text-gray-400 hover:text-gray-700">✕</button>
    </header>
    <main className="min-h-0 flex-1 overflow-y-auto p-5">
      {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-xs text-red-600">{error}</p>}
      {!unit && !error && <p className="py-12 text-center text-sm text-gray-400">正在读取 History…</p>}
      {unit && <HistoryDetail unit={unit} onSelectHistory={onSelectHistory} />}
    </main>
  </div>;
}
