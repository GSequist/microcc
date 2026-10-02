import { useEffect, useState, useRef } from 'react';
import { ChevronDownIcon } from './icons';
import { cn } from '../lib/utils/utils';

interface MessageReasoningProps {
  text: string;
  isStreaming: boolean;
}

export function MessageReasoning({
  text,
  isStreaming,
}: MessageReasoningProps) {

  // v6: isStreaming comes directly from part.state === "streaming"
  // When streaming stops, state flips to "done" → isStreaming = false
  const isComplete = !isStreaming;

  // Initialize collapsed if already complete (prevents flicker on remount)
  const [isExpanded, setIsExpanded] = useState(!isComplete);
  const [thinkingTime, setThinkingTime] = useState<number | null>(null);
  const startTimeRef = useRef<number | null>(null);
  const prevCompleteRef = useRef(isComplete);

  useEffect(() => {
    if (!startTimeRef.current && !isComplete) {
      startTimeRef.current = Date.now();
    }

    // Only collapse on transition from incomplete → complete (not on remount)
    if (isComplete && !prevCompleteRef.current && startTimeRef.current) {
      setThinkingTime((Date.now() - startTimeRef.current) / 1000);
      setIsExpanded(false);
      startTimeRef.current = null;
    }
    prevCompleteRef.current = isComplete;
  }, [isComplete]);

  return (
    <div className="flex flex-col">
      {!isComplete ? (
        <div className="flex flex-row gap-2 items-center">
          <div className="w-1.5 h-1.5 rounded-full bg-foreground/40 animate-pulse" />
          <span className="inline-block bg-[length:250%_100%] bg-clip-text text-transparent bg-no-repeat animate-shimmer text-sm"
            style={{
              backgroundImage: 'linear-gradient(90deg, transparent 40%, hsl(var(--background)) 50%, transparent 60%), linear-gradient(hsl(var(--muted-foreground)), hsl(var(--muted-foreground)))',
            }}
          >
            thinking
          </span>
          <button
            data-testid="message-reasoning-toggle"
            type="button"
            className={cn(
              'transform transition-transform duration-200 text-muted-foreground',
              isExpanded ? 'rotate-180' : ''
            )}
            onClick={() => setIsExpanded(!isExpanded)}
          >
            <ChevronDownIcon />
          </button>
        </div>
      ) : (
        <div className="flex flex-row gap-2 items-center">
          <div className="w-1.5 h-1.5 rounded-full bg-foreground/60" />
          <div className="text-sm text-muted-foreground">
            {thinkingTime && thinkingTime > 0
              ? `thought for ${thinkingTime.toFixed(1)}s`
              : 'done thinking'}
          </div>
          <button
            data-testid="message-reasoning-toggle"
            type="button"
            className={cn(
              'transform transition-transform duration-200 text-muted-foreground',
              isExpanded ? 'rotate-180' : ''
            )}
            onClick={() => setIsExpanded(!isExpanded)}
          >
            <ChevronDownIcon />
          </button>
        </div>
      )}

      {/* Outer wrapper: positioning context + clips content */}
      <div className={cn(
        'transition-all duration-300 rounded-t-lg relative overflow-hidden',
        'bg-muted/10',
        isExpanded
          ? 'opacity-100 max-h-40 mt-2'
          : 'opacity-0 max-h-0 mt-0'
      )}>
        {/* Inner: scrollable content */}
        <div className="overflow-y-auto max-h-40 p-3 whitespace-pre-wrap font-mono italic text-xs text-muted-foreground">
          {text}
        </div>
        {/* Fade stays fixed at bottom of visible area */}
        <div className="absolute bottom-0 left-0 right-0 h-8 bg-gradient-to-t from-background to-transparent pointer-events-none" />
      </div>
    </div>
  );
}
