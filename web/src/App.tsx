import { useState } from 'react';
import { ChatComposer } from './components/ChatComposer';
import { ChatTimeline } from './components/ChatTimeline';
import { ErrorBoundary } from './components/ErrorBoundary';
import { FileExplorer } from './components/FileExplorer';
import { LLMSettingsPanel } from './components/LLMSettingsPanel';
import { ProjectInsightsPanel } from './components/ProjectInsightsPanel';
import { ProjectOverview } from './components/ProjectOverview';
import { RagPanel } from './components/RagPanel';
import { Sidebar } from './components/Sidebar';
import { TokenBar } from './components/TokenBar';
import { useChat } from './hooks/useChat';

export default function App() {
  const chat = useChat();
  const [showRag, setShowRag] = useState(false);
  const activeProject = chat.projects.find(project => project.project_id === chat.activeProject) ?? null;

  return (
    <div className="flex h-screen bg-[#f8f9fb] font-sans">
      <Sidebar chat={chat} />

      <main className="flex-1 flex flex-col min-w-0">
        <header className="h-14 border-b border-gray-200 flex items-center gap-3 px-4 bg-white/80 backdrop-blur shrink-0">
          <div className="min-w-0 flex-1 flex items-center gap-2 text-sm text-gray-500">
            <span className="w-1.5 h-1.5 rounded-full bg-green-400 shrink-0" />
            <span className="truncate">{chat.activeSession ? '会话进行中' : activeProject ? `${activeProject.name} · 项目总览` : '选择一个项目开始'}</span>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {chat.activeSession && <TokenBar tokenCount={chat.tokenCount} limit={150000} onCompress={chat.handleCompress} disabled={chat.loading} />}
            <ProjectInsightsPanel projectId={chat.activeProject} />
            {chat.activeProject && <button type="button" onClick={() => setShowRag(true)} className="rounded-md border border-gray-200 px-2.5 py-1.5 text-xs text-gray-600 hover:border-purple-300 hover:text-purple-600">资料库</button>}
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
          {activeProject && !chat.activeSession ? (
            <ProjectOverview
              project={activeProject}
              sessions={chat.sessions}
              onNewSession={() => void chat.handleNewSession()}
              onSelectSession={sessionId => void chat.loadSession(sessionId)}
            />
          ) : chat.activeSession ? (
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
          ) : (
            <div className="flex flex-1 items-center justify-center bg-white text-sm text-gray-400">从左侧创建或选择一个项目。</div>
          )}
        </ErrorBoundary>
      </main>

      {chat.activeProject && <FileExplorer key={chat.activeProject} projectId={chat.activeProject} />}
      {showRag && chat.activeProject && <RagPanel projectId={chat.activeProject} onClose={() => setShowRag(false)} />}
    </div>
  );
}
