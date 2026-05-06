import { useState } from 'react';
import { EditToggle, EditPanel } from './EditCard';

export function ToolResultCard({ toolName, content }: { toolName: string; content: string }) {
  const [expanded, setExpanded] = useState(false);
  const cleanName = toolName.replace(/^🤖\s?/, '');

  if (cleanName === 'Edit') {
    const parts = (content || '').split('\n===EDIT_DIFF===\n');
    const diff = parts[1] || '';
    const oldMatch = diff.match(/^旧:\s?(.*?)(?=\n新:)/s);
    const newMatch = diff.match(/新:\s?(.*)$/s);
    const oldText = oldMatch ? oldMatch[1].trim() : '';
    const newText = newMatch ? newMatch[1].trim() : '';
    const maxLines = Math.max(oldText.split('\n').length, newText.split('\n').length);
    const [editFolded, setEditFolded] = useState(maxLines > 12);

    return (
      <div className="my-2 w-full">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs text-blue-500 font-medium bg-blue-50 px-2 py-0.5 rounded-full">✎ Edit</span>
          {maxLines > 12 && <EditToggle maxLines={maxLines} folded={editFolded} onToggle={() => setEditFolded(!editFolded)} />}
        </div>
        <div className="grid grid-cols-2 gap-2">
          {oldText && <EditPanel label="旧" text={oldText} color="red" folded={editFolded} />}
          {newText && <EditPanel label="新" text={newText} color="green" folded={editFolded} />}
        </div>
      </div>
    );
  }

  const isWrite = cleanName === 'Write';
  const lines = (content || '').split('\n');
  const isLong = lines.length > 10;
  const displayContent = (expanded || !isLong) ? content : lines.slice(0, 10).join('\n') + '\n…';

  return (
    <div className="my-1">
      <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${isWrite ? 'text-blue-500 bg-blue-50' : 'text-green-600 bg-green-50'}`}>
        {isWrite ? '✏️ Write' : `📋 ${cleanName}`}
      </span>
      <div className={`font-mono text-sm leading-relaxed whitespace-pre-wrap rounded-lg border p-2.5 mt-1 ${isWrite ? 'bg-blue-50/50 border-blue-100 text-blue-800' : 'bg-green-50/50 border-green-100 text-green-800'} ${isLong && !expanded ? 'max-h-48 overflow-hidden' : ''}`}>
        {displayContent}
      </div>
      {isLong && <button onClick={() => setExpanded(!expanded)} className="text-xs text-gray-400 hover:text-gray-600 mt-1 ml-1">{expanded ? '收起 ▲' : `展开全部 (${lines.length} 行) ▼`}</button>}
    </div>
  );
}
