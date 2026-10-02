import { useState, memo, useCallback } from 'react';
import { CopyIcon, CheckIcon } from './icons';
import { Button } from './ui/button';

interface CodeBlockProps {
  node: any;
  inline: boolean;
  className: string;
  children: any;
}

function PureCodeBlock({
  node,
  inline,
  className,
  children,
  ...props
}: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(() => {
    const code = String(children).replace(/\n$/, '');
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [children]);

  if (!inline) {
    return (
      <div className="not-prose flex flex-col relative group min-w-0">
        <Button
          onClick={handleCopy}
          variant="ghost"
          size="sm"
          className="absolute top-2 right-2 h-8 w-8 p-0 opacity-0 group-hover:opacity-100 transition-opacity bg-zinc-700/50 hover:bg-zinc-700 text-zinc-100"
        >
          {copied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
        </Button>
        <pre
          {...props}
          className={`font-mono text-sm w-full overflow-x-auto dark:bg-zinc-900 p-3 border border-zinc-200 dark:border-zinc-700 rounded-md dark:text-zinc-50 text-zinc-900`}
        >
          <code className="whitespace-pre">{children}</code>
        </pre>
      </div>
    );
  } else {
    return (
      <code
        className={`${className} font-mono text-sm bg-zinc-100 dark:bg-zinc-800 py-0.5 px-1 rounded-md`}
        {...props}
      >
        {children}
      </code>
    );
  }
}

// CRITICAL FIX: Memoize to prevent re-render when parent markdown updates
export const CodeBlock = memo(PureCodeBlock, (prevProps, nextProps) => {
  if (prevProps.inline !== nextProps.inline) return false;
  if (prevProps.className !== nextProps.className) return false;
  if (prevProps.children !== nextProps.children) return false;
  return true;
});
