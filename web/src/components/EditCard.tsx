export function EditToggle({ maxLines, folded, onToggle }: { maxLines: number; folded: boolean; onToggle: () => void }) {
  return <button onClick={onToggle} className="text-[10px] text-gray-400 hover:text-gray-600 transition-colors">{folded ? `展开 (${maxLines} 行) ▼` : '收起 ▲'}</button>;
}

export function EditPanel({ label, text, color, folded }: { label: string; text: string; color: string; folded: boolean }) {
  const c = color === 'red'
    ? { label: 'text-red-400', bg: 'bg-red-50', border: 'border-red-100', text: 'text-red-700' }
    : { label: 'text-green-500', bg: 'bg-green-50', border: 'border-green-100', text: 'text-green-700' };
  return (
    <div>
      <div className={`text-xs font-medium uppercase tracking-wide ${c.label} mb-1`}>— {label} —</div>
      <div className={`${c.bg} border ${c.border} rounded-lg p-3 font-mono text-sm ${c.text} whitespace-pre-wrap leading-relaxed ${folded ? 'max-h-40 overflow-hidden' : ''}`}>{text}</div>
    </div>
  );
}
