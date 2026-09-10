import { useState } from 'react';
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

function FolderIcon({ open }: { open: boolean }) {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" aria-hidden="true" className="shrink-0 text-gray-500">
      <path d="M1.75 4.25A1.5 1.5 0 0 1 3.25 2.75h3.1l1.1 1.35h5.3a1.5 1.5 0 0 1 1.5 1.5v6.9a1.5 1.5 0 0 1-1.5 1.5h-9.5a1.5 1.5 0 0 1-1.5-1.5v-8.25Z" stroke="currentColor" strokeWidth="1.25" fill={open ? "#fef3c7" : "#f3f4f6"} />
    </svg>
  );
}

function Chevron({ open }: { open: boolean }) {
  return <span aria-hidden="true" className={`w-3 text-[10px] text-gray-400 transition-transform ${open ? 'rotate-90' : ''}`}>›</span>;
}

export function Sidebar({ chat }: SidebarProps) {
  const [collapsedProjects, setCollapsedProjects] = useState<Set<string>>(new Set());
  const activeProject = chat.projects.find(project => project.project_id === chat.activeProject) ?? null;

  const selectProject = (projectId: string) => {
    if (projectId === chat.activeProject) {
      setCollapsedProjects(current => {
        const next = new Set(current);
        if (next.has(projectId)) next.delete(projectId);
        else next.add(projectId);
        return next;
      });
      return;
    }
    setCollapsedProjects(current => {
      const next = new Set(current);
      next.delete(projectId);
      return next;
    });
    void chat.loadSessions(projectId);
  };

  return (
    <aside className="w-[264px] max-md:hidden bg-[#f7f9fb] border-r border-gray-200 text-gray-700 flex flex-col shrink-0 select-none">
      <div className="h-12 px-3 flex items-center justify-between shrink-0">
        <span className="text-[13px] font-medium text-gray-600">项目</span>
        <div className="flex items-center gap-1">
          <button type="button" className="w-7 h-7 rounded-md text-gray-400 hover:bg-gray-200 hover:text-gray-700 text-lg leading-none" aria-label="项目菜单">…</button>
          <button type="button" onClick={() => chat.setShowNewProject(true)} className="w-7 h-7 rounded-md text-gray-400 hover:bg-gray-200 hover:text-gray-700 text-lg leading-none" aria-label="新建项目">+</button>
        </div>
      </div>

      {chat.showNewProject && (
        <form
          onSubmit={event => { event.preventDefault(); void chat.handleNewProject(); }}
          className="mx-3 mb-2 rounded-lg border border-purple-200 bg-white p-2 shadow-sm"
        >
          <input
            autoFocus
            value={chat.newProjectName}
            onChange={event => chat.setNewProjectName(event.target.value)}
            placeholder="项目名称"
            className="w-full rounded-md border border-gray-200 px-2 py-1.5 text-xs outline-none focus:border-purple-400"
          />
          <div className="mt-2 flex justify-end gap-2">
            <button type="button" onClick={() => chat.setShowNewProject(false)} className="px-2 py-1 text-xs text-gray-500 hover:text-gray-700">取消</button>
            <button type="submit" className="rounded-md bg-purple-600 px-2.5 py-1 text-xs text-white hover:bg-purple-700">创建</button>
          </div>
        </form>
      )}

      <div className="flex-1 overflow-y-auto px-2 pb-4">
        {chat.projects.map(project => {
          const selected = project.project_id === chat.activeProject;
          const expanded = selected && !collapsedProjects.has(project.project_id);
          return (
            <div key={project.project_id} className="mb-0.5">
              <div className={`group flex items-center rounded-lg transition-colors ${selected ? 'bg-[#e8eef5]' : 'hover:bg-gray-200/70'}`}>
                <button type="button" onClick={() => selectProject(project.project_id)} className="flex min-w-0 flex-1 items-center gap-1.5 px-2 py-1.5 text-left text-[13px]">
                  <Chevron open={expanded} />
                  <FolderIcon open={expanded} />
                  <span className="truncate">{project.name}</span>
                </button>
                {selected && (
                  <button type="button" onClick={() => void chat.handleNewSession()} className="mr-1.5 h-6 w-6 rounded text-gray-400 opacity-0 group-hover:opacity-100 hover:bg-white hover:text-purple-600" aria-label={`在 ${project.name} 中新建会话`}>+</button>
                )}
              </div>

              {expanded && (
                <div className="ml-5 border-l border-gray-200 pl-1.5 py-0.5">
                  {chat.sessions.map(session => (
                    <div key={session.session_id} className={`group relative mb-0.5 rounded-md ${session.session_id === chat.activeSession ? 'bg-white shadow-sm ring-1 ring-gray-200' : 'hover:bg-gray-200/70'}`}>
                      <button
                        type="button"
                        onClick={() => void chat.loadSession(session.session_id)}
                        className="block w-full truncate px-2 py-1.5 pr-7 text-left text-xs text-gray-600"
                        title={session.title || '新会话'}
                      >{session.title || '新会话'}</button>
                      <button
                        type="button"
                        onClick={() => void chat.handleDeleteSession(session.session_id)}
                        className="absolute right-1 top-1/2 -translate-y-1/2 rounded px-1 text-xs text-gray-300 opacity-0 group-hover:opacity-100 hover:text-red-500"
                        aria-label={`删除会话：${session.title || '新会话'}`}
                      >×</button>
                    </div>
                  ))}
                  {chat.sessions.length === 0 && <div className="px-2 py-2 text-[11px] text-gray-400">暂无会话</div>}
                  <button type="button" onClick={() => void chat.handleNewSession()} className="mt-1 flex w-full items-center gap-1 rounded-md px-2 py-1.5 text-xs text-gray-400 hover:bg-gray-200/70 hover:text-purple-600">
                    <span className="text-sm leading-none">+</span> 新建会话
                  </button>
                </div>
              )}
            </div>
          );
        })}

        {chat.projects.length === 0 && !chat.showNewProject && (
          <div className="px-3 py-10 text-center text-xs leading-5 text-gray-400">创建一个项目后，会话、洞察与记忆都会归属在项目下。</div>
        )}
      </div>

      {activeProject && <div className="border-t border-gray-200 px-3 py-2 text-[11px] text-gray-400">{(activeProject.word_count ?? 0).toLocaleString()} 字</div>}
    </aside>
  );
}
