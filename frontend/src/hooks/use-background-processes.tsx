import { useEffect, useState } from 'react';

export interface BackgroundProcess {
  pid: number;
  command: string;
  cwd: string;
  age: number;
}

// Polls the same self-scoped dict the TUI's #bgproc-status pill reads (see
// start_live_.py's _poll_bgprocs_tick) — only processes bash_ itself left
// running via `cmd &`, never the host's wider process table. A plain
// in-memory dict lookup server-side, so a 3s interval costs nothing even
// while idle.
export function useBackgroundProcesses(): BackgroundProcess[] {
  const [processes, setProcesses] = useState<BackgroundProcess[]>([]);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const res = await fetch('/api/background-status');
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (!cancelled) setProcesses(data.processes ?? []);
      } catch {
        // Transient fetch failure — next tick retries; nothing to show meanwhile.
      }
    };

    poll();
    const interval = setInterval(poll, 3000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  return processes;
}
