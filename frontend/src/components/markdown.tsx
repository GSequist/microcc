import React, { memo } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { CodeBlock } from './code-block';

const components: Partial<Components> = {
  code: (props: any) => {
    const { node, children, className } = props;
    // react-markdown v10 removed the inline prop.
    // Inline code = no className (no language-*) and single-line content.
    const inline = !className && typeof children === 'string' && !children.includes('\n');

    return (
      <CodeBlock
        node={node}
        inline={inline}
        className={className}
        children={children}
      />
    );
  },
  ol: ({ node, children, ...props }) => {
    return (
      <ol className="list-decimal list-outside ml-4" {...props}>
        {children}
      </ol>
    );
  },
  li: ({ node, children, ...props }) => {
    return (
      <li className="py-1" {...props}>
        {children}
      </li>
    );
  },
  ul: ({ node, children, ...props }) => {
    return (
      <ul className="list-disc list-outside ml-4" {...props}>
        {children}
      </ul>
    );
  },
  strong: ({ node, children, ...props }) => {
    return (
      <span className="font-semibold" {...props}>
        {children}
      </span>
    );
  },
  a: ({ node, children, ...props }) => {
    return (
      <a
        className="text-blue-500 hover:underline"
        target="_blank"
        rel="noreferrer"
        {...props}
      >
        {children}
      </a>
    );
  },
  h1: ({ node, children, ...props }) => {
    return (
      <h1 className="text-3xl font-semibold mt-4 mb-2" {...props}>
        {children}
      </h1>
    );
  },
  h2: ({ node, children, ...props }) => {
    return (
      <h2 className="text-2xl font-semibold mt-4 mb-2" {...props}>
        {children}
      </h2>
    );
  },
  h3: ({ node, children, ...props }) => {
    return (
      <h3 className="text-xl font-semibold mt-4 mb-2" {...props}>
        {children}
      </h3>
    );
  },
  h4: ({ node, children, ...props }) => {
    return (
      <h4 className="text-lg font-semibold mt-4 mb-2" {...props}>
        {children}
      </h4>
    );
  },
  h5: ({ node, children, ...props }) => {
    return (
      <h5 className="text-base font-semibold mt-4 mb-2" {...props}>
        {children}
      </h5>
    );
  },
  h6: ({ node, children, ...props }) => {
    return (
      <h6 className="text-sm font-semibold mt-4 mb-2" {...props}>
        {children}
      </h6>
    );
  },
  sup: ({ node, children, ...props }) => {
    return (
      <sup className="text-[10px] text-blue-400 ml-0.5" {...props}>
        {children}
      </sup>
    );
  },
  sub: ({ node, children, ...props }) => {
    return (
      <sub className="text-xs" {...props}>
        {children}
      </sub>
    );
  },
  em: ({ node, children, ...props }) => {
    return (
      <em className="italic" {...props}>
        {children}
      </em>
    );
  },
  blockquote: ({ node, children, ...props }) => {
    return (
      <blockquote
        className="relative my-3 rounded-r-lg bg-muted/40 py-2 pl-5 pr-4 italic text-muted-foreground before:absolute before:left-0 before:top-1.5 before:bottom-1.5 before:w-1 before:rounded-full before:bg-foreground/30"
        {...props}
      >
        {children}
      </blockquote>
    );
  },
  hr: ({ node, ...props }) => {
    return <hr className="my-4 border-gray-600" {...props} />;
  },
  del: ({ node, children, ...props }) => {
    return (
      <del className="line-through text-gray-400" {...props}>
        {children}
      </del>
    );
  },
  table: ({ node, children, ...props }) => {
    return (
      <div className="my-4 overflow-hidden rounded-xl border border-border">
        <div className="overflow-x-auto">
          <table
            className="w-full border-collapse text-sm [&_tr:last-child]:border-0"
            {...props}
          >
            {children}
          </table>
        </div>
      </div>
    );
  },
  thead: ({ node, children, ...props }) => {
    return (
      <thead className="bg-muted text-foreground" {...props}>
        {children}
      </thead>
    );
  },
  tbody: ({ node, children, ...props }) => {
    return <tbody {...props}>{children}</tbody>;
  },
  tr: ({ node, children, ...props }) => {
    return (
      <tr className="border-b border-border" {...props}>
        {children}
      </tr>
    );
  },
  th: ({ node, children, ...props }) => {
    return (
      <th className="px-4 py-2 text-left font-semibold" {...props}>
        {children}
      </th>
    );
  },
  td: ({ node, children, ...props }) => {
    return (
      <td className="px-4 py-2" {...props}>
        {children}
      </td>
    );
  },
};

const remarkPlugins = [remarkGfm];

const NonMemoizedMarkdown = ({ children }: { children: string }) => {
  return (
    <div className="[&_pre]:max-w-full [&_pre]:overflow-x-auto [&_table]:max-w-full">
      <ReactMarkdown remarkPlugins={remarkPlugins} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
};

export const Markdown = memo(
  NonMemoizedMarkdown,
  (prevProps, nextProps) => prevProps.children === nextProps.children,
);
