import { useState } from 'react';
import { ChevronDownIcon } from './icons';
import { Markdown } from './markdown';
import { cn } from '../lib/utils/utils';
import type { Notice } from '../lib/subagent-notice';

// Compact card for subagent wake turns; parsing lives in lib/subagent-notice.

const SYMBOL: Record<string, string> = { DONE: '✓', FAILED: '✗', NEEDS_INPUT: '?', PAUSED: '⏸', UNKNOWN: '?' };
const TINT: Record<string, string> = {
  DONE: 'text-emerald-500', FAILED: 'text-red-500', NEEDS_INPUT: 'text-amber-500', PAUSED: 'text-sky-500',
};

function NoticeRow({ n }: { n: Notice }) {
  const [open, setOpen] = useState(false);
  const canOpen = Boolean(n.result);
  return (
    <div className="flex flex-col min-w-0">
      <button
        type="button"
        disabled={!canOpen}
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2 py-1 text-sm font-mono text-muted-foreground text-left min-w-0 disabled:cursor-default"
      >
        <span className="opacity-60">⚙</span>
        <span className={cn('shrink-0', TINT[n.status] ?? 'text-muted-foreground')}>{SYMBOL[n.status] ?? '?'}</span>
        <span className="text-foreground shrink-0">{n.name}</span>
        <span className="truncate">— {n.summary}</span>
        {canOpen && <ChevronDownIcon size={12} className={cn('shrink-0 transition-transform', open && 'rotate-180')} />}
      </button>
      {open && (
        <div className="ml-5 mt-1 mb-2 rounded-lg border border-border/60 bg-muted/30 px-3 py-2 text-sm min-w-0">
          <Markdown>{n.result}</Markdown>
          {(n.usage || n.transcript) && (
            <div className="mt-2 text-xs font-mono text-muted-foreground/70 break-all">
              {[n.usage, n.transcript].filter(Boolean).join(' · ')}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function SubagentNotice({ notices }: { notices: Notice[] }) {
  return (
    <div data-testid="subagent-notice" className="flex flex-col w-full min-w-0">
      {notices.map((n, i) => <NoticeRow key={i} n={n} />)}
    </div>
  );
}
