import type { UIMessage } from 'ai';
import cx from 'classnames';
import type React from 'react';
import {
  useRef,
  useEffect,
  useState,
  useCallback,
  type ChangeEvent,
  memo,
} from 'react';
import { toast } from './toast';
import { useLocalStorage, useWindowSize } from 'usehooks-ts';
import { ArrowUpIcon, PaperclipIcon, StopIcon, UploadIcon } from './icons';
import { Button } from './ui/button';
import { Textarea } from './ui/textarea';
import type { UseChatHelpers } from '@ai-sdk/react';
import { getCopy } from '../envUtils';
import { useSession, type SlashCommand } from '../contexts/SessionContext';
import { Tip } from './tip';
import { useBannerColor } from './banner';

// Uploads land in project_dir, so anything the model's read_/vision tools can
const ALLOWED_EXTENSIONS = ['.pdf', '.txt', '.docx', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.xls', '.xlsx', '.csv', '.html', '.md', '.pptx', '.odt', '.eml', '.json', '.xml', '.yaml', '.yml', '.sql', '.log', '.zip', '.py', '.tsx', '.ts', '.js', '.jsx', '.go', '.rs', '.sh', '.toml', '.ini', '.env'];

const validateFileExtension = (filename: string): boolean => {
  const extension = filename.toLowerCase().substring(filename.lastIndexOf('.'));
  return ALLOWED_EXTENSIONS.includes(extension);
};

const AT_MENTION_MAX_RESULTS = 50;

// Ranks @ dropdown candidates so an obvious match (typing "config" for
// src/config.py) doesn't get buried under every unrelated path that happens
// to contain the substring somewhere in a directory name. Three tiers —
// basename starts with query, basename contains query, full path contains
// query — each shallow-path-first. Mirrors rank_file_matches in
// utils/project_files_.py (the TUI's equivalent picker).
const rankFileMatches = (files: string[], query: string, limit = AT_MENTION_MAX_RESULTS): string[] => {
  const queryLc = query.toLowerCase();
  const depthThenName = (a: string, b: string) =>
    a.split('/').length - b.split('/').length || a.localeCompare(b);

  if (!queryLc) return [...files].sort(depthThenName).slice(0, limit);

  const tiers: string[][] = [[], [], []];
  for (const f of files) {
    const base = (f.split('/').pop() ?? f).toLowerCase();
    if (base.startsWith(queryLc)) tiers[0].push(f);
    else if (base.includes(queryLc)) tiers[1].push(f);
    else if (f.toLowerCase().includes(queryLc)) tiers[2].push(f);
  }
  return tiers.flatMap((t) => [...t].sort(depthThenName)).slice(0, limit);
};

function PureMultimodalInput({
  chatId,
  sendMessage,
  status,
  stop,
  messages,
  setMessages,
  onCommand,
}: {
  chatId: string;
  sendMessage: UseChatHelpers<UIMessage>['sendMessage'];
  status: UseChatHelpers<UIMessage>['status'];
  stop: () => Promise<void>;
  messages: Array<UIMessage>;
  setMessages: UseChatHelpers<UIMessage>['setMessages'];
  onCommand: (name: string, args: string) => void;
}) {
  const [input, setInput] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { width } = useWindowSize();
  const t = getCopy();

  const hasLineBreaks = input.includes('\n');
  const isTextWrapping = textareaRef.current && textareaRef.current.scrollHeight > 80;
  const shouldExpand = hasLineBreaks || isTextWrapping;

  const { files, refreshFiles, commands, hints } = useSession();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploadQueue, setUploadQueue] = useState<Array<string>>([]);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [filesUploadedInThisMessage, setFilesUploadedInThisMessage] = useState<Array<string>>([]);

  const [isDragOver, setIsDragOver] = useState(false);

  const [isFileDropdownOpen, setIsFileDropdownOpen] = useState(false);
  const [atSymbolPosition, setAtSymbolPosition] = useState<number | null>(null);
  const [fileSearchQuery, setFileSearchQuery] = useState('');
  const [selectedFileIndex, setSelectedFileIndex] = useState(0);

  // Slash palette rides the exact same rail as the @ dropdown below: one
  // highlighted index, arrows to move, Tab/Enter to accept, Escape to dismiss.
  const [selectedCmdIndex, setSelectedCmdIndex] = useState(0);

  const [placeholderHint, setPlaceholderHint] = useState<string | null>(null);
  // Same cycling colour as the banner/wordmark — the ⌖ tips read as
  // "micro-cc talking", not generic muted placeholder text.
  const accent = useBannerColor();
  const isHintPlaceholder = placeholderHint?.startsWith('⌖') ?? false;

  // Long pastes collapse to a token so the box stays readable; submitForm
  // expands them back before sending. Same trick as the TUI's ⟪paste:N⟫.
  const [pastes, setPastes] = useState<Record<string, { full: string; count: number }>>({});
  const [pendingPaste, setPendingPaste] = useState<{ id: string; full: string; count: number } | null>(null);
  const hintTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [localStorageInput, setLocalStorageInput] = useLocalStorage('input', '');

  // ── Slash palette state ──
  const cmdQuery = /^\/[\w-]*$/.test(input) ? input : null;
  const filteredCmds: SlashCommand[] =
    cmdQuery === null ? [] : commands.filter((c) => c.name.startsWith(cmdQuery));
  const isCmdDropdownOpen = filteredCmds.length > 0;

  useEffect(() => {
    setSelectedCmdIndex(0);
  }, [cmdQuery]);

  useEffect(() => {
    if (textareaRef.current) adjustHeight();
  }, []);

  // Show a random tip 4s after a response finishes, drop it after 9s. Tips
  // come from the server's shared registry (utils/hints.py), so the terminal
  // and the browser teach the same things.
  useEffect(() => {
    if (status === 'streaming') {
      setPlaceholderHint(t.streamingMultiModalPlaceholder);
      return;
    }

    if (messages.length > 0 && status === 'ready') {
      setPlaceholderHint(null);
      const showId = setTimeout(() => {
        if (hints.length > 0) setPlaceholderHint(`⌖ ${hints[Math.floor(Math.random() * hints.length)]}`);
      }, 4000);
      const hideId = setTimeout(() => setPlaceholderHint(null), 9000);
      return () => {
        clearTimeout(showId);
        clearTimeout(hideId);
      };
    }
    setPlaceholderHint(null);
  }, [messages.length, status, hints]);

  useEffect(() => {
    if (textareaRef.current) {
      const domValue = textareaRef.current.value;
      setInput(domValue || localStorageInput || '');
      adjustHeight();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const timeoutId = setTimeout(() => setLocalStorageInput(input), 500);
    return () => clearTimeout(timeoutId);
  }, [input]);

  const adjustHeight = () => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${textareaRef.current.scrollHeight + 2}px`;
    }
  };

  const resetHeight = () => {
    if (textareaRef.current) textareaRef.current.style.height = 'auto';
  };

  const filteredFiles = rankFileMatches(files || [], fileSearchQuery || '');

  const handleInput = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    const value = event.target.value;
    const cursorPos = event.target.selectionStart;
    setInput(value);
    adjustHeight();

    let dropdownShouldBeOpen = isFileDropdownOpen;
    let currentAtPosition = atSymbolPosition;

    if (value[cursorPos - 1] === '@') {
      dropdownShouldBeOpen = true;
      currentAtPosition = cursorPos;
      setIsFileDropdownOpen(true);
      setAtSymbolPosition(cursorPos);
      setFileSearchQuery('');
      setSelectedFileIndex(0);
    }

    if (dropdownShouldBeOpen && currentAtPosition !== null) {
      const textAfterAt = value.slice(currentAtPosition, cursorPos);
      setFileSearchQuery(textAfterAt);

      const textFromAt = value.slice(currentAtPosition - 1);
      if (!textFromAt.startsWith('@') || textFromAt.includes(' ')) {
        setIsFileDropdownOpen(false);
      }
    }
  };

  const handleKeyDown = async (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (isCmdDropdownOpen) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        setSelectedCmdIndex((prev) => Math.min(prev + 1, filteredCmds.length - 1));
        return;
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        setSelectedCmdIndex((prev) => Math.max(prev - 1, 0));
        return;
      }
      if (event.key === 'Tab') {
        event.preventDefault();
        setInput(filteredCmds[selectedCmdIndex].name + ' ');
        return;
      }
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        runCommand(filteredCmds[selectedCmdIndex].name, '');
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        setInput('');
        return;
      }
    }

    if (isFileDropdownOpen && filteredFiles.length > 0) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        setSelectedFileIndex((prev) => Math.min(prev + 1, filteredFiles.length - 1));
        return;
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        setSelectedFileIndex((prev) => Math.max(prev - 1, 0));
        return;
      }
      if (event.key === 'Tab' || event.key === 'Enter') {
        event.preventDefault();
        insertFileName(filteredFiles[selectedFileIndex]);
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        setIsFileDropdownOpen(false);
        return;
      }
    }

    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();

      if (uploadQueue.length > 0) {
        toast.warning(t.submitOnUploadWarning);
        return;
      }

      if (status !== 'ready') {
        if (input.trim()) {
          await stop();
          submitForm();
        }
      } else {
        submitForm();
      }
    }
  };

  const runCommand = (name: string, args: string) => {
    setInput('');
    setLocalStorageInput('');
    resetHeight();
    onCommand(name, args);
  };

  const insertFileName = (fileName: string) => {
    if (atSymbolPosition === null || !fileName) return;

    const beforeAt = input.slice(0, atSymbolPosition - 1);
    const cursorPos = textareaRef.current?.selectionStart ?? input.length;
    const afterQuery = input.slice(cursorPos);

    setInput(`${beforeAt}${fileName}${afterQuery}`);
    setIsFileDropdownOpen(false);
    setAtSymbolPosition(null);
    setFileSearchQuery('');
    setSelectedFileIndex(0);

    setTimeout(() => {
      const newCursorPos = beforeAt.length + fileName.length;
      textareaRef.current?.setSelectionRange(newCursorPos, newCursorPos);
      textareaRef.current?.focus();
    }, 0);
  };

  const nextId = () => crypto.randomUUID().slice(4);

  const insertAtCursor = (text: string) => {
    const el = textareaRef.current;
    if (!el) return;
    const cursor = el.selectionStart;
    const before = input.slice(0, cursor);
    const after = input.slice(cursor);
    setInput(before + text + after);
    setTimeout(() => {
      const pos = before.length + text.length;
      el.setSelectionRange(pos, pos);
    }, 0);
    adjustHeight();
  };

  const uploadFile = useCallback(async (file: File) => {
    const body = new FormData();
    body.append('file', file);
    setUploadProgress(15);
    try {
      const res = await fetch('/api/upload', { method: 'POST', body });
      setUploadProgress(90);
      if (!res.ok) return { success: false as const, error: await res.text() };
      const data = await res.json();
      await refreshFiles();
      return { success: true as const, path: data.path as string };
    } catch (e) {
      return { success: false as const, error: String(e) };
    } finally {
      setUploadProgress(0);
    }
  }, [refreshFiles]);

  const acceptFile = useCallback(async (file: File) => {
    if (status !== 'ready') {
      toast.error(t.modelBusy);
      return;
    }
    if (!validateFileExtension(file.name)) {
      toast.error(`${t.fileTypeNotSupportedPrefix} ${ALLOWED_EXTENSIONS.join(', ')}`);
      return;
    }

    setUploadQueue([file.name]);
    try {
      const result = await uploadFile(file);
      if (result.success) {
        toast.success(`"${file.name}" ${t.fileUploadedSuffix}`);
        setFilesUploadedInThisMessage((prev) => [...prev, result.path]);
      } else {
        toast.error(`${t.uploadFailedPrefix} "${file.name}": ${result.error}`);
      }
    } catch (error) {
      console.error('Error uploading file!', error);
      toast.error(t.fileUploadFailed);
    } finally {
      setUploadQueue([]);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  }, [status, uploadFile, t]);

  const handleOnPaste = async (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const clipboardFiles = event.clipboardData.files;
    if (clipboardFiles && clipboardFiles.length) {
      event.preventDefault();
      await acceptFile(clipboardFiles[0]);
      return;
    }

    const text = event.clipboardData.getData('text');
    if (text.length <= 50) return;
    event.preventDefault();

    // Second Ctrl+V within 3s expands the token back to the full text.
    if (pendingPaste && pendingPaste.full === text) {
      const tokenStr = `<pasted #${pendingPaste.id} | ${pendingPaste.count} chars>`;
      setInput((prev) => prev.replace(tokenStr, text));
      setPastes((prev) => {
        const next = { ...prev };
        delete next[pendingPaste.id];
        return next;
      });
      setPendingPaste(null);
      adjustHeight();
      return;
    }

    const id = nextId();
    insertAtCursor(`<pasted #${id} | ${text.length} chars>`);
    setPendingPaste({ id, full: text, count: text.length });
    setPastes((prev) => ({ ...prev, [id]: { full: text, count: text.length } }));

    clearTimeout(hintTimerRef.current ?? undefined);
    hintTimerRef.current = setTimeout(() => setPendingPaste(null), 3000);
  };

  const submitForm = useCallback(() => {
    const text = input.trim();
    if (!text) return;

    // A bare "/cmd [args]" line is a command, not a prompt.
    const cmdMatch = text.match(/^(\/[\w-]+)(?:\s+([\s\S]*))?$/);
    if (cmdMatch && commands.some((c) => c.name === cmdMatch[1])) {
      runCommand(cmdMatch[1], cmdMatch[2] ?? '');
      return;
    }

    const expanded = input.replace(
      /<pasted #([\w-]+) \| \d+ chars>/g,
      (_, id) => pastes[id]?.full ?? ''
    );

    // Attachments are already on disk in project_dir; the parts carry them for
    // rendering and the body carries the paths so the backend can prepend them
    // to the prompt the way the TUI does for @-mentions.
    const fileParts = filesUploadedInThisMessage.map((path) => ({
      type: 'source-document' as const,
      sourceId: `upload-${path}`,
      mediaType: 'application/octet-stream',
      title: path,
      filename: path,
    }));

    sendMessage(
      {
        parts: [{ type: 'text', text: expanded }, ...fileParts],
      },
      { body: { files: filesUploadedInThisMessage } }
    );

    setInput('');
    setPastes({});
    setPendingPaste(null);
    setLocalStorageInput('');
    resetHeight();
    setFilesUploadedInThisMessage([]);

    if (width && width > 768) textareaRef.current?.focus();
  }, [input, setLocalStorageInput, width, sendMessage, pastes, filesUploadedInThisMessage, commands]);

  const handleFileChange = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const picked = Array.from(event.target.files || []);
      if (picked.length === 0) return;
      await acceptFile(picked[0]);
    },
    [acceptFile]
  );

  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (!e.currentTarget.contains(e.relatedTarget as Node)) setIsDragOver(false);
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const handleDrop = useCallback(
    async (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setIsDragOver(false);
      const dropped = Array.from(e.dataTransfer.files);
      if (dropped.length === 0) return;
      await acceptFile(dropped[0]);
    },
    [acceptFile]
  );

  return (
    <div
      className={cx(
        'relative w-full flex flex-col overflow-visible transition-all duration-200 backdrop-blur-xl bg-background/80',
        {
          'bg-accent/20 border-2 border-dashed border-accent rounded-2xl p-4': isDragOver,
          'gap-2': !shouldExpand,
          'gap-4': shouldExpand,
          'rounded-3xl': !shouldExpand,
          'rounded-2xl': shouldExpand,
        }
      )}
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      {isDragOver && (
        <div className="absolute inset-0 bg-accent/10 border-2 border-dashed border-accent rounded-2xl flex items-center justify-center z-40 pointer-events-none">
          <div className="text-center">
            <div className="mx-auto mb-2 text-accent">
              <UploadIcon size={32} />
            </div>
            <p className="text-sm font-medium text-accent">{t.dropFileHere}</p>
            <p className="text-xs text-muted-foreground">{t.onlyOneFile}</p>
          </div>
        </div>
      )}

      <input
        type="file"
        className="fixed -top-4 -left-4 size-0.5 opacity-0 pointer-events-none"
        ref={fileInputRef}
        onChange={handleFileChange}
        tabIndex={-1}
      />

      {/* Attached-this-turn chips */}
      {filesUploadedInThisMessage.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-1">
          {filesUploadedInThisMessage.map((path) => (
            <span
              key={path}
              className="flex items-center gap-1.5 rounded-lg bg-muted/60 px-2 py-1 text-[11px] font-mono text-muted-foreground"
            >
              <span className="opacity-60">⟐</span>
              {path.split('/').pop()}
              <button
                className="opacity-50 hover:opacity-100"
                onClick={() =>
                  setFilesUploadedInThisMessage((prev) => prev.filter((p) => p !== path))
                }
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}

      {uploadQueue.length > 0 && (
        <div className="bg-muted/30 backdrop-blur-sm rounded-lg px-3 py-1.5 text-xs text-muted-foreground">
          <span>
            {t.uploading}: {uploadQueue[0]}
          </span>
          {uploadProgress > 0 && (
            <div className="w-full bg-background/50 rounded-full h-1 mt-1.5 overflow-hidden">
              <div
                className="bg-foreground/40 h-full rounded-full transition-all duration-300 ease-out"
                style={{ width: `${uploadProgress}%` }}
              />
            </div>
          )}
        </div>
      )}

      {/* One popover slot, three tenants: commands, files, paste hint. */}
      {isCmdDropdownOpen ? (
        <div className="absolute bottom-full left-0 mb-1 z-[9999] bg-background border-2 rounded-lg shadow-2xl max-h-[220px] overflow-y-auto w-96">
          <div className="px-3 py-1.5 border-b border-border/60 text-[10px] uppercase tracking-wide text-muted-foreground/60">
            commands · ↑↓ to move · tab completes · enter runs
          </div>
          {filteredCmds.map((cmd, index) => (
            <button
              key={cmd.name}
              className={cx(
                'w-full text-left px-3 py-2 hover:bg-accent cursor-pointer flex items-baseline gap-2',
                { 'bg-accent': index === selectedCmdIndex }
              )}
              onClick={() => runCommand(cmd.name, '')}
              onMouseEnter={() => setSelectedCmdIndex(index)}
            >
              <span className="font-mono text-sm shrink-0">{cmd.name}</span>
              <span className="text-xs text-muted-foreground truncate">{cmd.hint}</span>
              {cmd.kind === 'tui' && (
                <span className="ml-auto text-[10px] text-muted-foreground/60 shrink-0">terminal only</span>
              )}
            </button>
          ))}
        </div>
      ) : isFileDropdownOpen && filteredFiles.length > 0 ? (
        <div
          key={`dropdown-${atSymbolPosition}-${isFileDropdownOpen}`}
          className="absolute bottom-full left-0 mb-1 z-[9999] bg-background border-2 rounded-lg shadow-2xl max-h-[160px] overflow-y-auto w-96"
        >
          <div className="sticky top-0 bg-background px-3 py-1.5 border-b border-border/60 text-[10px] uppercase tracking-wide text-muted-foreground/60">
            files in this project · tab or enter inserts the path
          </div>
          {filteredFiles.map((file, index) => (
            <button
              key={file}
              className={cx(
                'w-full text-left px-3 py-2 hover:bg-accent cursor-pointer flex items-center gap-2',
                { 'bg-accent': index === selectedFileIndex }
              )}
              onClick={() => insertFileName(file)}
              onMouseEnter={() => setSelectedFileIndex(index)}
            >
              <span className="truncate font-mono text-xs">{file}</span>
            </button>
          ))}
        </div>
      ) : pendingPaste ? (
        <div className="absolute bottom-full left-0 right-0 mb-1 z-[9999] bg-background border rounded-lg shadow-lg px-4 py-2 text-xs text-muted-foreground">
          {t.pasteHintPrefix} ({pendingPaste.count} {t.pasteHintChars})
        </div>
      ) : null}

      <Textarea
        data-testid="multimodal-input"
        ref={textareaRef}
        placeholder={placeholderHint || t.inputPlaceholder}
        value={input}
        onChange={handleInput}
        onPaste={handleOnPaste}
        style={isHintPlaceholder ? ({ '--hint-color': accent } as React.CSSProperties) : undefined}
        className={cx(
          'min-h-[24px] max-h-[calc(75dvh)] overflow-hidden resize-none !text-s bg-muted pb-10 pl-5 pr-5 pt-2 dark:border-zinc-700 transition-all duration-200',
          {
            'rounded-3xl': !shouldExpand,
            'rounded-2xl': shouldExpand,
            'placeholder:text-[var(--hint-color)] placeholder:transition-colors placeholder:duration-1000': isHintPlaceholder,
          }
        )}
        rows={1}
        autoFocus
        onKeyDown={handleKeyDown}
      />

      <div className="absolute bottom-0 p-3 w-fit flex flex-row justify-start items-center gap-2">
        <AttachmentsButton fileInputRef={fileInputRef} status={status} />
      </div>

      <div className="absolute bottom-0 right-0 p-3 w-fit flex flex-row justify-end">
        {status === 'submitted' || status === 'streaming' ? (
          <StopButton stop={stop} setMessages={setMessages} />
        ) : (
          <SendButton input={input} submitForm={submitForm} uploadQueue={uploadQueue} />
        )}
      </div>
    </div>
  );
}

export const MultimodalInput = memo(PureMultimodalInput, (prevProps, nextProps) => {
  if (prevProps.status !== nextProps.status) return false;
  if (prevProps.messages.length !== nextProps.messages.length) return false;
  // onCommand's identity changes once /api/session's commands list loads
  // (handleCommand in chat.tsx is a useCallback keyed on `commands`). Missing
  // this meant the dropdown/Enter path kept calling a closure frozen on
  // commands=[] forever, so every command silently no-opped.
  if (prevProps.onCommand !== nextProps.onCommand) return false;
  return true;
});

function PureAttachmentsButton({
  fileInputRef,
  status,
}: {
  fileInputRef: React.MutableRefObject<HTMLInputElement | null>;
  status: UseChatHelpers<UIMessage>['status'];
}) {
  return (
    <Tip side="top" text="Attach a file. You can also drag one in, or paste an image.">
      <Button
        data-testid="attachments-button"
        variant="ghost"
        className="rounded-md rounded-bl-lg p-[7px] h-fit dark:border-zinc-700 hover:bg-slate-500"
        onClick={(event) => {
          event.preventDefault();
          fileInputRef.current?.click();
        }}
        disabled={status !== 'ready'}
      >
        <PaperclipIcon size={14} />
      </Button>
    </Tip>
  );
}

const AttachmentsButton = memo(PureAttachmentsButton);

function PureStopButton({
  stop,
  setMessages,
}: {
  stop: () => Promise<void>;
  setMessages: UseChatHelpers<UIMessage>['setMessages'];
}) {
  return (
    <Tip side="top" text="Interrupt. Stops the model at its next step; work already done is kept in the transcript.">
      <Button
        data-testid="stop-button"
        variant="ghost"
        className="rounded-full p-1.5 h-fit border dark:border-zinc-600 hover:bg-red-500"
        onClick={async (event) => {
          event.preventDefault();
          await stop();
          setMessages((messages) => messages);
        }}
      >
        <StopIcon size={14} />
      </Button>
    </Tip>
  );
}

const StopButton = memo(PureStopButton);

function PureSendButton({
  submitForm,
  input,
  uploadQueue,
}: {
  submitForm: () => void;
  input: string;
  uploadQueue: Array<string>;
}) {
  return (
    <Tip side="top" text="Send (enter). Use shift+enter for a newline.">
      <Button
        data-testid="send-button"
        variant="ghost"
        className="rounded-full p-1.5 h-fit border dark:border-zinc-600 hover:bg-slate-500"
        onClick={(event) => {
          event.preventDefault();
          submitForm();
        }}
        disabled={input.length === 0 || uploadQueue.length > 0}
      >
        <ArrowUpIcon size={14} />
      </Button>
    </Tip>
  );
}

const SendButton = memo(PureSendButton, (prevProps, nextProps) => {
  if (prevProps.uploadQueue.length !== nextProps.uploadQueue.length) return false;
  if (prevProps.input !== nextProps.input) return false;
  return true;
});
