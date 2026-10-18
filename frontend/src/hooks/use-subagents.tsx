import { useEffect, useState } from 'react';

export interface Subagent {
  target: string;
  name: string;
  status: string;
  summary: string;
  pending: number;
  pid: number | null;
}

// Same tracker the TUI status line reads; /api/subagents is a cheap disk poll.
export function useSubagents(): Subagent[] {
  const [subagents, setSubagents] = useState<Subagent[]>([]);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await fetch('/api/subagents');
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (!cancelled) setSubagents(data.subagents ?? []);
      } catch {
        // transient failure — next tick retries
      }
    };
    poll();
    const interval = setInterval(poll, 3000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  return subagents;
}
