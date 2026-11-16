import { useEffect, useState } from 'react';
import type { GateState } from '../lib/gate';

const COPY: Record<Exclude<GateState, 'ok'>, string> = {
  closed: 'GUI closed — the terminal has the session again. You can close this tab.',
  foreign: 'This session is open in another tab — this one takes over once that tab closes or goes quiet.',
  denied: 'This link has expired. Run /gui in the terminal for a new one.',
};

export function GuiGate() {
  const [state, setState] = useState<GateState | null>(null);

  useEffect(() => {
    const on = (e: Event) => {
      const next = (e as CustomEvent<GateState>).detail;
      setState((prev) => (prev === 'denied' ? prev : next === 'ok' ? null : next));
    };
    window.addEventListener('gui-gate', on);
    return () => window.removeEventListener('gui-gate', on);
  }, []);

  if (!state || state === 'ok') return null;
  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-background/95 p-6 text-center font-mono text-sm text-muted-foreground">
      {COPY[state]}
    </div>
  );
}
