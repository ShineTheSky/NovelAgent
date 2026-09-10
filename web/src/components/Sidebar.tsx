import type { ChatController } from '../hooks/useChat';

interface SidebarProps {
  chat: Pick<ChatController,
    | 'projects'
    | 'activeProject'
    | 'sessions'
    | 'activeSession'
    | 'showNewProject'
    | 'setShowNewProject'
    | 'newProjectName'
    | 'setNewProjectName'
    | 'loadSessions'
    | 'loadSession'
    | 'handleNewProject'
    | 'handleNewSession'
    | 'handleDeleteSession'>;
}

export function Sidebar({ chat }: SidebarProps) {
  return (
    <aside className="w-[260px] max-md:hidden bg-[#1a1a2e] text-white flex flex-col shrink-0 select-none">
      <div className="px-4 py-3 border-b border-white/10">
        <h1 className="text-base font-semibold tracking-tight">NovelAgent<span className="text-purple-400">2</span></h1>
      </div>

      <div className="px-3 py-2 border-b border-white/10">
        {chat.showNewProject ? (
          <div className="flex gap-1.5">
            <input
              autoFocus
              value={chat.newProjectName}
              onChange={event => chat.setNewProjectName(event.target.value)}
              onKeyDown={event => event.key === 'Enter' && void chat.handleNewProject()}
              placeholder="项目名称…"
              className="flex-1 bg-white/10 border border-white/10 rounded px-2 py-1 text-xs text-white outline-none focus:border-purple-400/50"
            />
            <button type="button" onClick={() => void chat.handleNewProject()} className="px-2 py-1 text-xs bg-purple-600 hover:bg-purple-500 rounded">创建</button>
            <button type="button" onClick={() => chat.setShowNewProject(false)} aria-label="取消创建项目" className="text-xs text-white/40 hover:text-white/60">✕</button>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <select
              value={chat.activeProject || ''}
              onChange={event => { if (event.target.value) void chat.loadSessions(event.target.value); }}
              className="flex-1 bg-white/10 border border-white/10 rounded-lg px-2.5 py-1.5 text-sm text-white/80 outline-none focus:border-purple-400/50 cursor-pointer"
            >
              <option value="" className="bg-[#1a1a2e]">{chat.projects.length === 0 ? '点击 + 创建项目' : '选择项目'}</option>
              {chat.projects.map(project => <option key={project.project_id} value={project.project_id} className="bg-[#1a1a2e]">{project.name}</option>)}
            </select>
            <button type="button" onClick={() => chat.setShowNewProject(true)} aria-label="新建项目" className="shrink-0 w-7 h-7 flex items-center justify-center rounded-lg text-white/40 hover:text-purple-300 hover:bg-white/5 text-lg">+</button>
          </div>
        )}
      </div>

      <div className="flex items-center justify-between px-3 py-1.5 border-b border-white/5">
        <span className="text-[10px] uppercase tracking-wider text-white/30">会话</span>
        {chat.activeProject && <button type="button" onClick={() => void chat.handleNewSession()} aria-label="新建会话" className="text-white/30 hover:text-purple-300 text-sm">+</button>}
      </div>

      <div className="flex-1 overflow-y-auto py-1">
        {chat.sessions.map(session => (
          <div key={session.session_id} className={`group relative mx-2 my-0.5 rounded-lg border transition-all ${session.session_id === chat.activeSession ? 'bg-purple-600/30 border-purple-400/30' : 'hover:bg-white/5 border-transparent'}`}>
            <button
              type="button"
              onClick={() => void chat.loadSession(session.session_id)}
              className="w-full text-left px-3 py-2 rounded-lg cursor-pointer text-sm outline-none focus-visible:ring-1 focus-visible:ring-purple-400"
            >
              <span className="block truncate text-white/80 pr-5">{session.title || '新会话'}</span>
              <span className="block text-[10px] text-white/30 mt-0.5">{session.updated_at?.slice(5, 16)}</span>
            </button>
            <button
              type="button"
              onClick={() => void chat.handleDeleteSession(session.session_id)}
              aria-label={`删除会话：${session.title || '新会话'}`}
              className="absolute right-2 top-2 opacity-0 group-hover:opacity-100 text-white/30 hover:text-red-400 transition-all text-xs"
            >
              ✕
            </button>
          </div>
        ))}
        {chat.sessions.length === 0 && chat.activeProject && <div className="px-4 py-8 text-center text-xs text-white/50">暂无会话</div>}
      </div>
    </aside>
  );
}
