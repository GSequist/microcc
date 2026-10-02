import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

// Everything the GUI needs to know about the running micro-cc process. Mirrors
// what the TUI status bar and pickers read out of settings_store_ / registry —
// the browser is just another view onto the same ~/.micro-cc/settings.json.
export interface Session {
  project_dir: string;
  version: string;
  model: string;
  models: string[];
  dangerous: string[];
  gateable: string[];
  tokens_budget: number;
}

export interface SlashCommand {
  name: string;
  hint: string;
  /** 'prompt' = injects a hidden turn and streams; 'action' = server-side, no
   *  model call; 'ui' = handled entirely in the browser; 'tui' = terminal-only. */
  kind: 'prompt' | 'action' | 'ui' | 'tui';
  takes_args?: boolean;
}

interface SessionCtx {
  session: Session | null;
  commands: SlashCommand[];
  /** Tips from the server's shared registry (utils/hints.py) — same copy the
   *  TUI rotates through its hint bar. */
  hints: string[];
  files: string[];
  refresh: () => Promise<void>;
  refreshFiles: () => Promise<void>;
  setModel: (model: string) => Promise<void>;
  setDangerous: (tools: string[]) => Promise<void>;
}

const Ctx = createContext<SessionCtx | null>(null);

export function useSession() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useSession must be used inside <SessionProvider>');
  return ctx;
}

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [commands, setCommands] = useState<SlashCommand[]>([]);
  const [hints, setHints] = useState<string[]>([]);
  const [files, setFiles] = useState<string[]>([]);

  const refresh = useCallback(async () => {
    const res = await fetch('/api/session');
    if (!res.ok) return;
    const data = await res.json();
    setSession(data.session);
    setCommands(data.commands);
    setHints(data.hints ?? []);
  }, []);

  // Backs the @ mention dropdown. The server walks project_dir honouring
  // .gitignore, so this is the same file universe the model's glob_ sees.
  const refreshFiles = useCallback(async () => {
    const res = await fetch('/api/files');
    if (!res.ok) return;
    setFiles((await res.json()).files ?? []);
  }, []);

  useEffect(() => {
    refresh();
    refreshFiles();
  }, [refresh, refreshFiles]);

  const setModel = useCallback(async (model: string) => {
    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    });
    setSession((s) => (s ? { ...s, model } : s));
  }, []);

  const setDangerous = useCallback(async (dangerous: string[]) => {
    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dangerous }),
    });
    setSession((s) => (s ? { ...s, dangerous } : s));
  }, []);

  const value = useMemo(
    () => ({ session, commands, hints, files, refresh, refreshFiles, setModel, setDangerous }),
    [session, commands, hints, files, refresh, refreshFiles, setModel, setDangerous]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
