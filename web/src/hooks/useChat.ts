import { useCallback, useEffect, useRef, useState } from 'react';
import {
  compressSession,
  createProject,
  createSession,
  deleteSession,
  getSession,
  listProjects,
  listImportableProjects,
  listSessions,
  importExistingProject,
  permissionResponse,
  questionResponse,
  sendMessage,
  stopGeneration,
  toggleAcceptEdits,
} from '../api/client';
import type {
  Message,
  PendingAsk,
  PendingQuestion,
  ProjectInfo,
  ImportableProject,
  QuestionAnswer,
  SessionInfo,
  StreamEvent,
} from '../types/chat';

function messageText(value: unknown) {
  return typeof value === 'string' ? value : '';
}

function normalizeMessages(messages: Message[] | undefined): Message[] {
  return (messages ?? []).map(message => ({ ...message, content: messageText(message.content) }));
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : '操作失败，请稍后重试';
}

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
  const [showImportProjects, setShowImportProjects] = useState(false);
  const [importableProjects, setImportableProjects] = useState<ImportableProject[]>([]);
  const [importLoading, setImportLoading] = useState(false);
  const [pendingAsk, setPendingAsk] = useState<PendingAsk | null>(null);
  const [pendingQuestion, setPendingQuestion] = useState<PendingQuestion | null>(null);
  const [currentAction, setCurrentAction] = useState('');
  const [tokenCount, setTokenCount] = useState(0);
  const [appError, setAppError] = useState<string | null>(null);

  const controllerRef = useRef<AbortController | null>(null);
  const activeSessionRef = useRef<string | null>(null);
  const streamIdRef = useRef(0);
  const navigationIdRef = useRef(0);
  const msgIdRef = useRef(0);
  const lastUserMsgRef = useRef('');
  const subPresetRef = useRef('');

  const nextMessageId = useCallback(() => ++msgIdRef.current, []);
  const appendSystemMessage = useCallback((content: string) => {
    setMessages(previous => [...previous, { id: nextMessageId(), role: 'system', content }]);
  }, [nextMessageId]);

  const cancelActiveStream = useCallback(() => {
    streamIdRef.current += 1;
    controllerRef.current?.abort();
    controllerRef.current = null;
    setLoading(false);
    setCurrentAction('');
    setPendingAsk(null);
    setPendingQuestion(null);
    subPresetRef.current = '';
  }, []);

  useEffect(() => {
    activeSessionRef.current = activeSession;
    if (activeSession) localStorage.setItem('activeSession', activeSession);
    else localStorage.removeItem('activeSession');
  }, [activeSession]);

  useEffect(() => {
    if (activeProject) localStorage.setItem('activeProject', activeProject);
    else localStorage.removeItem('activeProject');
  }, [activeProject]);

  useEffect(() => {
    let disposed = false;

    const restorePreviousSession = async () => {
      try {
        const availableProjects = await listProjects();
        if (disposed) return;
        setProjects(availableProjects);

        const savedProject = localStorage.getItem('activeProject');
        const savedSession = localStorage.getItem('activeSession');
        if (!savedProject || !availableProjects.some(project => project.project_id === savedProject)) return;

        setActiveProject(savedProject);
        const availableSessions = await listSessions(savedProject);
        if (disposed) return;
        setSessions(availableSessions);
        if (!savedSession || !availableSessions.some(session => session.session_id === savedSession)) return;

        const session = await getSession(savedSession);
        if (disposed) return;
        activeSessionRef.current = savedSession;
        setActiveSession(savedSession);
        setMessages(normalizeMessages(session.messages));
        setAcceptEdits(session.accept_edits_mode);
        setTokenCount(session.token_count);
      } catch (error) {
        if (!disposed) setAppError(`初始化失败：${errorText(error)}`);
      }
    };

    void restorePreviousSession();
    return () => { disposed = true; };
  }, []);

  const loadSessions = useCallback(async (projectId: string) => {
    cancelActiveStream();
    const navigationId = ++navigationIdRef.current;
    activeSessionRef.current = null;
    setActiveProject(projectId);
    setActiveSession(null);
    setSessions([]);
    setMessages([]);
    setTokenCount(0);
    setAppError(null);

    try {
      const availableSessions = await listSessions(projectId);
      if (navigationId === navigationIdRef.current) setSessions(availableSessions);
    } catch (error) {
      if (navigationId === navigationIdRef.current) setAppError(`加载会话失败：${errorText(error)}`);
    }
  }, [cancelActiveStream]);

  const loadSession = useCallback(async (sessionId: string) => {
    cancelActiveStream();
    const navigationId = ++navigationIdRef.current;
    activeSessionRef.current = sessionId;
    setActiveSession(sessionId);
    setMessages([]);
    setTokenCount(0);
    setAppError(null);

    try {
      const session = await getSession(sessionId);
      if (navigationId !== navigationIdRef.current) return;
      setMessages(normalizeMessages(session.messages));
      setAcceptEdits(session.accept_edits_mode);
      setTokenCount(session.token_count);
    } catch (error) {
      if (navigationId !== navigationIdRef.current) return;
      setMessages([]);
      setAppError(`加载会话失败：${errorText(error)}`);
    }
  }, [cancelActiveStream]);

  const handleNewProject = useCallback(async () => {
    const name = newProjectName.trim();
    if (!name) return;
    setAppError(null);

    try {
      const project = await createProject(name);
      setProjects(previous => [project, ...previous]);
      setShowNewProject(false);
      setNewProjectName('');
      await loadSessions(project.project_id);
    } catch (error) {
      setAppError(`创建项目失败：${errorText(error)}`);
    }
  }, [loadSessions, newProjectName]);

  const loadImportableProjects = useCallback(async () => {
    setImportLoading(true);
    setAppError(null);
    try {
      setImportableProjects(await listImportableProjects());
    } catch (error) {
      setAppError(`读取可导入项目失败：${errorText(error)}`);
    } finally {
      setImportLoading(false);
    }
  }, []);

  const handleImportProject = useCallback(async (projectId: string) => {
    setImportLoading(true);
    setAppError(null);
    try {
      const project = await importExistingProject(projectId);
      setProjects(previous => [project, ...previous]);
      setImportableProjects(previous => previous.filter(item => item.project_id !== projectId));
      setShowImportProjects(false);
      await loadSessions(project.project_id);
    } catch (error) {
      setAppError(`导入项目失败：${errorText(error)}`);
    } finally {
      setImportLoading(false);
    }
  }, [loadSessions]);

  const handleNewSession = useCallback(async () => {
    if (!activeProject) return;
    setAppError(null);
    try {
      const session = await createSession(activeProject);
      setSessions(previous => [{ session_id: session.session_id, title: '新会话', updated_at: '' }, ...previous]);
      await loadSession(session.session_id);
    } catch (error) {
      setAppError(`创建会话失败：${errorText(error)}`);
    }
  }, [activeProject, loadSession]);

  const handleStreamEvent = useCallback((event: StreamEvent, sessionId: string, streamId: number, optimisticMessageId: number) => {
    const isCurrentStream = () => activeSessionRef.current === sessionId && streamIdRef.current === streamId;
    if (!isCurrentStream()) return;

    switch (event.type) {
      case 'conflict':
        setMessages(previous => previous.filter(message => message.id !== optimisticMessageId));
        appendSystemMessage('⏳ 上一条消息还在处理中，请稍候');
        setLoading(false);
        break;
      case 'text_delta': {
        const source = event.source ?? 'main';
        const role = source === 'subagent' ? 'subagent_assistant' : 'assistant';
        setCurrentAction(source === 'subagent' ? '子 Agent 创作中…' : '');
        setMessages(previous => {
          const next = [...previous];
          const last = next.at(-1);
          if (last?.role === role && !last.tool_calls?.length) {
            next[next.length - 1] = { ...last, content: last.content + event.delta };
          } else {
            next.push({ id: nextMessageId(), role, content: event.delta, source, preset: subPresetRef.current });
          }
          return next;
        });
        break;
      }
      case 'thinking': {
        const source = event.source ?? 'main';
        setCurrentAction(source === 'subagent' ? '子 Agent 工作中…' : '');
        setMessages(previous => {
          const last = previous.at(-1);
          if (last?.role === 'system' && last.source === source && last._thinking) {
            return [...previous.slice(0, -1), { ...last, content: last.content + event.content }];
          }
          return [...previous, {
            id: nextMessageId(),
            role: 'system',
            content: `${source === 'subagent' ? '🤖 子Agent: ' : '💭 '}${event.content}`,
            source,
            _thinking: true,
          }];
        });
        break;
      }
      case 'tool_call':
        if (event.source !== 'subagent' && event.tool === 'SubAgent' && typeof event.params.preset === 'string') {
          subPresetRef.current = event.params.preset;
        }
        setCurrentAction(event.tool === 'Write' ? '正在写入文件…' : event.tool === 'SubAgent' ? '启动子 Agent…' : `正在执行 ${event.tool}…`);
        setMessages(previous => [...previous, {
          id: nextMessageId(),
          role: 'system',
          content: `${event.source === 'subagent' ? '🤖 ' : ''}🔧 ${event.tool}`,
          name: 'tool_call',
          tool_calls: [{ tool: event.tool, params: event.params }],
          source: event.source ?? 'main',
          preset: subPresetRef.current,
        }]);
        break;
      case 'tool_result':
        setCurrentAction('');
        setMessages(previous => [...previous, {
          id: nextMessageId(),
          role: 'tool_result',
          content: event.success
            ? typeof event.data === 'string' ? event.data : JSON.stringify(event.data ?? '', null, 2)
            : `❌ ${event.error ?? '工具执行失败'}`,
          name: `${event.source === 'subagent' ? '🤖 ' : ''}${event.tool}`,
          source: event.source ?? 'main',
          preset: subPresetRef.current,
        }]);
        break;
      case 'subagent_result':
        setCurrentAction('');
        setMessages(previous => [...previous, {
          id: nextMessageId(),
          role: 'subagent_result',
          content: event.content,
          name: event.preset,
          source: 'subagent',
          preset: event.preset,
        }]);
        break;
      case 'subagent_done':
        setCurrentAction('');
        subPresetRef.current = '';
        setMessages(previous => [...previous, {
          id: nextMessageId(),
          role: 'system',
          content: `✅ 子Agent完成 — ${(event.result ?? '').slice(0, 150)}…`,
          source: 'subagent',
        }]);
        break;
      case 'permission_ask':
        setCurrentAction('等待确认…');
        setPendingAsk({
          tool: event.tool,
          params_summary: event.params_summary ?? '',
          onAllow: () => {
            setPendingAsk(null);
            setCurrentAction('');
            void permissionResponse(sessionId, true).catch(error => appendSystemMessage(`⚠️ 权限响应失败：${errorText(error)}`));
          },
          onDeny: () => {
            setPendingAsk(null);
            setCurrentAction('');
            void permissionResponse(sessionId, false).catch(error => appendSystemMessage(`⚠️ 权限响应失败：${errorText(error)}`));
          },
        });
        break;
      case 'question_ask':
        setCurrentAction('等待回答…');
        setPendingQuestion({
          questions: event.questions,
          onSubmit: (answers: QuestionAnswer[]) => {
            setPendingQuestion(null);
            setCurrentAction('');
            void questionResponse(sessionId, answers).catch(error => appendSystemMessage(`⚠️ 提交回答失败：${errorText(error)}`));
          },
        });
        break;
      case 'done':
        controllerRef.current = null;
        setLoading(false);
        setCurrentAction('');
        if (event.token_count !== undefined) setTokenCount(event.token_count);
        break;
      case 'error':
        appendSystemMessage(`⚠️ ${event.message}`);
        setLoading(false);
        setCurrentAction('');
        break;
    }
  }, [appendSystemMessage, nextMessageId]);

  const sendContent = useCallback((content: string) => {
    if (!content.trim() || !activeSession || loading) return;
    const sessionId = activeSession;
    const streamId = ++streamIdRef.current;
    const optimisticMessageId = nextMessageId();
    lastUserMsgRef.current = content;
    setAppError(null);
    setLoading(true);
    setMessages(previous => [...previous, { id: optimisticMessageId, role: 'user', content }]);

    controllerRef.current = sendMessage(
      sessionId,
      content,
      acceptEdits,
      event => handleStreamEvent(event, sessionId, streamId, optimisticMessageId),
      error => {
        if (activeSessionRef.current !== sessionId || streamIdRef.current !== streamId) return;
        appendSystemMessage(`⚠️ ${errorText(error)}`);
        setLoading(false);
        setCurrentAction('');
      },
    );
  }, [acceptEdits, activeSession, appendSystemMessage, handleStreamEvent, loading, nextMessageId]);

  const handleSend = useCallback(() => {
    if (!input.trim()) return;
    const content = input;
    setInput('');
    sendContent(content);
  }, [input, sendContent]);

  const handleRetry = useCallback(() => {
    if (!lastUserMsgRef.current) return;
    sendContent(lastUserMsgRef.current);
  }, [sendContent]);

  const handleStop = useCallback(async () => {
    const sessionId = activeSession;
    cancelActiveStream();
    if (!sessionId) return;
    try {
      await stopGeneration(sessionId);
    } catch (error) {
      appendSystemMessage(`⚠️ 停止生成失败：${errorText(error)}`);
    }
  }, [activeSession, appendSystemMessage, cancelActiveStream]);

  const handleCompress = useCallback(async () => {
    if (!activeSession || loading) return;
    setLoading(true);
    setCurrentAction('正在压缩上下文…');
    try {
      const result = await compressSession(activeSession);
      setTokenCount(result.token_count);
      appendSystemMessage(`🧹 ${result.message}`);
    } catch (error) {
      appendSystemMessage(`⚠️ 压缩失败：${errorText(error)}`);
    } finally {
      setLoading(false);
      setCurrentAction('');
    }
  }, [activeSession, appendSystemMessage, loading]);

  const handleToggleAcceptEdits = useCallback(async (enabled: boolean) => {
    if (!activeSession) return;
    const previous = acceptEdits;
    setAcceptEdits(enabled);
    try {
      await toggleAcceptEdits(activeSession, enabled);
    } catch (error) {
      setAcceptEdits(previous);
      appendSystemMessage(`⚠️ 更新自动编辑设置失败：${errorText(error)}`);
    }
  }, [acceptEdits, activeSession, appendSystemMessage]);

  const handleDeleteSession = useCallback(async (sessionId: string) => {
    if (!activeProject) return;
    try {
      await deleteSession(sessionId);
      if (activeSessionRef.current === sessionId) {
        cancelActiveStream();
        activeSessionRef.current = null;
        setActiveSession(null);
        setMessages([]);
        setTokenCount(0);
      }
      setSessions(await listSessions(activeProject));
    } catch (error) {
      setAppError(`删除会话失败：${errorText(error)}`);
    }
  }, [activeProject, cancelActiveStream]);

  const canRetry = Boolean(!loading
    && messages.at(-1)?.role === 'system'
    && messages.at(-1)?.content.startsWith('⚠️'));

  return {
    projects,
    activeProject,
    sessions,
    activeSession,
    messages,
    input,
    setInput,
    loading,
    acceptEdits,
    showNewProject,
    setShowNewProject,
    newProjectName,
    setNewProjectName,
    showImportProjects,
    setShowImportProjects,
    importableProjects,
    importLoading,
    pendingAsk,
    pendingQuestion,
    currentAction,
    tokenCount,
    appError,
    loadSessions,
    loadSession,
    handleNewProject,
    loadImportableProjects,
    handleImportProject,
    handleNewSession,
    handleSend,
    handleRetry,
    handleStop,
    handleCompress,
    handleToggleAcceptEdits,
    handleDeleteSession,
    canRetry,
  };
}

export type ChatController = ReturnType<typeof useChat>;
