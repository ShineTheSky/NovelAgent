import { useEffect, useRef } from 'react';
import { useChat } from './hooks/useChat';
import { MessageBubble } from './components/MessageBubble';
import { ToolCard } from './components/ToolCard';
import { ToolResultCard } from './components/ToolResultCard';

import { SubagentGroup } from './components/SubagentGroup';
import { TokenBar } from './components/TokenBar';
import { PermissionDialog } from './components/PermissionDialog';
import { QuestionDialog } from './components/QuestionDialog';
import { ErrorBoundary } from './components/ErrorBoundary';

export default function App() {
  const chat = useChat();
  const chatEndRef = useRef<HTMLDivElement>(null);
  useEffect(() => { chatEndRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [chat.messages]);

  return (
    <div className="flex h-screen bg-[#f8f9fb] font-sans">
      {/* Sidebar */}
      <aside className="w-[260px] max-md:hidden bg-[#1a1a2e] text-white flex flex-col shrink-0 select-none">
        <div className="px-4 py-3 border-b border-white/10">
          <h1 className="text-base font-semibold tracking-tight">NovelAgent<span className="text-purple-400">2</span></h1>
        </div>
        <div className="px-3 py-2 border-b border-white/10">
          {chat.showNewProject ? (
            <div className="flex gap-1.5">
              <input autoFocus value={chat.newProjectName} onChange={e => chat.setNewProjectName(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && chat.handleNewProject()}
                placeholder="项目名称…" className="flex-1 bg-white/10 border border-white/10 rounded px-2 py-1 text-xs text-white outline-none focus:border-purple-400/50" />
              <button onClick={chat.handleNewProject} className="px-2 py-1 text-xs bg-purple-600 hover:bg-purple-500 rounded">创建</button>
              <button onClick={() => chat.setShowNewProject(false)} className="text-xs text-white/40 hover:text-white/60">✕</button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <select value={chat.activeProject || ''} onChange={e => chat.loadSessions(e.target.value)}
                className="flex-1 bg-white/10 border border-white/10 rounded-lg px-2.5 py-1.5 text-sm text-white/80 outline-none focus:border-purple-400/50 cursor-pointer">
                <option value="" className="bg-[#1a1a2e]">{chat.projects.length === 0 ? '点击 + 创建项目' : '选择项目'}</option>
                {chat.projects.map(p => <option key={p.project_id} value={p.project_id} className="bg-[#1a1a2e]">{p.name}</option>)}
              </select>
              <button onClick={() => chat.setShowNewProject(true)} aria-label="新建项目" className="shrink-0 w-7 h-7 flex items-center justify-center rounded-lg text-white/40 hover:text-purple-300 hover:bg-white/5 text-lg">+</button>
            </div>
          )}
        </div>
        <div className="flex items-center justify-between px-3 py-1.5 border-b border-white/5">
          <span className="text-[10px] uppercase tracking-wider text-white/30">会话</span>
          {chat.activeProject && <button onClick={chat.handleNewSession} aria-label="新建会话" className="text-white/30 hover:text-purple-300 text-sm">+</button>}
        </div>
        <div className="flex-1 overflow-y-auto py-1">
          {chat.sessions.map(s => (
            <button key={s.session_id} onClick={() => chat.loadSession(s.session_id)}
              className={`group w-full text-left mx-2 my-0.5 px-3 py-2 rounded-lg cursor-pointer text-sm transition-all outline-none focus-visible:ring-1 focus-visible:ring-purple-400 ${s.session_id === chat.activeSession ? 'bg-purple-600/30 border border-purple-400/30' : 'hover:bg-white/5 border border-transparent'}`}>
              <div className="flex items-center justify-between">
                <span className="truncate text-white/80 flex-1">{s.title || '新会话'}</span>
                <span onClick={e => { e.stopPropagation(); chat.deleteSession(s.session_id).then(() => chat.loadSessions(chat.activeProject!)); }}
                  role="button" aria-label="删除会话" tabIndex={0} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); chat.deleteSession(s.session_id).then(() => chat.loadSessions(chat.activeProject!)); } }}
                  className="opacity-0 group-hover:opacity-100 text-white/30 hover:text-red-400 transition-all text-xs ml-1">✕</span>
              </div>
              <div className="text-[10px] text-white/30 mt-0.5">{s.updated_at?.slice(5, 16)}</div>
            </button>
          ))}
          {chat.sessions.length === 0 && chat.activeProject && <div className="px-4 py-8 text-center text-xs text-white/50">暂无会话</div>}
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 flex flex-col min-w-0">
        <header className="h-12 border-b border-gray-200 flex items-center justify-between px-5 bg-white/80 backdrop-blur shrink-0">
          <div className="flex items-center gap-2 text-sm text-gray-500">
            <span className="w-1.5 h-1.5 rounded-full bg-green-400" />
            {chat.activeSession ? '会话进行中' : '选择一个项目开始'}
          </div>
          <div className="flex items-center gap-3">
            {chat.activeSession && <TokenBar tokenCount={chat.tokenCount} limit={150000} sessionId={chat.activeSession} onCompress={chat.handleCompress} disabled={chat.loading} />}
            <button onClick={() => { chat.setAcceptEdits(!chat.acceptEdits); if (chat.activeSession) chat.toggleAcceptEdits(chat.activeSession, !chat.acceptEdits); }}
              role="switch" aria-checked={chat.acceptEdits} aria-label={chat.acceptEdits ? '关闭自动接受编辑' : '开启自动接受编辑'} className={`flex items-center gap-1.5 text-xs cursor-pointer ${chat.acceptEdits ? 'text-purple-600' : 'text-gray-400'}`}>
              <div className={`w-8 h-4 rounded-full relative transition-colors ${chat.acceptEdits ? 'bg-purple-500' : 'bg-gray-300'}`}>
                <div className={`w-3 h-3 rounded-full bg-white absolute top-0.5 transition-all ${chat.acceptEdits ? 'left-4' : 'left-0.5'}`} />
              </div>
              <span className="select-none">acceptEdits</span>
            </button>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto" role="log" aria-live="polite" aria-label="对话消息">
          <ErrorBoundary>
          {chat.messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-gray-300 select-none">
              <div className="text-6xl mb-4">✎</div>
              <div className="text-lg font-medium text-gray-400">NovelAgent2</div>
              <div className="text-sm text-gray-300 mt-1">选择或创建一个项目开始创作</div>
            </div>
          ) : (
            <div className="max-w-3xl mx-auto px-4 py-6 space-y-4">
              {(() => {
                const elements: React.ReactNode[] = [];
                let i = 0;
                while (i < chat.messages.length) {
                  const m = chat.messages[i];
                  if (m.source === 'subagent') {
                    const groupMsgs: typeof chat.messages = [];
                    let groupPreset = m.preset || '';
                    while (i < chat.messages.length && chat.messages[i]?.source === 'subagent') {
                      groupMsgs.push(chat.messages[i]);
                      if (!groupPreset && chat.messages[i].preset) groupPreset = chat.messages[i].preset;
                      i++;
                    }
                    elements.push(<SubagentGroup key={groupMsgs[0]?.id || `sg-${i}`} messages={groupMsgs} preset={groupPreset} />);
                  } else {
                    elements.push(
                      <div key={m.id || i}>
                        {m.name === 'tool_call' ? <ToolCard tool={m.tool_calls?.[0]?.tool || ''} params={m.tool_calls?.[0]?.input || {}} />
                        : m.role === 'tool_result' ? <ToolResultCard toolName={m.name || ''} content={m.content} />
                        : <MessageBubble role={m.role} content={m.content} />}
                      </div>
                    );
                    i++;
                  }
                }
                return elements;
              })()}
              {chat.pendingAsk && <PermissionDialog ask={chat.pendingAsk} />}
              {chat.pendingQuestion && <QuestionDialog qa={chat.pendingQuestion} />}
              {chat.loading && (
                <div className="flex items-center gap-2 px-1 py-2">
                  <div className="flex gap-1"><div className="w-2 h-2 bg-purple-400 rounded-full animate-bounce motion-reduce:animate-none" /><div className="w-2 h-2 bg-purple-400 rounded-full animate-bounce motion-reduce:animate-none" style={{ animationDelay: '150ms' }} /><div className="w-2 h-2 bg-purple-400 rounded-full animate-bounce motion-reduce:animate-none" style={{ animationDelay: '300ms' }} /></div>
                  <span className="text-xs text-gray-400 ml-2">{chat.currentAction || 'AI 思考中…'}</span>
                </div>
              )}
              {chat.canRetry && (
                <div className="flex justify-center my-2">
                  <button onClick={() => { chat.setInput(chat.lastUserMsgRef.current); setTimeout(chat.handleSend, 50); }}
                    className="text-xs text-purple-600 hover:text-purple-700 border border-purple-200 rounded-full px-3 py-1">重试</button>
                </div>
              )}
              <div ref={chatEndRef} />
            </div>
          )}
          </ErrorBoundary>
        </div>

        <div className="border-t border-gray-200 bg-white px-4 py-3 shrink-0">
          <div className="max-w-3xl mx-auto flex gap-3 items-end">
            <textarea value={chat.input} onChange={e => chat.setInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && e.ctrlKey) chat.handleSend(); }}
              placeholder="输入消息… (Ctrl+Enter 发送)" rows={1} autocomplete="off"
              className="flex-1 resize-none rounded-xl border border-gray-200 px-4 py-3 text-sm outline-none focus-visible:border-purple-300 focus-visible:ring-2 focus-visible:ring-purple-100 placeholder:text-gray-400 transition-all"
              disabled={chat.loading}
              onInput={e => { const t = e.target as HTMLTextAreaElement; t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 160) + 'px'; }} />
            {chat.loading ? (
              <button onClick={chat.handleStop} aria-label="停止生成" className="shrink-0 w-10 h-10 flex items-center justify-center rounded-xl bg-red-50 text-red-500 hover:bg-red-100">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="3" y="3" width="10" height="10" rx="1" /></svg>
              </button>
            ) : (
              <button onClick={chat.handleSend} aria-label="发送消息" className="shrink-0 w-10 h-10 flex items-center justify-center rounded-xl bg-purple-600 text-white hover:bg-purple-700 disabled:opacity-30">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M1.5 1.5L14.5 8L1.5 14.5L4 8L1.5 1.5Z" /></svg>
              </button>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
