import { useEffect } from 'react';

// Wakeups the server queued for us (subagent checkpoint, watch line, schedule,
// message from another session). Offered only while idle; the server consumes it on send.
export function useIncoming(idle: boolean, send: (text: string, id: number) => void) {
  useEffect(() => {
    if (!idle) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await fetch('/api/incoming?claim=true');
        if (!res.ok || cancelled) return;
        const { id, content } = await res.json();
        if (content && !cancelled) send(content, id);
      } catch {
        // transient failure — next tick retries
      }
    };
    poll();
    const interval = setInterval(poll, 2000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [idle, send]);
}
