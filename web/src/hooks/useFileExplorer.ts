import { useState, useEffect, useCallback, useRef } from 'react';
import { fetchFileTree, fetchFileContent, type FileNode } from '../api/client';

export function useFileExplorer(projectId: string) {
  const [tree, setTree] = useState<FileNode | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set());
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [fileContentLoading, setFileContentLoading] = useState(false);
  const [isCollapsed, setIsCollapsed] = useState(true);
  const contentAbortRef = useRef<AbortController | null>(null);

  const loadTree = useCallback(async (pid: string, signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchFileTree(pid, signal);
      setTree(data.tree);
      const topDirs = new Set<string>();
      data.tree?.children?.forEach(c => {
        if (c.type === 'directory') topDirs.add(c.path);
      });
      setExpandedPaths(topDirs);
    } catch (err) {
      if (!signal?.aborted) setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    queueMicrotask(() => {
      if (!controller.signal.aborted) void loadTree(projectId, controller.signal);
    });
    return () => {
      controller.abort();
      contentAbortRef.current?.abort();
    };
  }, [projectId, loadTree]);

  const selectFile = useCallback(async (path: string) => {
    if (!projectId) return;
    contentAbortRef.current?.abort();
    setSelectedPath(path);
    setFileContent(null);
    setFileContentLoading(true);
    const controller = new AbortController();
    contentAbortRef.current = controller;
    try {
      const data = await fetchFileContent(projectId, path, controller.signal);
      if (!controller.signal.aborted) setFileContent(data.content);
    } catch (err) {
      if (!controller.signal.aborted) setFileContent(`⚠️ ${(err as Error).message}`);
    } finally {
      if (!controller.signal.aborted) setFileContentLoading(false);
    }
  }, [projectId]);

  const toggleExpanded = useCallback((path: string) => {
    setExpandedPaths(prev => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const toggleCollapsed = useCallback(() => setIsCollapsed(p => !p), []);
  const closePreview = useCallback(() => {
    contentAbortRef.current?.abort();
    setSelectedPath(null);
    setFileContent(null);
    setFileContentLoading(false);
  }, []);

  return {
    tree, loading, error, loadTree,
    expandedPaths, toggleExpanded,
    selectedPath, fileContent, fileContentLoading, selectFile, closePreview,
    isCollapsed, toggleCollapsed,
  };
}
