import { Component, ReactNode } from 'react';

export class ErrorBoundary extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: Error) { return { error: e.message }; }
  render() {
    if (this.state.error) return (
      <div className="flex items-center justify-center h-full text-gray-400">
        <div className="text-center"><div className="text-4xl mb-2">⚠</div><div className="text-sm">出错了</div><div className="text-xs text-gray-300 mt-1">{this.state.error}</div></div>
      </div>
    );
    return this.props.children;
  }
}
