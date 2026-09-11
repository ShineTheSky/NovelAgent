import { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { fetchNovelNode, fetchNovelTree } from '../api/client';
import type { NovelDocument, NovelTree } from '../types/novel';

interface NovelWorkspaceProps {
  projectId: string;
}

function formatCount(count: number) {
  return `${count.toLocaleString()} 字`;
}

function DocumentButton({ document, selectedId, onSelect, indent = false }: {
  document: NovelDocument;
  selectedId: string | null;
  onSelect: (document: NovelDocument) => void;
  indent?: boolean;
}) {
  const prefix = document.type === 'section' ? `${document.volume}.${document.chapter}.${document.section}` : document.type === 'volume_outline' ? '卷纲' : '章纲';
  return (
    <button
      type="button"
      onClick={() => onSelect(document)}
      className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors ${indent ? 'ml-3 w-[calc(100%-0.75rem)]' : ''} ${selectedId === document.id ? 'bg-purple-50 text-purple-700' : 'text-gray-600 hover:bg-gray-100'}`}
    >
      <span className="shrink-0 font-mono text-[10px] text-gray-400">{prefix}</span>
      <span className="min-w-0 flex-1 truncate">{document.title}</span>
      <span className="shrink-0 text-[10px] text-gray-400">{formatCount(document.char_count)}</span>
    </button>
  );
}

export function NovelWorkspace({ projectId }: NovelWorkspaceProps) {
  const [tree, setTree] = useState<NovelTree | null>(null);
  const [selected, setSelected] = useState<NovelDocument | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [loadingTree, setLoadingTree] = useState(true);
  const [loadingContent, setLoadingContent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showNavigator, setShowNavigator] = useState(true);
  const [navigatorWidth, setNavigatorWidth] = useState(288);
  const contentAbortRef = useRef<AbortController | null>(null);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const resizingNavigatorRef = useRef(false);
  const navigatorStartXRef = useRef(0);
  const navigatorStartWidthRef = useRef(288);

  useEffect(() => {
    const onMouseMove = (event: MouseEvent) => {
      if (!resizingNavigatorRef.current || !workspaceRef.current) return;
      const maxWidth = Math.max(180, workspaceRef.current.clientWidth - 280);
      setNavigatorWidth(Math.max(180, Math.min(maxWidth, navigatorStartWidthRef.current + event.clientX - navigatorStartXRef.current)));
    };
    const onMouseUp = () => { resizingNavigatorRef.current = false; };
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    return () => {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
    };
  }, []);

  const loadTree = useCallback(async () => {
    setLoadingTree(true);
    setError(null);
    try {
      const nextTree = await fetchNovelTree(projectId);
      setTree(nextTree);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '小说目录加载失败');
    } finally {
      setLoadingTree(false);
    }
  }, [projectId]);

  useEffect(() => {
    queueMicrotask(() => { void loadTree(); });
    return () => contentAbortRef.current?.abort();
  }, [loadTree]);

  const selectDocument = async (document: NovelDocument) => {
    contentAbortRef.current?.abort();
    const controller = new AbortController();
    contentAbortRef.current = controller;
    setSelected(document);
    setContent(null);
    setLoadingContent(true);
    setError(null);
    try {
      const result = await fetchNovelNode(projectId, document.id, controller.signal);
      if (!controller.signal.aborted) setContent(result.content);
    } catch (requestError) {
      if (!controller.signal.aborted) setError(requestError instanceof Error ? requestError.message : '正文加载失败');
    } finally {
      if (!controller.signal.aborted) setLoadingContent(false);
    }
  };

  const startNavigatorResize = (event: React.MouseEvent) => {
    event.preventDefault();
    resizingNavigatorRef.current = true;
    navigatorStartXRef.current = event.clientX;
    navigatorStartWidthRef.current = navigatorWidth;
  };

  return (
    <div ref={workspaceRef} className="flex min-h-0 flex-1 bg-white">
      {showNavigator ? <div style={{ width: navigatorWidth }} className="relative shrink-0 max-md:hidden">
        <aside className="h-full overflow-y-auto border-r border-gray-200 bg-[#fafbfc] p-3">
        <div className="mb-3 flex cursor-default items-center justify-between select-none">
          <div>
            <h2 className="text-sm font-semibold text-gray-700">小说结构</h2>
            <p className="mt-0.5 text-[11px] text-gray-400">按数字编号自动排序</p>
          </div>
          <div className="flex items-center gap-1">
            <button type="button" onClick={() => void loadTree()} className="rounded px-2 py-1 text-xs text-purple-600 hover:bg-purple-50">刷新</button>
            <button type="button" onClick={() => setShowNavigator(false)} className="rounded px-2 py-1 text-xs text-gray-500 hover:bg-gray-200" title="隐藏小说目录">隐藏</button>
          </div>
        </div>
        {loadingTree && <div className="py-8 text-center text-xs text-gray-400">正在读取小说文件…</div>}
        {!loadingTree && error && <div role="alert" className="rounded-md bg-red-50 px-2 py-2 text-xs text-red-600">{error}</div>}
        {!loadingTree && !error && tree?.volumes.length === 0 && <div className="py-8 text-center text-xs text-gray-400">尚未生成卷纲、章纲或正文。</div>}
        {!loadingTree && tree?.volumes.map(volume => (
          <section key={volume.volume} className="mb-3">
            {volume.outline ? (
              <button
                type="button"
                onClick={() => void selectDocument(volume.outline!)}
                className={`mb-1 flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-xs font-medium transition-colors ${selected?.id === volume.outline.id ? 'bg-purple-50 text-purple-700' : 'text-gray-700 hover:bg-gray-100'}`}
              >
                <span className="min-w-0 truncate">第 {volume.volume} 卷 · {volume.title}</span>
                <span className="ml-2 shrink-0 text-[10px] font-normal text-gray-400">{formatCount(volume.char_count)}</span>
              </button>
            ) : (
              <div className="mb-1 flex items-center justify-between px-2 py-1.5 text-xs font-medium text-gray-700">
                <span className="truncate">第 {volume.volume} 卷 · {volume.title}</span>
                <span className="ml-2 shrink-0 text-[10px] font-normal text-gray-400">{formatCount(volume.char_count)}</span>
              </div>
            )}
            {volume.chapters.map(chapter => (
              <div key={`${chapter.volume}.${chapter.chapter}`} className="mt-1 border-l border-gray-200 pl-1">
                {chapter.outline ? (
                  <button type="button" onClick={() => void selectDocument(chapter.outline!)} className={`flex w-full items-center justify-between rounded-md px-2 py-1 text-left text-xs transition-colors ${selected?.id === chapter.outline.id ? 'bg-purple-50 text-purple-700' : 'text-gray-600 hover:bg-gray-100'}`}>
                    <span className="min-w-0 truncate">第 {chapter.chapter} 章 · {chapter.title}</span>
                    <span className="ml-2 shrink-0 text-[10px] text-gray-400">{formatCount(chapter.char_count)}</span>
                  </button>
                ) : (
                  <div className="px-2 py-1 text-xs text-gray-600">第 {chapter.chapter} 章 · {chapter.title}</div>
                )}
                {chapter.sections.map(section => <DocumentButton key={section.id} document={section} selectedId={selected?.id ?? null} onSelect={selectDocument} indent />)}
              </div>
            ))}
          </section>
        ))}
        </aside>
        <div onMouseDown={startNavigatorResize} role="separator" aria-orientation="vertical" aria-label="调整小说目录宽度" className="absolute right-0 top-0 z-10 h-full w-1.5 cursor-col-resize hover:bg-purple-300 active:bg-purple-400" />
      </div> : <div className="flex w-8 shrink-0 items-start justify-center border-r border-gray-200 bg-[#fafbfc] pt-3 max-md:hidden">
        <button type="button" onClick={() => setShowNavigator(true)} className="rounded px-1.5 py-1 text-xs text-purple-600 hover:bg-purple-50" title="显示小说目录">›</button>
      </div>}

      <article className="min-w-0 flex-1 overflow-y-auto px-6 py-6 sm:px-10">
        {!selected && !loadingTree && <div className="flex h-full items-center justify-center text-sm text-gray-400">从左侧选择卷纲、章纲或小节正文。</div>}
        {loadingContent && <div className="py-20 text-center text-sm text-gray-400">正在加载内容…</div>}
        {selected && !loadingContent && content !== null && (
          <div className="mx-auto max-w-3xl">
            <div className="mb-6 border-b border-gray-100 pb-4">
              <p className="text-xs text-purple-600">{selected.path}</p>
              <div className="mt-2 flex items-baseline justify-between gap-3">
                <h1 className="text-2xl font-semibold text-gray-800">{selected.title}</h1>
                <span className="shrink-0 text-xs text-gray-400">{formatCount(selected.char_count)}</span>
              </div>
            </div>
            <div className="prose max-w-none text-gray-700 prose-headings:text-gray-800 prose-p:leading-8 prose-li:leading-7">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
            </div>
          </div>
        )}
      </article>
    </div>
  );
}
