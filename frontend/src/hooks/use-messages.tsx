import { useCallback, useEffect } from 'react';
import type { UIMessage } from 'ai';
import type { UseChatHelpers } from '@ai-sdk/react';
import { useStickToBottom } from 'use-stick-to-bottom';

// Scroll behavior is delegated to use-stick-to-bottom (StackBlitz — the same
// library Vercel's AI Elements wraps). It replaces our hand-rolled
// IntersectionObserver + ResizeObserver + rAF easing + wheel/touch listeners:
//
// - User-escape detection: instead of listening for gestures, it compares the
//   scrollTop it *expects* (from its own animation) against the scrollTop that
//   *actually* happens. Any mismatch must be the user — covers wheel, touch,
//   scrollbar drag, and keyboard (PgUp/space), which our gesture listeners missed.
// - Smooth follow: velocity-based spring (damping/stiffness/mass), so it speeds
//   up under bursty token streams and settles softly, instead of trailing at a
//   fixed fraction per frame like our EASE=0.3 loop.
// - Re-arms stickiness when the user scrolls back down to the bottom.
// ┌────────────────┬─────────────────────────────────┬──────────────────────────────────────────────────┐
// │      Knob      │          Mechanically           │             Turning it up feels like             │
// ├────────────────┼─────────────────────────────────┼──────────────────────────────────────────────────┤
// │ stiffness      │ how strongly the gap pulls      │ snappier catch-up, less trail                    │
// │ (0.05)         │ velocity                        │                                                  │
// ├────────────────┼─────────────────────────────────┼──────────────────────────────────────────────────┤
// │ damping (0.7)  │ how much previous velocity      │ more glide/inertia (note: opposite of classic    │
// │                │ carries over                    │ spring "damping")                                │
// ├────────────────┼─────────────────────────────────┼──────────────────────────────────────────────────┤
// │ mass (1.25)    │ divides the whole thing         │ heavier, slower, more deliberate                 │
// └────────────────┴─────────────────────────────────┴──────────────────────────────────────────────────┘
export function useMessages({
  chatId,
  status,
}: {
  chatId: string;
  status: UseChatHelpers<UIMessage>['status'];
}) {
  const { scrollRef, contentRef, isAtBottom, scrollToBottom } = useStickToBottom({
    // First render of a conversation: jump, don't animate through history.
    initial: 'instant',
    // resize: { mass: 1.5 },
  });

  // Chat switch — snap to bottom instantly
  useEffect(() => {
    if (chatId) scrollToBottom('instant');
  }, [chatId]); // eslint-disable-line react-hooks/exhaustive-deps

  // On submit — snap to bottom (also re-arms stickiness for the reply stream)
  useEffect(() => {
    if (status === 'submitted') scrollToBottom('instant');
  }, [status]); // eslint-disable-line react-hooks/exhaustive-deps

  // Wrap so consumers can pass it straight to onClick without leaking the
  // MouseEvent into ScrollToBottomOptions.
  const scrollToBottomSmooth = useCallback(() => {
    scrollToBottom();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return {
    containerRef: scrollRef,  // goes on the overflow-y-auto viewport
    contentRef,               // goes on the growing content column
    isAtBottom,
    scrollToBottom: scrollToBottomSmooth,
  };
}
