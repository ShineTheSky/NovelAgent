import type { FileNode } from '../api/client';

interface FileTreeNodeProps {
  node: FileNode;
  depth: number;
  selectedPath: string | null;
  expandedPaths: Set<string>;
  onToggleExpand: (path: string) => void;
  onSelectFile: (path: string) => void;
  fileContentLoading: boolean;
}

function ChevronRight() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" className="shrink-0 text-gray-400">
      <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}

function ChevronDown() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" className="shrink-0 text-gray-400">
      <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}

function FolderIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" className="shrink-0">
      <path d="M2 4a1 1 0 011-1h3.5l1.5 2H14a1 1 0 011 1v7a1 1 0 01-1 1H3a1 1 0 01-1-1V4z" fill="#facc15" stroke="#eab308" strokeWidth="0.5"/>
    </svg>
  );
}

function FileIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" className="shrink-0">
      <path d="M3 2h6l4 4v9a1 1 0 01-1 1H3a1 1 0 01-1-1V3a1 1 0 011-1z" fill="#e5e7eb" stroke="#d1d5db" strokeWidth="0.5"/>
      <path d="M9 2v4h4" fill="none" stroke="#9ca3af" strokeWidth="0.5"/>
    </svg>
  );
}

function LoadingSpinner() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" className="shrink-0 animate-spin text-gray-300">
      <circle cx="7" cy="7" r="5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeDasharray="20 10"/>
    </svg>
  );
}

export function FileTreeNode({
  node, depth, selectedPath, expandedPaths,
  onToggleExpand, onSelectFile, fileContentLoading,
}: FileTreeNodeProps) {
  const isDir = node.type === 'directory';
  const isExpanded = expandedPaths.has(node.path);
  const isSelected = selectedPath === node.path;
  const isLoading = isSelected && fileContentLoading;

  return (
    <div>
      <div
        role="treeitem"
        aria-level={depth + 1}
        aria-expanded={isDir ? isExpanded : undefined}
        aria-selected={isSelected}
        tabIndex={0}
        className={`flex items-center gap-1 h-7 cursor-pointer select-none text-[13px] transition-colors
          ${isSelected ? 'bg-blue-50 text-blue-700' : 'text-gray-700 hover:bg-gray-100'}`}
        style={{ paddingLeft: depth * 12 + 8 }}
        onClick={() => isDir ? onToggleExpand(node.path) : onSelectFile(node.path)}
        onKeyDown={event => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          if (isDir) onToggleExpand(node.path);
          else onSelectFile(node.path);
        }}
      >
        {isDir ? (isExpanded ? <ChevronDown /> : <ChevronRight />) : <span className="w-4 shrink-0" />}
        {isDir ? <FolderIcon /> : isLoading ? <LoadingSpinner /> : <FileIcon />}
        <span className={`truncate ${isDir ? 'font-medium text-gray-600' : ''}`}>{node.name}</span>
      </div>
      {isDir && isExpanded && node.children?.map(child => (
        <FileTreeNode
          key={child.path}
          node={child}
          depth={depth + 1}
          selectedPath={selectedPath}
          expandedPaths={expandedPaths}
          onToggleExpand={onToggleExpand}
          onSelectFile={onSelectFile}
          fileContentLoading={fileContentLoading}
        />
      ))}
    </div>
  );
}
