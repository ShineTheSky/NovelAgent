import { useState, useRef, useCallback, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useFileExplorer } from '../hooks/useFileExplorer';
import { FileTreeNode } from './FileTreeNode';
import type { FileNode } from '../api/client';

function countFiles(node: FileNode | null): number {
  if (!node) return 0;
  if (node.type === 'file') return 1;
  if (!node.children) return 0;
  return node.children.reduce((sum, c) => sum + countFiles(c), 0);
}

function FolderSvg() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" className="shrink-0">
      <path d="M2 4a1 1 0 011-1h3.5l1.5 2H14a1 1 0 011 1v7a1 1 0 01-1 1H3a1 1 0 01-1-1V4z" fill="#facc15" stroke="#eab308" strokeWidth="0.5"/>
    </svg>
  );
}

function FileSvg() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" className="shrink-0">
      <path d="M3 2h6l4 4v9a1 1 0 01-1 1H3a1 1 0 01-1-1V3a1 1 0 011-1z" fill="#e5e7eb" stroke="#d1d5db" strokeWidth="0.5"/>
      <path d="M9 2v4h4" fill="none" stroke="#9ca3af" strokeWidth="0.5"/>
    </svg>
  );
}

interface FileExplorerProps {
  projectId: string;
}

export function FileExplorer({ projectId }: FileExplorerProps) {
  const {
    tree, loading, error, loadTree,
    expandedPaths, toggleExpanded,
    selectedPath, fileContent, fileContentLoading, selectFile, closePreview,
    isCollapsed, toggleCollapsed,
  } = useFileExplorer(projectId);

  const [contentWidth, setContentWidth] = useState(440);
  const resizingRef = useRef(false);
  const startXRef = useRef(0);
  const startWidthRef = useRef(440);

  const onResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    resizingRef.current = true;
    startXRef.current = e.clientX;
    startWidthRef.current = contentWidth;
  }, [contentWidth]);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!resizingRef.current) return;
      const delta = startXRef.current - e.clientX;
      setContentWidth(Math.max(280, Math.min(800, startWidthRef.current + delta)));
    };
    const onUp = () => { resizingRef.current = false; };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    return () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
  }, []);

  if (isCollapsed) {
    return (
      <div
        className="w-[4px] shrink-0 bg-gray-100 hover:bg-blue-300 cursor-pointer transition-colors group relative max-md:hidden"
        onClick={toggleCollapsed}
        title="展开文件面板"
      >
        <div className="absolute right-full mr-1 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 bg-gray-700 text-white text-[11px] px-2 py-1 rounded whitespace-nowrap pointer-events-none transition-opacity z-50">
          展开文件面板
        </div>
      </div>
    );
  }

  return (
    <>
      {/* File content panel — appears when a file is selected */}
      {selectedPath && (
        <div className="shrink-0 bg-white flex flex-col h-full overflow-hidden max-md:hidden relative" style={{ width: contentWidth }}>
          {/* Resize handle — left edge */}
          <div
            className="absolute left-0 top-0 bottom-0 w-[5px] cursor-col-resize hover:bg-blue-400/30 transition-colors z-10"
            style={{ marginLeft: -2 }}
            onMouseDown={onResizeStart}
          />
          {/* Header */}
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-gray-100 shrink-0">
            <div className="flex items-center gap-2 text-[13px] font-medium text-gray-600 min-w-0">
              <FileSvg />
              <span className="truncate">{selectedPath}</span>
            </div>
            <button
              onClick={closePreview}
              className="w-7 h-7 flex items-center justify-center rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 text-sm transition-colors shrink-0"
              title="关闭"
            >✕</button>
          </div>

          {/* Content area */}
          <div className="flex-1 overflow-y-auto px-4 py-3">
            {fileContentLoading && (
              <div className="flex items-center justify-center gap-1.5 py-16">
                {[0, 1, 2].map(i => (
                  <div key={i} className="w-2 h-2 bg-gray-300 rounded-full animate-bounce" style={{ animationDelay: `${i * 0.15}s` }} />
                ))}
              </div>
            )}
            {!fileContentLoading && fileContent !== null && (
              <div className="prose-sm max-w-none text-gray-700 leading-relaxed
                [&_h1]:text-xl [&_h1]:font-bold [&_h1]:mt-4 [&_h1]:mb-2
                [&_h2]:text-lg [&_h2]:font-semibold [&_h2]:mt-3 [&_h2]:mb-1.5
                [&_h3]:text-base [&_h3]:font-medium [&_h3]:mt-2 [&_h3]:mb-1
                [&_p]:mb-2 [&_p]:leading-relaxed
                [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:mb-2
                [&_ol]:list-decimal [&_ol]:pl-5 [&_ol]:mb-2
                [&_code]:bg-gray-100 [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded [&_code]:text-sm
                [&_pre]:bg-gray-50 [&_pre]:p-3 [&_pre]:rounded-lg [&_pre]:mb-3 [&_pre]:overflow-x-auto [&_pre]:text-sm
                [&_blockquote]:border-l-3 [&_blockquote]:border-gray-300 [&_blockquote]:pl-3 [&_blockquote]:text-gray-500 [&_blockquote]:italic
                [&_hr]:my-3 [&_hr]:border-gray-200
                [&_table]:w-full [&_table]:text-sm [&_table]:mb-3
                [&_th]:border [&_th]:border-gray-200 [&_th]:px-2 [&_th]:py-1 [&_th]:bg-gray-50
                [&_td]:border [&_td]:border-gray-200 [&_td]:px-2 [&_td]:py-1"
              >
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{fileContent}</ReactMarkdown>
              </div>
            )}
          </div>
        </div>
      )}

      {/* File tree panel */}
      <div className="w-[260px] shrink-0 border-l border-gray-200 bg-[#fafbfc] flex flex-col h-full overflow-hidden max-md:hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-3 py-2 border-b border-gray-100 shrink-0">
          <div className="flex items-center gap-1.5 text-[13px] font-medium text-gray-600">
            <FolderSvg />
            <span>项目文件</span>
          </div>
          <button
            onClick={toggleCollapsed}
            className="w-7 h-7 flex items-center justify-center rounded hover:bg-gray-200 text-gray-400 hover:text-gray-600 text-sm transition-colors"
            title="折叠面板"
          >−</button>
        </div>

        {/* Tree area */}
        <div className="flex-1 overflow-y-auto py-1">
          {loading && (
            <div className="px-3 py-2 space-y-2">
              {[55, 70, 64, 78, 60].map((width, index) => (
                <div key={index} className="h-5 bg-gray-100 rounded animate-pulse" style={{ width: `${width}%` }} />
              ))}
            </div>
          )}
          {!loading && error && (
            <div className="flex flex-col items-center justify-center h-full text-gray-400 py-8 px-3">
              <div className="text-xs text-center mb-1">加载失败</div>
              <div className="text-[10px] text-gray-300 text-center mb-2">{error}</div>
              <button onClick={() => loadTree(projectId)} className="text-[11px] text-blue-500 hover:text-blue-600">重试</button>
            </div>
          )}
          {!loading && !error && tree && tree.children && tree.children.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-gray-300 select-none py-8">
              <div className="text-3xl mb-2">📂</div>
              <div className="text-xs text-gray-400">目录为空</div>
            </div>
          )}
          {!loading && !error && tree?.children && (
            <div role="tree" aria-label="项目文件">
              {tree.children.map(child => (
                <FileTreeNode
                  key={child.path}
                  node={child}
                  depth={0}
                  selectedPath={selectedPath}
                  expandedPaths={expandedPaths}
                  onToggleExpand={toggleExpanded}
                  onSelectFile={selectFile}
                  fileContentLoading={fileContentLoading}
                />
              ))}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-3 py-1.5 border-t border-gray-100 text-[11px] text-gray-400 shrink-0">
          {countFiles(tree)} 个文件
        </div>
      </div>

    </>
  );
}
