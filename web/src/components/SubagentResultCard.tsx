import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { presetColors, defaultPresetColor } from './presetColors';

export function SubagentResultCard({ preset, content }: { preset: string; content: string }) {
  const c = presetColors[preset] || defaultPresetColor;

  return (
    <div className={`rounded-xl overflow-hidden border ${c.border} shadow-sm my-3`}>
      <div className={`${c.bar} text-white text-xs font-semibold px-4 py-2 tracking-wide`}>
        子Agent ({preset}) 返回
      </div>
      <div className={`${c.bg} px-4 py-3 text-sm prose prose-sm max-w-none`}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
      </div>
    </div>
  );
}
