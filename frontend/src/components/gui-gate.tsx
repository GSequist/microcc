import { useEffect, useState } from 'react';
import type { GateState } from '../lib/gate';

const COPY: Record<GateState, string> = {
  closed: 'GUI closed — the terminal has the session again. You can close this tab.',
  foreign: 'This session is already open in another tab.',
  denied: 'This link has expired. Run /gui in the terminal for a new one.',
};

export function GuiGate() {
  const [state, setState] = useState<GateState | null>(null);

  useEffect(() => {
    const on = (e: Event) => setState((e as CustomEvent<GateState>).detail);
    window.addEventListener('gui-gate', on);
    return () => window.removeEventListener('gui-gate', on);
  }, []);

  if (!state) return null;
  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-background/95 p-6 text-center font-mono text-sm text-muted-foreground">
      {COPY[state]}
    </div>
  );
}
