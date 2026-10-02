import { memo } from 'react';
import type { UIMessage } from 'ai';
import { Markdown } from './markdown';
import { MessageActions } from './message-actions';
import { MessageReasoning } from './message-reasoning';
import { cn } from '../lib/utils/utils';
import type { UseChatHelpers } from '@ai-sdk/react';
import { AssistantMark } from './banner';
import { ToolCall } from './tool-call';
import { LoadingSkeleton } from './tool-skeleton';
import { TOOL_LABELS } from './tool-registry';


// ─── PreviewMessage ──────────────────────────────────────────────
// Renders one UIMessage: loops over message.parts[] and renders
// the right component for each part type.

const PurePreviewMessage = ({
  chatId,
  message,
  isLoading,
  setMessages,
  onApproval,
}: {
  chatId: string;
  message: UIMessage;
  isLoading: boolean;
  setMessages: UseChatHelpers<UIMessage>['setMessages'];
  onApproval: (approvalId: string, toolCallId: string, approved: boolean) => void;
}) => {
  return (
    <div
      data-testid={`message-${message.role}`}
      className="w-full group/message animate-in fade-in duration-200"
      data-role={message.role}
    >
      <div
        className={cn(
          'flex gap-4 w-full group-data-[role=user]/message:ml-auto group-data-[role=user]/message:max-w-2xl',
        )}
      >
        {message.role === 'assistant' && (
          <AssistantMark />
        )}

        <div
          className={cn('flex flex-col gap-3 w-full min-w-0 overflow-hidden')}
        >
          {message.parts?.map((part, index) => {
            const key = `message-${message.id}-part-${index}`;

            if (part.type === 'reasoning') {
              return (
                <MessageReasoning
                  key={key}
                  text={part.text}
                  isStreaming={part.state === 'streaming'}
                />
              );
            }

            if (part.type === 'text') {
              const isLongMessage = part.text.length > 400;
              return (
                <div key={key} className={cn("flex flex-row gap-2 items-start min-w-0",
                  message.role === 'user' ? "justify-end" : "")}>
                  {message.role === 'user' ? (
                    <div className={cn(
                      "rounded-xl max-w-2xl bg-muted relative",
                      isLongMessage ? "max-h-32 overflow-y-auto" : ""
                    )}>
                      <div className="px-3 py-2">
                        <Markdown>{part.text}</Markdown>
                      </div>
                      {isLongMessage && (
                        <div className="absolute bottom-0 left-0 right-0 h-6 bg-gradient-to-t from-muted to-transparent pointer-events-none" />
                      )}
                    </div>
                  ) : (
                    <div
                      data-testid="message-content"
                      className="flex flex-col gap-4 min-w-0"
                    >
                      <div className={cn(isLoading && 'streaming-fade')}>
                        <Markdown>{part.text}</Markdown>
                      </div>
                      {message.role === 'assistant' && (
                        <div className="flex justify-end mt-2">
                          <MessageActions
                            message={message}
                            isLoading={isLoading}
                          />
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            }

            // ── Attachments ──
            // Uploads land as real files in project_dir (see /api/upload), so
            // a "source" here is just a path the server can hand back. No S3,
            // no presigning, no thumbnail pipeline — an <img> for images and a
            // chip for everything else.
            if (part.type === 'source-document') {
              const sp = part as any;
              const path: string = sp.filename ?? sp.title ?? '';
              const isImg = /\.(png|jpe?g|webp|gif)$/i.test(path);
              const href = `/api/file?path=${encodeURIComponent(path)}`;
              return (
                <div key={key} className={cn('flex', message.role === 'user' ? 'justify-end' : '')}>
                  {isImg ? (
                    <a href={href} target="_blank" rel="noopener noreferrer">
                      <img
                        src={href}
                        alt={path}
                        className="max-h-56 w-auto rounded-xl border border-border/60"
                      />
                    </a>
                  ) : (
                    <a
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center gap-2 rounded-lg border border-border/60 bg-muted/30 px-2.5 py-1.5 text-xs font-mono text-muted-foreground hover:bg-muted transition-colors"
                    >
                      <span className="opacity-60">⟐</span>
                      <span className="truncate max-w-[16rem]">{path.split('/').pop()}</span>
                    </a>
                  )}
                </div>
              );
            }


            // ── Step boundary ──
            if (part.type === 'step-start') {
              return (
                <div key={key} className="border-t border-dashed border-border/60 my-1" />
              );
            }

            if (part.type === 'data-plan') {
              return null;
            }

            // ── Dynamic tool ──
            if (part.type === 'dynamic-tool') {
              const { toolName, toolCallId, state, input } = part;
              const output = 'output' in part ? part.output : undefined;

              // Empty input-streaming — skeleton shimmer
              if (state === 'input-streaming' && !input) {
                return <LoadingSkeleton key={key} toolName={toolName} />;
              }

              // Approved: quiet line, real execution is in the next message
              if (state === 'approval-responded' && (part as any).approval?.approved) {
                const approvedLabel = TOOL_LABELS[toolName] || toolName;
                return (
                  <div key={key} className="flex items-center gap-2 py-1 text-sm font-mono text-muted-foreground">
                    <span className="w-1.5 h-1.5 rounded-full bg-green-500/60 shrink-0" />
                    <span>{approvedLabel}</span>
                    <span className="text-xs text-muted-foreground/60">approved</span>
                  </div>
                );
              }

              return (
                <ToolCall
                  key={part.toolCallId}
                  part={part}
                  onApproval={onApproval}
                />
              );
            }

            return null;
          })}
        </div>
      </div>
    </div>
  );
};

export const PreviewMessage = memo(PurePreviewMessage, (prev, next) => {
  // AI SDK keeps stable refs for unchanged messages — only the streaming
  // message gets a new object each token. Shallow compare on the big hitters.
  if (prev.message !== next.message) return false;
  if (prev.isLoading !== next.isLoading) return false;
  return true;
});

export const PendingMessage = () => {
  return (
    <div
      data-testid="message-assistant-loading"
      className="w-full group/message animate-in fade-in duration-300"
      data-role="assistant"
    >
      <div className="flex gap-4 w-full">
        <AssistantMark />

        {/* matches MessageReasoning streaming state */}
        <div className="flex flex-row gap-2 items-center pt-1">
          <div className="w-1.5 h-1.5 rounded-full bg-foreground/40 animate-pulse" />
          <span className="inline-block bg-[length:250%_100%] bg-clip-text text-transparent bg-no-repeat animate-shimmer text-sm"
            style={{
              backgroundImage: 'linear-gradient(90deg, transparent 40%, hsl(var(--background)) 50%, transparent 60%), linear-gradient(hsl(var(--muted-foreground)), hsl(var(--muted-foreground)))',
            }}
          >
            starting
          </span>
        </div>
      </div>
    </div>
  );
};
