import { useState, useEffect, useRef, useCallback } from 'react';
import { listProjects, createProject, createSession, listSessions, getSession, sendMessage, stopGeneration, toggleAcceptEdits, permissionResponse, deleteSession, compressSession, questionResponse } from '../api/client';

export interface Message { id?: number; role: string; content: string; tool_calls?: any[]; tool_call_id?: string; name?: string; source?: string; preset?: string; }
export interface SessionInfo { session_id: string; title: string; updated_at: string; }
export interface ProjectInfo { project_id: string; name: string; genre: string; }
export interface PendingAsk { tool: string; params_summary: string; onAllow: () => void; onDeny: () => void; }
export interface PendingQuestion { questions: any[]; onSubmit: (answers: any[]) => void; }

export function useChat() {
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [activeProject, setActiveProject] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [activeSession, setActiveSession] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [acceptEdits, setAcceptEdits] = useState(false);
  const [showNewProject, setShowNewProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  const [pendingAsk, setPendingAsk] = useState<PendingAsk | null>(null);
  const [pendingQuestion, setPendingQuestion] = useState<PendingQuestion | null>(null);
  const [currentAction, setCurrentAction] = useState('');
  const [tokenCount, setTokenCount] = useState(0);
  const controllerRef = useRef<AbortController | null>(null);
  const msgIdRef = useRef(0);
  const lastUserMsgRef = useRef('');
  const subPresetRef = useRef('');

  // 页面加载时恢复上次选中的项目和会话
  useEffect(() => {
    listProjects().then(ps => {
      setProjects(ps);
      const savedProject = localStorage.getItem('activeProject');
      const savedSession = localStorage.getItem('activeSession');
      if (savedProject && ps.some(p => p.project_id === savedProject)) {
        setActiveProject(savedProject);
        listSessions(savedProject).then(ss => {
          setSessions(ss);
          if (savedSession && ss.some(s => s.session_id === savedSession)) {
            getSession(savedSession).then(s => {
              setActiveSession(savedSession);
              setMessages(s.messages || []);
              setAcceptEdits(s.accept_edits_mode || false);
            }).catch(() => {});
          }
        }).catch(() => {});
      }
    }).catch(() => {});
  }, []);

  // 选中项目时持久化
  useEffect(() => { if (activeProject) localStorage.setItem('activeProject', activeProject); }, [activeProject]);
  // 选中会话时持久化
  useEffect(() => { if (activeSession) localStorage.setItem('activeSession', activeSession); }, [activeSession]);

  const loadSessions = useCallback(async (pid: string) => {
    controllerRef.current?.abort();
    setLoading(false); setCurrentAction('');
    setActiveProject(pid); setActiveSession(null); setSessions([]); setMessages([]);
    try { setSessions(await listSessions(pid)); } catch {}
  }, []);

  const loadSession = useCallback(async (sid: string) => {
    controllerRef.current?.abort();
    setLoading(false); setCurrentAction('');
    setActiveSession(sid);
    try {
      const s = await getSession(sid);
      setMessages(s.messages || []);
      setAcceptEdits(s.accept_edits_mode || false);
      setTokenCount(s.token_count || 0);
    } catch { setMessages([]); }
  }, []);

  const handleNewProject = async () => {
    if (!newProjectName.trim()) return;
    const p = await createProject(newProjectName.trim());
    setProjects(prev => [p, ...prev]);
    setShowNewProject(false); setNewProjectName('');
    await loadSessions(p.project_id);
    const s = await createSession(p.project_id);
    setSessions(prev => [s, ...prev]);
    await loadSession(s.session_id);
  };

  const handleNewSession = async () => {
    if (!activeProject) return;
    const s = await createSession(activeProject);
    setSessions(prev => [s, ...prev]);
    await loadSession(s.session_id);
  };

  const handleSend = async () => {
    if (!input.trim() || !activeSession || loading) return;
    const content = input; setInput(''); setLoading(true);
    lastUserMsgRef.current = content;
    const mid = () => { msgIdRef.current++; return msgIdRef.current; };
    setMessages(prev => [...prev, { id: mid(), role: 'user', content }]);

    controllerRef.current = sendMessage(activeSession, content, acceptEdits,
      (type, data) => {
        // 409 冲突 → 会话正在处理中，忽略本次发送
        if (type === 'conflict') { setLoading(false); setMessages(prev => [...prev, { id: mid(), role: 'system', content: '⏳ 上一条消息还在处理中，请稍候' }]); return; }
        switch (type) {
          case 'text_delta':
            setCurrentAction(data.source === 'subagent' ? '子Agent创作中…' : '');
            setMessages(prev => {
              const msgs = [...prev];
              const last = msgs[msgs.length - 1];
              const role = data.source === 'subagent' ? 'subagent_assistant' : 'assistant';
              if (last?.role === role && !last.tool_calls?.length) msgs[msgs.length - 1] = { ...last, content: last.content + data.delta };
              else msgs.push({ id: mid(), role, content: data.delta, source: data.source, preset: subPresetRef.current });
              return msgs;
            }); break;
          case 'thinking':
            setCurrentAction(data.source === 'subagent' ? '子Agent工作中…' : '');
            setMessages(prev => {
              const last = prev[prev.length - 1];
              if (last && last.role === 'system' && last.source === (data.source || 'main') && last._thinking) {
                return [...prev.slice(0, -1), { ...last, content: last.content + data.content }];
              }
              return [...prev, { id: mid(), role: 'system', content: (data.source === 'subagent' ? '🤖 子Agent: ' : '💭 ') + data.content, source: data.source || 'main', _thinking: true }];
            }); break;
          case 'tool_call':
            // 主循环调用SubAgent时记录预设名
            if (!data.source && data.tool === 'SubAgent' && data.params?.preset) subPresetRef.current = data.params.preset;
            setCurrentAction(data.tool === 'Write' ? '正在写入文件…' : data.tool === 'SubAgent' ? '启动子Agent…' : `正在执行 ${data.tool}…`);
            setMessages(prev => [...prev, { id: mid(), role: 'system', content: `${data.source === 'subagent' ? '🤖 ' : ''}🔧 ${data.tool}`, name: 'tool_call', tool_calls: [data], source: data.source, preset: subPresetRef.current }]); break;
          case 'tool_result':
            setCurrentAction('');
            if (data.tool === 'Edit') console.log('[Edit result]', data.data?.slice(0, 200));
            setMessages(prev => [...prev, { id: mid(), role: 'tool_result', content: data.success ? (typeof data.data === 'string' ? data.data : JSON.stringify(data.data || '', null, 2)) : `❌ ${data.error}`, name: `${data.source === 'subagent' ? '🤖 ' : ''}${data.tool}`, source: data.source, preset: subPresetRef.current }]); break;
          case 'subagent_result':
            setCurrentAction('');
            setMessages(prev => [...prev, { id: mid(), role: 'subagent_result', content: data.content, name: data.preset, source: 'subagent', preset: data.preset }]); break;
          case 'subagent_done':
            setCurrentAction('');
            subPresetRef.current = '';
            setMessages(prev => [...prev, { id: mid(), role: 'system', content: `✅ 子Agent完成 — ${(data.result || '').slice(0, 150)}…`, source: 'subagent' }]); break;
          case 'permission_ask':
            setCurrentAction('等待确认…');
            setPendingAsk({ tool: data.tool, params_summary: data.params_summary || '',
              onAllow: () => { setPendingAsk(null); setCurrentAction(''); if (activeSession) permissionResponse(activeSession, true); },
              onDeny: () => { setPendingAsk(null); setCurrentAction(''); if (activeSession) permissionResponse(activeSession, false); },
            }); break;
          case 'question_ask':
            setCurrentAction('等待回答…');
            setPendingQuestion({ questions: data.questions || [],
              onSubmit: (answers: any[]) => { setPendingQuestion(null); setCurrentAction(''); if (activeSession) questionResponse(activeSession, answers); },
            }); break;
          case 'done':
            setLoading(false); setCurrentAction('');
            if (data.token_count) setTokenCount(data.token_count); break;
          case 'error':
            setMessages(prev => [...prev, { id: mid(), role: 'system', content: `⚠️ ${data.message}` }]);
            setLoading(false); setCurrentAction(''); break;
        }
      },
      () => {
        // 连接断开 → 自动从后端恢复最新消息
        setLoading(false); setCurrentAction('');
        if (activeSession) {
          getSession(activeSession).then(s => {
            if (s.messages?.length) setMessages(s.messages);
          }).catch(() => {});
        }
      }
    );
  };

  const handleStop = async () => { controllerRef.current?.abort(); if (activeSession) await stopGeneration(activeSession); setLoading(false); setCurrentAction(''); };
  const canRetry = !loading && messages.length > 0 && messages[messages.length - 1]?.role === 'system' && messages[messages.length - 1]?.content?.startsWith('⚠️');

  const handleCompress = useCallback(async () => {
    if (!activeSession || loading) return;
    setLoading(true); setCurrentAction('正在压缩上下文…');
    try {
      const result = await compressSession(activeSession);
      setTokenCount(result.token_count);
      setMessages(prev => [...prev, { id: msgIdRef.current++, role: 'system', content: `🧹 ${result.message}` }]);
    } catch {
      setMessages(prev => [...prev, { id: msgIdRef.current++, role: 'system', content: '⚠️ 压缩失败' }]);
    } finally {
      setLoading(false); setCurrentAction('');
    }
  }, [activeSession, loading]);

  return { projects, activeProject, sessions, activeSession, messages, input, setInput, loading, acceptEdits, setAcceptEdits, showNewProject, setShowNewProject, newProjectName, setNewProjectName, pendingAsk, pendingQuestion, currentAction, tokenCount, loadSessions, loadSession, handleNewProject, handleNewSession, handleSend, handleStop, handleCompress, toggleAcceptEdits, deleteSession, lastUserMsgRef, canRetry };
}
