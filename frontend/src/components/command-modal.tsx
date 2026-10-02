import { useEffect, useState } from 'react';
import type { UIMessage } from 'ai';
import { cn } from '../lib/utils/utils';
import { useSession } from '../contexts/SessionContext';
import { toast } from './toast';

export type ModalKind = 'model' | 'dangerous' | 'rewind' | 'files' | null;

interface RewindOption {
  index: number;
  label: string;
}

// One shell for the three pickers the TUI renders as Textual OptionLists.
// Same keyboard contract: Escape closes, click selects.
export function CommandModal({
  kind,
  onClose,
  onCleared,
  onRewound,
}: {
  kind: ModalKind;
  onClose: () => void;
  onCleared: () => void;
  onRewound: (msgs: UIMessage[]) => void;
}) {
  const { session, files, setModel, setDangerous } = useSession();
  const [rewindOptions, setRewindOptions] = useState<RewindOption[]>([]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => {
    if (kind !== 'rewind') return;
    fetch('/api/rewind')
      .then((r) => r.json())
      .then((d) => setRewindOptions(d.options ?? []))
      .catch(() => {});
  }, [kind]);

  if (!kind || !session) return null;

  const title =
    kind === 'model'
      ? 'model'
      : kind === 'dangerous'
        ? 'tools requiring approval'
        : kind === 'files'
          ? 'files in project'
          : 'rewind to turn';

  const doRewind = async (index: number) => {
    const res = await fetch('/api/rewind', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index }),
    });
    const data = await res.json();
    onRewound(data.messages ?? []);
    if (data.messages?.length === 0) onCleared();
    toast.success(`rewound — ${data.rewound_text?.slice(0, 60) ?? ''}`);
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm p-4 animate-in fade-in duration-150"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg rounded-2xl border border-border bg-background shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-2.5 border-b border-border/60 font-mono text-xs text-muted-foreground">
          {title}
        </div>

        <div className="max-h-[60vh] overflow-y-auto py-1">
          {kind === 'model' &&
            session.models.map((m) => (
              <button
                key={m}
                onClick={() => {
                  setModel(m);
                  onClose();
                }}
                className={cn(
                  'w-full text-left px-4 py-2 font-mono text-sm hover:bg-accent transition-colors flex items-center gap-2',
                  m === session.model && 'text-foreground'
                )}
              >
                <span className={cn('w-1.5 h-1.5 rounded-full shrink-0', m === session.model ? 'bg-foreground' : 'bg-transparent')} />
                {m}
              </button>
            ))}

          {kind === 'dangerous' &&
            session.gateable.map((tool) => {
              const on = session.dangerous.includes(tool);
              return (
                <button
                  key={tool}
                  onClick={() =>
                    setDangerous(
                      on ? session.dangerous.filter((d) => d !== tool) : [...session.dangerous, tool]
                    )
                  }
                  className="w-full text-left px-4 py-2 font-mono text-sm hover:bg-accent transition-colors flex items-center gap-2.5"
                >
                  <span
                    className={cn(
                      'grid place-items-center w-3.5 h-3.5 rounded border text-[9px] shrink-0',
                      on ? 'bg-foreground text-background border-foreground' : 'border-border'
                    )}
                  >
                    {on ? '✓' : ''}
                  </span>
                  <span className={on ? 'text-foreground' : 'text-muted-foreground'}>{tool}</span>
                </button>
              );
            })}

          {kind === 'rewind' &&
            (rewindOptions.length === 0 ? (
              <div className="px-4 py-6 text-center text-xs text-muted-foreground">
                nothing to rewind to yet
              </div>
            ) : (
              rewindOptions.map((opt, n) => (
                <button
                  key={opt.index}
                  onClick={() => doRewind(opt.index)}
                  className="w-full text-left px-4 py-2 font-mono text-xs hover:bg-accent transition-colors flex gap-3"
                >
                  <span className="text-muted-foreground/50 tabular-nums shrink-0">
                    {String(n + 1).padStart(2, ' ')}
                  </span>
                  <span className="truncate text-muted-foreground">{opt.label}</span>
                </button>
              ))
            ))}

          {kind === 'files' &&
            (files.length === 0 ? (
              <div className="px-4 py-6 text-center text-xs text-muted-foreground">
                no files in this project yet
              </div>
            ) : (
              files.map((path) => (
                <a
                  key={path}
                  href={`/api/file?path=${encodeURIComponent(path)}`}
                  target="_blank"
                  rel="noreferrer"
                  className="block w-full text-left px-4 py-2 font-mono text-xs hover:bg-accent transition-colors truncate text-muted-foreground hover:text-foreground"
                >
                  {path}
                </a>
              ))
            ))}
        </div>
      </div>
    </div>
  );
}
