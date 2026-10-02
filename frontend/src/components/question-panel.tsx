import { useEffect, useRef, useState } from 'react';
import { cn } from '../lib/utils/utils';
import { ArrowDownIcon } from './icons';
import { Markdown } from './markdown';

// Mirrors AskQuestion in tools/ask_user_tool.py.
export interface AskOption {
  label: string;
  description?: string;
  preview?: string; // markdown, rendered alongside the list as the cursor moves onto this option
}

export interface AskQuestion {
  question: string;
  header: string;
  options: AskOption[];   // [] means freeform — the user types an answer
  multiSelect?: boolean;
}

export interface PendingQuestion {
  toolCallId: string;
  approvalId: string;
  questions: AskQuestion[];
}

/**
 * The GUI counterpart of the TUI's ask-user flow (_show_current_ask_question /
 * _advance_ask_user in start_live_.py). It takes over the composer's slot
 * rather than floating above it, for the same reason the TUI hides its prompt
 * and puts the picker there: answering IS the input right now, and the
 * transcript above must stay readable while you decide.
 *
 * Answer shapes must match the TUI's exactly — the model receives whatever we
 * build here as the tool result:
 *
 *   freeform      options: []                    → string
 *   single-choice options set, multiSelect false → the chosen label (string)
 *   multi-choice  options set, multiSelect true  → array of chosen labels
 *
 * Keyed by `header`, asked one at a time in order.
 */
export function QuestionPanel({
  pending,
  onSubmit,
  onCancel,
}: {
  pending: PendingQuestion;
  onSubmit: (answers: Record<string, string | string[]>) => void;
  onCancel: () => void;
}) {
  const [stage, setStage] = useState(0);
  const [answers, setAnswers] = useState<Record<string, string | string[]>>({});
  const [text, setText] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [cursor, setCursor] = useState(0);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const optionsRef = useRef<HTMLDivElement>(null);
  const [scrollable, setScrollable] = useState(false);

  const questions = pending.questions ?? [];
  const q = questions[stage];

  useEffect(() => {
    setText('');
    setSelected([]);
    setCursor(0);
    // Keystrokes have to land here, not in the composer that just went away.
    if (questions[stage]?.options?.length) panelRef.current?.focus();
    else textRef.current?.focus();
    // Ref is already painted with this stage's options by the time this runs.
    setScrollable(!!optionsRef.current && optionsRef.current.scrollHeight > optionsRef.current.clientHeight);
  }, [stage]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!q) return null;

  const isMulti = !!q.options?.length && !!q.multiSelect;
  const isSingle = !!q.options?.length && !q.multiSelect;
  const isFreeform = !q.options?.length;
  const hasPreview = (isSingle || isMulti) && q.options.some((o) => o.preview);
  const previewContent = hasPreview ? q.options[cursor]?.preview : undefined;

  const advance = (answer: string | string[]) => {
    const next = { ...answers, [q.header]: answer };
    setAnswers(next);
    if (stage + 1 >= questions.length) onSubmit(next);
    else setStage(stage + 1);
  };

  const toggle = (label: string) =>
    setSelected((s) => (s.includes(label) ? s.filter((x) => x !== label) : [...s, label]));

  // Same keys as the TUI's OptionList/SelectionList: arrows move, space
  // toggles in multi-select, enter accepts, escape backs out.
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!q.options?.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setCursor((c) => Math.min(c + 1, q.options.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setCursor((c) => Math.max(c - 1, 0));
    } else if (e.key === ' ' && isMulti) {
      e.preventDefault();
      toggle(q.options[cursor].label);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (isMulti) {
        if (selected.length) advance(selected);
      } else {
        advance(q.options[cursor].label);
      }
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onCancel();
    }
  };

  const lastStage = stage + 1 >= questions.length;

  return (
    <div
      ref={panelRef}
      tabIndex={-1}
      onKeyDown={onKeyDown}
      className="w-full rounded-2xl border border-border bg-background/95 backdrop-blur-xl shadow-lg outline-none overflow-hidden animate-in fade-in slide-in-from-bottom-2 duration-200"
    >
      {/* The TUI draws the question into the picker's border title; this is
          the same idea — the question labels the control, it isn't a chat
          message. */}
      <div className="flex items-start gap-3 px-4 py-2.5 border-b border-border/60">
        <span className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground/60 shrink-0">
          {q.header}
        </span>
        <span className="text-sm min-w-0">{q.question}</span>
        {(scrollable || questions.length > 1) && (
          <span className="ml-auto flex flex-col items-end gap-0.5 shrink-0">
            {questions.length > 1 && (
              <span className="font-mono text-[10px] text-muted-foreground/50 tabular-nums">
                {stage + 1}/{questions.length}
              </span>
            )}
            {scrollable && (
              <span className="flex items-center gap-1 animate-bounce">
                <ArrowDownIcon size={11} className="text-muted-foreground/50" />
                <span className="font-mono text-[10px] text-muted-foreground/50">more below</span>
              </span>
            )}
          </span>
        )}
      </div>

      {(isSingle || isMulti) && (
        <div className={cn('flex min-w-0', hasPreview && 'divide-x divide-border/60')}>
          <div
            ref={optionsRef}
            className={cn(
              'overflow-y-auto py-1',
              hasPreview ? 'w-[40%] shrink-0 max-h-[280px]' : 'w-full max-h-[220px]'
            )}
          >
            {q.options.map((opt, i) => {
              const on = selected.includes(opt.label);
              return (
                <button
                  key={opt.label}
                  onMouseEnter={() => setCursor(i)}
                  onClick={() => (isMulti ? toggle(opt.label) : advance(opt.label))}
                  className={cn(
                    'w-full text-left px-4 py-2 transition-colors flex gap-2.5',
                    i === cursor && 'bg-accent'
                  )}
                >
                  {isMulti && (
                    <span
                      className={cn(
                        'mt-0.5 grid place-items-center w-3.5 h-3.5 shrink-0 rounded border text-[9px]',
                        on ? 'bg-foreground text-background border-foreground' : 'border-border'
                      )}
                    >
                      {on ? '✓' : ''}
                    </span>
                  )}
                  <span className="min-w-0">
                    <span className="block text-sm">{opt.label}</span>
                    {opt.description && (
                      <span className="block text-xs text-muted-foreground mt-0.5 leading-relaxed">
                        {opt.description}
                      </span>
                    )}
                  </span>
                </button>
              );
            })}
          </div>
          {hasPreview && (
            <div className="flex-1 min-w-0 max-h-[280px] overflow-y-auto px-4 py-3 bg-muted/20">
              {previewContent ? (
                <div className="text-xs leading-relaxed [&_pre]:text-[11px]">
                  <Markdown>{previewContent}</Markdown>
                </div>
              ) : (
                <span className="text-xs text-muted-foreground/50">no preview for this option</span>
              )}
            </div>
          )}
        </div>
      )}

      {isFreeform && (
        <div className="px-3 pt-3">
          <textarea
            ref={textRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                if (text.trim()) advance(text.trim());
              } else if (e.key === 'Escape') {
                e.preventDefault();
                onCancel();
              }
            }}
            rows={2}
            placeholder="type your answer…"
            className="w-full resize-none rounded-xl bg-muted px-3 py-2 text-sm outline-none placeholder:text-muted-foreground/50"
          />
        </div>
      )}

      <div className="flex items-center gap-2 px-4 py-2.5">
        <span className="font-mono text-[10px] text-muted-foreground/50">
          {isMulti
            ? `space toggles · enter confirms · ${selected.length} selected`
            : isSingle
              ? '↑↓ to move · enter selects'
              : 'enter to answer · shift+enter for a newline'}
        </span>
        <span className="ml-auto flex items-center gap-2">
          <button
            onClick={onCancel}
            className="px-2.5 py-1 text-xs rounded-lg text-muted-foreground hover:bg-muted transition-colors"
          >
            Cancel
          </button>
          {(isMulti || isFreeform) && (
            <button
              onClick={() => (isMulti ? selected.length && advance(selected) : text.trim() && advance(text.trim()))}
              disabled={isMulti ? selected.length === 0 : !text.trim()}
              className="px-3 py-1 text-xs rounded-lg bg-foreground text-background hover:opacity-90 transition-opacity disabled:opacity-40"
            >
              {lastStage ? 'Answer' : 'Next'}
            </button>
          )}
        </span>
      </div>
    </div>
  );
}
