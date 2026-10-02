import type { ReactNode } from 'react';
import { Tooltip, TooltipContent, TooltipTrigger } from './ui/tooltip';

/**
 * Explanatory tooltip. Wraps Radix so call sites stay one line:
 *
 *   <Tip text="what this does">{trigger}</Tip>
 *
 * `asChild` means the trigger renders as the child element rather than
 * wrapping it in another button — important on the header, where the triggers
 * are already buttons and nesting them would be invalid HTML.
 */
export function Tip({
  text,
  children,
  side = 'bottom',
}: {
  text: ReactNode;
  children: ReactNode;
  side?: 'top' | 'bottom' | 'left' | 'right';
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent
        side={side}
        // Quieter than the shadcn default (text-sm, full-contrast foreground):
        // these explain chrome, so they should read as a footnote rather than
        // compete with the transcript. cn() lets these win the merge.
        // font-sans is explicit, not decorative: Radix renders Content inline
        // rather than through a Portal, so a tooltip triggered from the
        // header would otherwise inherit its font-mono. Prose reads as prose
        // wherever the trigger happens to live; only <code> stays mono.
        className="max-w-[17rem] rounded-lg border-border/60 px-2.5 py-1.5 font-sans text-[11px] font-normal leading-snug tracking-normal text-muted-foreground [&_strong]:font-normal [&_strong]:text-foreground [&_code]:font-mono [&_code]:text-[10px] [&_code]:text-foreground/80"
      >
        {text}
      </TooltipContent>
    </Tooltip>
  );
}
