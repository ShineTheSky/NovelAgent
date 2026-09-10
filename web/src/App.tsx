import { ChatComposer } from './components/ChatComposer';
import { ChatTimeline } from './components/ChatTimeline';
import { ErrorBoundary } from './components/ErrorBoundary';
import { FileExplorer } from './components/FileExplorer';
import { LLMSettingsPanel } from './components/LLMSettingsPanel';
import { Sidebar } from './components/Sidebar';
import { TokenBar } from './components/TokenBar';
import { useChat } from './hooks/useChat';

export default function App() {
  const chat = useChat();

  return (
    <div className="flex h-screen bg-[#f8f9fb] font-sans">
      <Sidebar chat={chat} />

      <main className="flex-1 flex flex-col min-w-0">
        <header className="h-12 border-b border-gray-200 flex items-center justify-between px-5 bg-white/80 backdrop-blur shrink-0">
          <div className="flex items-center gap-2 text-sm text-gray-500">
            <span className="w-1.5 h-1.5 rounded-full bg-green-400" />
            {chat.activeSession ? '会话进行中' : '选择一个项目开始'}
          </div>
          <div className="flex items-center gap-3">
            {chat.activeSession && <TokenBar tokenCount={chat.tokenCount} limit={150000} onCompress={chat.handleCompress} disabled={chat.loading} />}
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
          <ChatTimeline
            messages={chat.messages}
            loading={chat.loading}
            currentAction={chat.currentAction}
            pendingAsk={chat.pendingAsk}
            pendingQuestion={chat.pendingQuestion}
            canRetry={chat.canRetry}
            onRetry={chat.handleRetry}
          />
        </ErrorBoundary>

        <ChatComposer
          input={chat.input}
          loading={chat.loading}
          onInputChange={chat.setInput}
          onSend={chat.handleSend}
          onStop={() => void chat.handleStop()}
        />
      </main>

      {chat.activeProject && <FileExplorer key={chat.activeProject} projectId={chat.activeProject} />}
    </div>
  );
}
