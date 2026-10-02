import { useState } from 'react';
import type { UIMessage } from 'ai';
import { PreviewMessage, PendingMessage } from './message';
import { Greeting } from './greeting';
import type { UseChatHelpers } from '@ai-sdk/react';
import { useMessages } from '../hooks/use-messages';
import { ArrowDownIcon, ChevronDownIcon } from './icons';
import { TodoDetail } from './todo-detail';
import { getCopy } from '../envUtils';
import { cn } from '../lib/utils/utils';

// No RENDER_WINDOW / Load More here, unlike the app this was lifted from: that
// one pages a Postgres-backed history over the network. micro-cc reads the
// whole transcript off local disk in one shot (msg_store_ jsonl), so windowing
// would only add a way for the rendered list to disagree with the file.

interface MessagesProps {
  chatId: string;
  status: UseChatHelpers<UIMessage>['status'];
  messages: Array<UIMessage>;
  todos: ToDos | null;
  setMessages: UseChatHelpers<UIMessage>['setMessages'];
  onApproval: (approvalId: string, toolCallId: string, approved: boolean) => void;
}

interface ToDos {
  [id: string]: {
      content: string;
      status: 'pending' | 'in_progress' | 'completed' | 'failed';
      activeForm?: string;
      tag?: string;
      notes?: string;
  };
}

function PureMessages({
  chatId,
  status,
  messages,
  todos,
  setMessages,
  onApproval,
}: MessagesProps) {
  const { containerRef, contentRef, isAtBottom, scrollToBottom } = useMessages({ chatId, status });
  const [isTodosExpanded, setTodosExpanded]=useState(false);
  const t = getCopy();

  const items  = todos ? Object.values(todos) : [];
  const active = items.find((td) => td.status === 'in_progress');

  return (
    // Outer wrapper: takes flex space in chat layout. relative creates positioning context.
    <div className="relative flex-1">
      {/* Scroll viewport: absolute inset-0 locks dimensions to parent */}
      <div
        ref={containerRef}
        className="absolute inset-0 overflow-y-auto overflow-x-hidden"
      >
        {/* Content column: centered, width-capped, stacks messages vertically.
            contentRef → use-stick-to-bottom's ResizeObserver watches this grow. */}
        <div
          ref={contentRef}
          className={`mx-auto flex min-h-full min-w-0 max-w-3xl flex-col px-4 ${
          messages.length === 0 ? 'justify-center pb-28' : 'gap-4 pt-4 pb-28'
        }`}>
          {/* plan */}
          {todos && (
            <div className="sticky top-2 z-10">
              <div className="border border-gray-400 bg-amber-500/5 rounded-xl backdrop-blur-sm overflow-hidden">
                <button
                  onClick={() => setTodosExpanded(!isTodosExpanded)}
                  className="flex items-center justify-between gap-3 w-full px-3 py-2 text-left hover:bg-accent/50 transition-colors"
                >
                  <span className="flex items-baseline gap-3 min-w-0">
                    <span className="text-xs font-semibold uppercase tracking-wide shrink-0">{t.todosTitle}</span>
                    {active?.content && (
                      <span className="text-xs text-muted-foreground truncate">{active.content}</span>
                    )}
                  </span>
                  <div className={cn('transition-transform duration-200', isTodosExpanded && 'rotate-180')}>
                    <ChevronDownIcon />
                  </div>
                </button>
                {isTodosExpanded && (
                  <TodoDetail todos={todos} />
                )}
              </div>
            </div>
          )}

          {messages.length === 0 && <Greeting />}

          {messages.map((message, index) => (
            <PreviewMessage
              key={message.id}
              chatId={chatId}
              message={message}
              isLoading={status === 'streaming' && index === messages.length - 1}
              setMessages={setMessages}
              onApproval={onApproval}
            />
          ))}

          {status === 'submitted' &&
            messages.length > 0 &&
            messages[messages.length - 1].role === 'user' && <PendingMessage />}
        </div>
      </div>

      {/* Scroll-to-bottom button */}
      {!isAtBottom && (
        <button
          onClick={scrollToBottom}
          className="absolute bottom-28 left-1/2 -translate-x-1/2 z-20 rounded-full bg-background/80 p-2 shadow-md border border-border backdrop-blur-sm hover:bg-background transition-colors"
          aria-label="Scroll to bottom"
        >
          <ArrowDownIcon/>
        </button>
      )}
    </div>
  );
}

export const Messages = PureMessages;
