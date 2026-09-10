export function TokenBar({ tokenCount, limit, onCompress, disabled }: { tokenCount: number; limit: number; onCompress: () => void; disabled?: boolean }) {
  const pct = Math.min(100, Math.round((tokenCount / limit) * 100));
  const threshold = 80;
  const radius = 12;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (pct / 100) * circumference;
  const isWarning = pct >= threshold * 0.8;
  const isDanger = pct >= threshold;
  const color = isDanger ? '#ef4444' : isWarning ? '#f59e0b' : '#a855f7';

  return (
    <div className="relative group flex items-center" title={`${tokenCount.toLocaleString()} / ${limit.toLocaleString()} tokens (${pct}%) — 超过${threshold}%自动压缩`}>
      <button type="button" onClick={onCompress} disabled={disabled} className={`flex items-center ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}>
        <svg width="28" height="28" viewBox="0 0 28 28" className="transform -rotate-90">
          <circle cx="14" cy="14" r={radius} fill="none" stroke="#e5e7eb" strokeWidth="2.5" />
          <circle cx="14" cy="14" r={radius} fill="none" stroke={color} strokeWidth="2.5" strokeDasharray={circumference} strokeDashoffset={offset} strokeLinecap="round" className="transition-all duration-500" />
        </svg>
        <span className={`text-[10px] ml-1 font-mono ${isDanger ? 'text-red-500' : isWarning ? 'text-amber-500' : 'text-gray-400'}`}>{pct}%</span>
      </button>
      <div className="absolute top-full mt-1 right-0 bg-gray-800 text-white text-[11px] rounded-lg px-2.5 py-1.5 whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none z-50">
        {tokenCount.toLocaleString()} / {limit.toLocaleString()} tokens<br/>压缩阈值: {threshold}% · 点击强制压缩
      </div>
    </div>
  );
}
