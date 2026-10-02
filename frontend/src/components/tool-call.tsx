import type { DynamicToolUIPart } from 'ai';
import { LoadingSkeleton } from './tool-skeleton';
import { cn } from '../lib/utils/utils';
import { useState, useEffect } from 'react';
import { TOOL_ICONS, TOOL_LABELS } from './tool-registry'
import {
  TerminalIcon,
  ChevronDownIcon
} from './icons';
import { Markdown } from './markdown';
import { getCopy } from '../envUtils';
import { Tip } from './tip';

// ── State indicator icon ─────────────────────────────────────────

const SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⠏⠋⠙⠚⠞⠖⠦⠴⠲⠳';

function useSpinner(active: boolean, fps = 12) {
  const [i, setI] = useState(0);
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setI(n => (n + 1) % SPINNER.length), 1000 / fps);
    return () => clearInterval(id);
  }, [active, fps]);
  return active ? SPINNER[i] : '';
}

// Single glyph slot: braille spinner while in-flight, otherwise the real
// tool icon tinted by state. Never both at once (that's the comical part).
function StateIcon({ state, approved, preliminary, Icon }: { state: string; approved?: boolean; preliminary: boolean; Icon: any }) {
  const isActive = preliminary || state === 'input-streaming' || state === 'input-available';
  const frame = useSpinner(isActive, 8);

  if (isActive) {
    return <span className="inline-flex h-3.5 w-4 shrink-0 items-center justify-center text-sm font-bold leading-none text-muted-foreground">{frame}</span>;
  }

  const isBad = state === 'output-error' || state === 'output-denied' || (state === 'approval-responded' && approved === false);
  const isWarn = state === 'approval-requested';
  return (
    <Icon
      size={14}
      className={cn('shrink-0', isBad ? 'text-red-500/70' : isWarn ? 'text-amber-500' : 'text-muted-foreground')}
    />
  );
}


// ─── ToolCall ────────────────────────────────────────────────────
// Renders a single dynamic-tool part. Handles all states:
//   input-streaming  → shimmer (args arriving, partially parsed input)
//   input-available  → args complete, about to execute
//   approval-requested → args + Approve/Deny buttons
//   approval-responded → briefly shows status before output arrives
//   output-available + preliminary → progress bar
//   output-available → final result
//   output-denied    → "Denied by user"
//   output-error     → error message

interface ToolCallProps {
  part: DynamicToolUIPart;
  onApproval: (approvalId: string, toolCallId: string, approved: boolean) => void;
}


export function ToolCall({ part, onApproval }: ToolCallProps) {

  const { toolName, toolCallId, state, input } = part;
  const preliminary = state === 'output-available' ? (part as any).preliminary : false;
  const output = 'output' in part ? part.output : undefined;
  const outputStr = typeof output === 'string' ? output : '';
  const t = getCopy();

  // Screenshots (browser_/computer_use) come back as a path the server can
  // serve out of project_dir — worth showing inline rather than as a path
  // string. Everything else falls through to the pre/markdown block.
  const isImage = /\.(png|jpe?g|webp|gif)\s*$/i.test(outputStr.trim());

  /// global tool expand
  const [isExpanded, setIsExpanded] = useState(false);

  // Auto-expand when visual output arrives
  useEffect(() => {
    if (state === 'output-available' && !preliminary && isImage) {
      setIsExpanded(true);
    }
  }, [state, preliminary, isImage]);

  const Icon = TOOL_ICONS[toolName] || TerminalIcon;
  const label = TOOL_LABELS[toolName] || toolName;

  let inputStr = '';
  try { inputStr = input != null ? JSON.stringify(input, null, 2) : ''; } catch { }

  // Partial input that failed to stringify during streaming — show skeleton
  if (state === 'input-streaming' && !inputStr) {
    return <LoadingSkeleton toolName={toolName} />;
  }

  // ── State flags ──
  const isStreamingArgs = state === 'input-streaming';
  const isWaiting = state === 'input-available';
  const isApprovalRequested = state === 'approval-requested';
  const isApprovalResponded = state === 'approval-responded';
  const isDenied =
    state === 'output-denied' ||
    (isApprovalResponded && part.approval?.approved === false);
  const isError = state === 'output-error';
  const isProgress = state === 'output-available' && preliminary;
  const isComplete = state === 'output-available' && !preliminary;

  // One-line summary for collapsed header
  const headerSummary = isStreamingArgs
    ? inputStr.slice(0, 60) + (inputStr.length > 60 ? '…' : '')
    : isProgress && output && typeof output === 'object'
      ? (output as any).progress || ''
      : '';

  const isActive = isStreamingArgs || isWaiting || isProgress;
  const pct = isProgress && output && typeof output === 'object' ? (output as any).percentage ?? null : null;
  // Input only matters before completion (the Input block is hidden once
  // complete — see below), and a null output (non-user-facing tool) must not
  // make the chip expandable. So once complete, expand depends on real output.
  const hasExpandable = (!!inputStr && !isComplete) || (isComplete && output != null) || isError;

  return (
    <div className="w-full text-sm font-mono">
      {/* Header — a borderless log line, not a card */}
      <div className="flex items-center gap-2 w-full">
        <button
          onClick={() => setIsExpanded(!isExpanded)}
          className="flex items-center gap-2 min-w-0 flex-1 text-left py-1 hover:opacity-80 transition-opacity"
          title={`${label} — click to see the arguments and result`}
        >
          <StateIcon state={state} approved={part.approval?.approved} preliminary={preliminary} Icon={Icon} />

          {/* Label — shimmers only while in-flight */}
          <span
            className={cn(
              'shrink-0',
              isActive
                ? 'inline-block bg-[length:250%_100%] bg-clip-text text-transparent bg-no-repeat animate-shimmer'
                : 'text-muted-foreground'
            )}
            style={isActive ? { backgroundImage: 'linear-gradient(90deg, transparent 40%, hsl(var(--background)) 50%, transparent 60%), linear-gradient(hsl(var(--muted-foreground)), hsl(var(--muted-foreground)))' } : undefined}
          >
            {label}
          </span>

          {/* Live readout — stays still and legible */}
          {headerSummary && (
            <span className="text-xs text-muted-foreground/70 truncate min-w-0">{headerSummary}</span>
          )}
          {pct != null && (
            <span className="text-xs text-muted-foreground shrink-0 tabular-nums">{pct}%</span>
          )}
        </button>

        {hasExpandable && (
          <button
            onClick={() => setIsExpanded(!isExpanded)}
            className={cn('shrink-0 text-muted-foreground transition-transform duration-200', isExpanded && 'rotate-180')}
          >
            <ChevronDownIcon />
          </button>
        )}
      </div>

      {/* Progress — hairline baseline fill, no pill */}
      {isProgress && pct != null && (
        <div className="mt-1 h-px w-full bg-border/50 overflow-hidden">
          <div className="h-full bg-muted-foreground/60 transition-all duration-500 ease-out" style={{ width: `${pct}%` }} />
        </div>
      )}

      {/* Expanded detail — the only place that earns a real container */}
      {isExpanded && hasExpandable && (
        <div className="mt-2 rounded-xl border border-border/60 bg-muted/20 px-3 py-2.5 space-y-3 overflow-hidden">
          {/* Input args — only before output arrives */}
          {inputStr && !isComplete && (
            <div>
              <div className="text-xs text-muted-foreground mb-1">Input:</div>
              <pre className="text-xs whitespace-pre-wrap break-all max-h-48 overflow-y-auto">
                {inputStr}
              </pre>
            </div>
          )}

          {/* Output — final only */}
          {isComplete && output != null && (
            isImage ? (
              <img
                src={`/api/file?path=${encodeURIComponent(outputStr.trim())}`}
                alt={toolName}
                className="max-h-96 w-auto rounded-lg border border-border/60"
              />
            ) : (
              <div className="max-h-64 overflow-y-auto">
                {typeof output === 'object' && (output as any)?.markdown ? (
                  <div className="text-xs text-muted-foreground [&_h1]:text-xs [&_h2]:text-xs [&_h3]:text-xs [&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-medium [&_h1]:mt-0 [&_h2]:mt-0 [&_h3]:mt-0">
                    <Markdown>{(output as any).markdown}</Markdown>
                  </div>
                ) : (
                  <pre className="text-xs whitespace-pre-wrap break-all text-muted-foreground">
                    {typeof output === 'string' ? output : JSON.stringify(output, null, 2)}
                  </pre>
                )}
              </div>
            )
          )}

          {/* Error */}
          {isError && 'errorText' in part && (
            <div>
              <div className="text-xs text-red-500 mb-1">Error:</div>
              <pre className="text-xs whitespace-pre-wrap break-all text-red-400">
                {part.errorText}
              </pre>
            </div>
          )}
        </div>
      )}

      {/* Approval — rounded-xl, monochrome buttons (no red/green clash).
          ask_user_question_tool_ is excluded: it also arrives as an approval
          request (that's what arms the SDK's resume) but the real interaction
          is the question modal, and a stray Approve/Deny under it would both
          confuse and, if clicked, answer the model with a bare boolean. */}
      {isApprovalRequested && part.approval && toolName !== 'ask_user_question_tool_' && (
        <div className="mt-2 rounded-xl border border-amber-500/30 bg-amber-500/5 px-3 py-2.5 flex items-center gap-3">
          <div className="flex-1 flex flex-col gap-0.5">
            <span className="text-xs font-extralight">
              {t.tool_approval_ask} <strong className="font-medium text-foreground">{label}</strong>?
            </span>
            <Tip
              side="top"
              text={
                <>This tool is gated, so it pauses here instead of running. Expand the row above to see exactly what it would do. Change which tools are gated in the header, or with <code>/dangerous</code>.</>
              }
            >
              <span className="text-[10px] leading-tight text-muted-foreground/70 cursor-help underline decoration-dotted underline-offset-2">
                {t.tool_approval_hint}
              </span>
            </Tip>
          </div>
          <Tip side="top" text="Don't run it. The model is told you declined and asks what to do instead.">
            <button
              onClick={() => onApproval(part.approval!.id, toolCallId, false)}
              className="px-2.5 py-1 text-xs rounded-lg text-muted-foreground hover:bg-muted transition-colors"
            >
              Deny
            </button>
          </Tip>
          <Tip side="top" text="Run it once, now. This doesn't un-gate the tool — you'll be asked again next time.">
            <button
              onClick={() => onApproval(part.approval!.id, toolCallId, true)}
              className="px-2.5 py-1 text-xs rounded-lg bg-foreground text-background hover:opacity-90 transition-opacity"
            >
              Approve
            </button>
          </Tip>
        </div>
      )}

      {/* Denied */}
      {isDenied && (
        <div className="mt-1 pl-6 text-xs text-red-500/70">Denied</div>
      )}
    </div>
  );
}

