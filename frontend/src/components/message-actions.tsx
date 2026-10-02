import type { UIMessage } from 'ai';
import { memo } from 'react';
import { CopyIcon } from './icons';
import { toast } from 'sonner';
import { UseChatHelpers } from '@ai-sdk/react';

function PureMessageActions({ message, isLoading }: { message: UIMessage, isLoading: boolean }) {
  if (message.role === 'user') return null;

  if (isLoading) return null;

  const handleCopy = async () => {
    const text = message.parts
      ?.filter((p) => p.type === 'text')
      .map((p) => (p as { text: string }).text)
      .join('\n')
      .trim();

    if (!text) {
      toast.error("Nothing to copy");
      return;
    }

    await navigator.clipboard.writeText(text);
    toast.success('Copied to clipboard');
  };

  return (
    <button
      className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
      onClick={handleCopy}
      title="Copy"
    >
      <CopyIcon />
    </button>
  );
}

export const MessageActions = memo(PureMessageActions, (prev, next) => {
  if (prev.isLoading !== next.isLoading) return false;
  return prev.message.id === next.message.id;
});
