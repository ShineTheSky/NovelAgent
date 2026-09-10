import type { PendingAsk } from '../types/chat';

export function PermissionDialog({ ask }: { ask: PendingAsk }) {
  return (
    <div className="fixed inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center z-50" role="dialog" aria-modal="true" aria-labelledby="permission-dialog-title">
      <div className="bg-white rounded-2xl shadow-2xl p-6 max-w-sm w-full mx-4">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-full bg-amber-100 flex items-center justify-center text-lg" aria-hidden="true">⚠</div>
          <div>
            <h3 id="permission-dialog-title" className="font-semibold text-gray-800">权限确认</h3>
            <p className="text-xs text-gray-400">Agent 请求执行操作</p>
          </div>
        </div>
        <div className="bg-gray-50 rounded-xl p-3 mb-4">
          <div className="text-sm font-mono font-medium text-gray-700">{ask.tool}</div>
          {ask.params_summary && <div className="text-xs text-gray-400 mt-1 font-mono truncate">{ask.params_summary.slice(0, 100)}</div>}
        </div>
        <div className="flex gap-2">
          <button type="button" onClick={ask.onDeny} className="flex-1 py-2.5 text-sm border border-gray-200 rounded-xl hover:bg-gray-50 text-gray-500">拒绝</button>
          <button type="button" onClick={ask.onAllow} className="flex-1 py-2.5 text-sm bg-purple-600 text-white rounded-xl hover:bg-purple-700 font-medium">允许</button>
        </div>
      </div>
    </div>
  );
}
