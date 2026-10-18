import type { Subagent } from '../hooks/use-subagents';
import type { BackgroundProcess } from '../hooks/use-background-processes';

// One muted line above the input, next to the todo tracker: what the session
// has running in the background (subagents it spawned, `cmd &` processes).
export function BackgroundLine({ subagents, processes }: { subagents: Subagent[]; processes: BackgroundProcess[] }) {
  if (!subagents.length && !processes.length) return null;

  const parts = [
    ...subagents.map((a) => `${a.name} ${a.status.toLowerCase().replace('_', ' ')}${a.pending ? ` (${a.pending} unread)` : ''}`),
    ...processes.map((p) => `pid ${p.pid} ${p.command}`),
  ];
  const title = [
    ...subagents.map((a) => `${a.name}: ${a.status}${a.summary ? ` — ${a.summary}` : ''}`),
    ...processes.map((p) => `PID ${p.pid} (${p.age}s): ${p.command}`),
  ].join('\n');
  const running = subagents.filter((a) => a.status === 'RUNNING').length + processes.length;

  return (
    <div className="flex flex-row gap-2 items-center pl-5 pb-1 text-sm text-muted-foreground min-w-0" title={title}>
      <span className={running ? 'inline-block animate-spin' : 'inline-block'}>◇</span>
      <span className="truncate">{parts.join(' · ')}</span>
    </div>
  );
}
