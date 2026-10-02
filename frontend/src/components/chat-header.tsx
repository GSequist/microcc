import { useSession } from '../contexts/SessionContext';
import { useBackgroundProcesses } from '../hooks/use-background-processes';
import { BannerMark } from './banner';
import { Tip } from './tip';
import type { ModalKind } from './command-modal';

// Deliberately not a sidebar — micro-cc's GUI is one project, one
// conversation. Everything the TUI keeps in its status bar lives on this one
// line, and clicking a segment opens the same picker its slash command does.
// Each segment carries a tooltip, since "3 gated" means nothing on its own.
export function ChatHeader({ onOpenModal }: { onOpenModal: (k: ModalKind) => void }) {
  const { session, files, refreshFiles } = useSession();
  const bgProcesses = useBackgroundProcesses();
  if (!session) return null;

  const dir = session.project_dir.replace(/^\/Users\/[^/]+/, '~');

  return (
    <header className="sticky top-0 z-20 flex items-center gap-3 px-4 py-2 font-mono text-[11px] text-muted-foreground/70 bg-background/80 backdrop-blur-xl border-b border-border/40">
      <Tip
        side="bottom"
        text={
          <>
            <strong>Project directory.</strong>{' '}
            Everything the model reads, writes and runs is relative to here, and
            this conversation's history is stored against it.
            <div className="mt-1 font-mono text-[10px] opacity-70 break-all">
              {session.project_dir}
            </div>
          </>
        }
      >
        <span className="truncate cursor-help">{dir}</span>
      </Tip>

      <span className="ml-auto flex items-center gap-3 shrink-0">
        <Tip
          text={
            <>
              <strong>Model.</strong> Click to
              switch — same picker as <code>/model</code>. Takes effect on your
              next message; the conversation so far is kept.
            </>
          }
        >
          <button onClick={() => onOpenModal('model')} className="hover:text-foreground transition-colors">
            {session.model}
          </button>
        </Tip>

        <span className="opacity-30">·</span>

        <Tip
          text={
            <>
              <strong>
                {session.dangerous.length} of {session.gateable.length} tools are gated.
              </strong>{' '}
              Gated tools stop and ask before they run, so nothing touches your
              machine without a click. Currently:{' '}
              <span className="font-mono">{session.dangerous.join(', ') || 'none'}</span>.
              Click to change — same as <code>/dangerous</code>.
            </>
          }
        >
          <button onClick={() => onOpenModal('dangerous')} className="hover:text-foreground transition-colors">
            {session.dangerous.length} gated
          </button>
        </Tip>

        <span className="opacity-30">·</span>

        <Tip
          text={
            <>
              <strong>Files.</strong> Everything in the project
              directory, including anything you've dropped in via the
              attach button — it lands in <code>uploads/</code> here.
              Click a file to open it.
            </>
          }
        >
          <button
            onClick={() => {
              refreshFiles();
              onOpenModal('files');
            }}
            className="hover:text-foreground transition-colors"
          >
            {files.length} files
          </button>
        </Tip>

        <span className="opacity-30">·</span>

        <Tip
          text={
            <>
              <strong>Rewind.</strong> Wind the
              conversation back to any earlier message and carry on from there.
              Everything after the point you pick is discarded — same as{' '}
              <code>/rewind</code>.
            </>
          }
        >
          <button onClick={() => onOpenModal('rewind')} className="hover:text-foreground transition-colors">
            rewind
          </button>
        </Tip>

        {bgProcesses.length > 0 && (
          <>
            <span className="opacity-30">·</span>
            <Tip
              text={
                <>
                  <strong>
                    {bgProcesses.length === 1 ? '1 background process' : `${bgProcesses.length} background processes`}
                  </strong>{' '}
                  — started with <code>&amp;</code> via bash_ and still running.
                  <ul className="mt-1 space-y-0.5">
                    {bgProcesses.map((p) => (
                      <li key={p.pid} className="font-mono text-[10px] opacity-70">
                        PID {p.pid}: {p.command} ({p.age}s)
                      </li>
                    ))}
                  </ul>
                </>
              }
            >
              <span className="cursor-help">
                ◇ {bgProcesses.length} {bgProcesses.length === 1 ? 'process' : 'processes'}
              </span>
            </Tip>
          </>
        )}

        <span className="opacity-30">·</span>

        <Tip text={<>Installed micro-cc version. Update from the terminal with <code>/update</code>.</>}>
          <span className="opacity-60 cursor-help">v{session.version}</span>
        </Tip>

        <span className="opacity-30">·</span>

        <Tip
          text={
            <>
              <strong>micro cc</strong> — the
              same session that's running in your terminal, just rendered here.
              Press <code>esc</code> there to close this and take the terminal
              back.
            </>
          }
        >
          <span className="cursor-help">
            <BannerMark />
          </span>
        </Tip>
      </span>
    </header>
  );
}
