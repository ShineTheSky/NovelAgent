import { useEffect, useRef, useState } from 'react';
import { ChatComposer } from './components/ChatComposer';
import { ChatTimeline } from './components/ChatTimeline';
import { ErrorBoundary } from './components/ErrorBoundary';
import { LLMSettingsPanel } from './components/LLMSettingsPanel';
import { NovelWorkspace } from './components/NovelWorkspace';
import { ProjectInsightsPanel } from './components/ProjectInsightsPanel';
import { ProjectOverview } from './components/ProjectOverview';
import { RagPanel } from './components/RagPanel';
import { Sidebar } from './components/Sidebar';
import { TokenBar } from './components/TokenBar';
import { useChat } from './hooks/useChat';

export default function App() {
  const chat = useChat();
  const [showRag, setShowRag] = useState(false);
  const [showChatPane, setShowChatPane] = useState(true);
  const [showNovelPane, setShowNovelPane] = useState(true);
  const [chatPaneWidth, setChatPaneWidth] = useState(560);
  const splitRef = useRef<HTMLDivElement>(null);
  const resizingRef = useRef(false);
  const resizeStartXRef = useRef(0);
  const resizeStartWidthRef = useRef(560);
  const activeProject = chat.projects.find(project => project.project_id === chat.activeProject) ?? null;

  useEffect(() => {
    const onMouseMove = (event: MouseEvent) => {
      if (!resizingRef.current || !splitRef.current) return;
      const maxWidth = Math.max(360, splitRef.current.clientWidth - 440);
      setChatPaneWidth(Math.max(360, Math.min(maxWidth, resizeStartWidthRef.current + event.clientX - resizeStartXRef.current)));
    };
    const onMouseUp = () => { resizingRef.current = false; };
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    return () => {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
    };
  }, []);

  const startResize = (event: React.MouseEvent) => {
    event.preventDefault();
    resizingRef.current = true;
    resizeStartXRef.current = event.clientX;
    resizeStartWidthRef.current = chatPaneWidth;
  };

  const toggleChatPane = () => {
    if (showChatPane && !showNovelPane) return;
    setShowChatPane(current => !current);
  };

  const toggleNovelPane = () => {
    if (showNovelPane && !showChatPane) return;
    setShowNovelPane(current => !current);
  };

  return (
    <div className="flex h-screen bg-[#f8f9fb] font-sans">
      <Sidebar chat={chat} />

      <main className="flex-1 flex flex-col min-w-0">
        <header className="h-14 border-b border-gray-200 flex items-center gap-3 px-4 bg-white/80 backdrop-blur shrink-0">
          <div className="min-w-0 flex-1 flex items-center gap-2 text-sm text-gray-500">
            <span className="w-1.5 h-1.5 rounded-full bg-green-400 shrink-0" />
            <span className="truncate">{chat.activeSession ? '会话进行中' : activeProject ? `${activeProject.name} · 对话与小说` : '选择一个项目开始'}</span>
          </div>
          {activeProject && (
            <div className="flex rounded-lg bg-gray-100 p-0.5 text-xs" aria-label="显示面板">
              <button type="button" aria-pressed={showChatPane} onClick={toggleChatPane} className={`rounded-md px-2.5 py-1 transition-colors ${showChatPane ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>对话</button>
              <button type="button" aria-pressed={showNovelPane} onClick={toggleNovelPane} className={`rounded-md px-2.5 py-1 transition-colors ${showNovelPane ? 'bg-white text-purple-700 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>小说</button>
            </div>
          )}
          <div className="flex shrink-0 items-center gap-2">
            {chat.activeSession && <TokenBar tokenCount={chat.tokenCount} limit={150000} onCompress={chat.handleCompress} disabled={chat.loading} />}
            <ProjectInsightsPanel projectId={chat.activeProject} />
            <button type="button" onClick={() => setShowRag(true)} className="rounded-md border border-gray-200 px-2.5 py-1.5 text-xs text-gray-600 hover:border-purple-300 hover:text-purple-600">资料库</button>
            <LLMSettingsPanel />
            <button
              type="button"
              onClick={() => void chat.handleToggleAcceptEdits(!chat.acceptEdits)}
              disabled={!chat.activeSession}
              role="switch"
              aria-checked={chat.acceptEdits}
              aria-label={chat.acceptEdits ? '关闭自动接受编辑' : '开启自动接受编辑'}
              className={`flex items-center gap-1.5 text-xs cursor-pointer disabled:cursor-not-allowed disabled:opacity-40 ${chat.acceptEdits ? 'text-purple-600' : 'text-gray-400'}`}
            >
              <span className={`w-8 h-4 rounded-full relative transition-colors ${chat.acceptEdits ? 'bg-purple-500' : 'bg-gray-300'}`}>
                <span className={`w-3 h-3 rounded-full bg-white absolute top-0.5 transition-all ${chat.acceptEdits ? 'left-4' : 'left-0.5'}`} />
              </span>
              <span className="select-none">acceptEdits</span>
            </button>
          </div>
        </header>

        {chat.appError && <div role="alert" className="mx-4 mt-3 rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-xs text-red-600">{chat.appError}</div>}

        <ErrorBoundary>
          {activeProject ? (
            <div ref={splitRef} className="flex min-h-0 flex-1 overflow-hidden">
              {showChatPane && <section className={`flex flex-col bg-white ${showNovelPane ? 'shrink-0' : 'min-w-0 flex-1'}`} style={showNovelPane ? { width: chatPaneWidth } : undefined}>
                {!chat.activeSession ? (
                  <ProjectOverview
                    project={activeProject}
                    sessions={chat.sessions}
                    onNewSession={() => void chat.handleNewSession()}
                    onSelectSession={sessionId => void chat.loadSession(sessionId)}
                  />
                ) : (
                  <>
                    <ChatTimeline
                      messages={chat.messages}
                      loading={chat.loading}
                      currentAction={chat.currentAction}
                      pendingAsk={chat.pendingAsk}
                      pendingQuestion={chat.pendingQuestion}
                      canRetry={chat.canRetry}
                      onRetry={chat.handleRetry}
                    />
                    <ChatComposer
                      input={chat.input}
                      loading={chat.loading}
                      onInputChange={chat.setInput}
                      onSend={chat.handleSend}
                      onStop={() => void chat.handleStop()}
                    />
                  </>
                )}
              </section>}
              {showChatPane && showNovelPane && <div role="separator" aria-orientation="vertical" aria-label="调整对话与小说区域宽度" onMouseDown={startResize} className="w-1.5 shrink-0 cursor-col-resize bg-gray-100 hover:bg-purple-300 active:bg-purple-400" />}
              {showNovelPane && <section className="flex min-w-[27.5rem] flex-1 overflow-hidden bg-white">
                <NovelWorkspace key={activeProject.project_id} projectId={activeProject.project_id} />
              </section>}
            </div>
          ) : (
            <div className="flex flex-1 items-center justify-center bg-white text-sm text-gray-400">从左侧创建或选择一个项目。</div>
          )}
        </ErrorBoundary>
      </main>

      {showRag && <RagPanel onClose={() => setShowRag(false)} />}
    </div>
  );
}
