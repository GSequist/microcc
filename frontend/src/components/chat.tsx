import type { UIMessage } from 'ai';
import { DefaultChatTransport } from 'ai';
import { useChat } from '@ai-sdk/react';
import { useEffect, useCallback, useRef, useState } from 'react';
import { MultimodalInput } from './multimodal-input';
import { Messages } from './messages';
import { TodoTracker } from './todo-tracker';
import { StatusFlash } from './status-flash';
import { toast } from './toast';
import { ChatHeader } from './chat-header';
import { CommandModal, type ModalKind } from './command-modal';
import { QuestionPanel, type PendingQuestion } from './question-panel';
import { useSession } from '../contexts/SessionContext';

export function Chat() {
  const { session, commands, refresh } = useSession();
  const [modal, setModal] = useState<ModalKind>(null);
  const [question, setQuestion] = useState<PendingQuestion | null>(null);
  const historyLoaded = useRef(false);

  const {
    messages,
    sendMessage,
    stop,
    setMessages,
    status,
    addToolApprovalResponse,
  } = useChat({
    transport: new DefaultChatTransport({
      api: '/api/chat',
    }),

    // Approve → the SDK re-POSTs /api/chat, which the server resolves against
    // the still-suspended claude_loop generator rather than starting a new
    // turn. Once the server answers with a real assistant message this stops
    // matching, so there's no loop.
    sendAutomaticallyWhen: ({ messages: msgs }) => {
      const last = msgs[msgs.length - 1];
      return last?.parts?.some((p) => 'state' in p && p.state === 'approval-responded') ?? false;
    },

    // ask_user_question_tool_ parks the loop and ships its questions as a data
    // part; the modal below collects the answers.
    onData: (part) => {
      if (part.type === 'data-question') {
        setQuestion((part as any).data as PendingQuestion);
      }
    },

    onError: (error) => {
      console.error('Chat error:', error);
      toast.error(error.message || 'Something went wrong');
    },
  });

  // History comes off local disk in one shot — no pagination, no cursors.
  useEffect(() => {
    if (historyLoaded.current) return;
    historyLoaded.current = true;
    fetch('/api/messages')
      .then((r) => r.json())
      .then((data) => {
        if (data.messages?.length) setMessages(data.messages);
      })
      .catch(() => {});
  }, [setMessages]);

  const customStop = useCallback(async () => {
    try {
      await fetch('/api/chat/stop', { method: 'POST' });
    } catch (error) {
      console.error('Error stopping:', error);
    }
    stop();
  }, [stop]);

  // ── Approval flow ──
  // Identical in shape to the app this was lifted from; only the resume
  // mechanism behind /api/chat differs (live generator, not a Redis replay).
  const handleApproval = useCallback(
    async (approvalId: string, _toolCallId: string, approved: boolean) => {
      if (!approved) {
        try {
          await fetch('/api/chat/deny', { method: 'POST' });
        } catch {
          toast.error('Failed to deny tool call');
        }
      }
      addToolApprovalResponse({
        id: approvalId,
        approved,
        reason: approved ? undefined : 'User denied',
      });
    },
    [addToolApprovalResponse]
  );

  // ── Slash commands ──
  const handleCommand = useCallback(
    async (name: string, args: string) => {
      const cmd = commands.find((c) => c.name === name);
      if (!cmd) return;

      // Gate on an in-flight turn — a command like /clear or /rewind
      // mutating messages/session state while bridge.py's stream() is
      // concurrently writing into the same state (server-side, same race
      // start_live_tui_ guards against for the TUI) corrupts the turn
      // rather than just looking odd. 'error' is fine to act on: nothing
      // is in flight then, and /clear is often exactly the recovery move.
      if (status === 'submitted' || status === 'streaming') {
        toast.warning(`${name} — wait for the current response to finish, or stop it first`);
        return;
      }

      if (cmd.kind === 'tui') {
        toast.warning(`${name} handles secrets — run it in the terminal`);
        return;
      }

      if (cmd.kind === 'ui') {
        if (name === '/copy') {
          const text = messages
            .flatMap((m) =>
              (m.parts ?? [])
                .filter((p: any) => p.type === 'text')
                .map((p: any) => (m.role === 'user' ? `› ${p.text}` : p.text))
            )
            .join('\n\n');
          navigator.clipboard.writeText(text);
          toast.success('transcript copied');
          return;
        }
        setModal(name.slice(1) as ModalKind);
        return;
      }

      if (cmd.kind === 'prompt') {
        // /setup, /new-skill, /new-mcp, /skills, /mcp, /headless — the server
        // turns these into the same hidden <system-reminder> turn the TUI
        // sends, so the model does the work instead of us building a wizard
        // per command.
        sendMessage({ parts: [{ type: 'text', text: args ? `${name} ${args}` : name }] });
        return;
      }

      // kind === 'action' — server-side, no model call.
      const res = await fetch('/api/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, args }),
      });
      const data = await res.json();
      if (data.cleared) setMessages([]);
      if (data.message) toast.success(data.message);
      await refresh();
    },
    [commands, messages, sendMessage, setMessages, refresh, status]
  );

  // ── ask_user_question_tool_ ──
  // Answers go up first, then the SDK's approval response fires
  // sendAutomaticallyWhen, which re-POSTs /api/chat and resumes the parked
  // generator — by which point the answers are already in place server-side.
  const handleAnswer = useCallback(
    async (answers: Record<string, string | string[]>) => {
      const pending = question;
      setQuestion(null);
      if (!pending) return;
      try {
        await fetch('/api/chat/answer', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ answers }),
        });
      } catch {
        toast.error('Failed to send answers');
        return;
      }
      addToolApprovalResponse({ id: pending.approvalId, approved: true });
    },
    [question, addToolApprovalResponse]
  );

  const handleQuestionCancel = useCallback(() => {
    const pending = question;
    setQuestion(null);
    if (!pending) return;
    // A falsy answer is how claude_loop is told to abandon the turn.
    addToolApprovalResponse({ id: pending.approvalId, approved: false, reason: 'User cancelled' });
  }, [question, addToolApprovalResponse]);

  // Todos ride in as data-todos parts; walk back for the newest.
  const getLatestTodos = (msgs: UIMessage[]) => {
    for (let i = msgs.length - 1; i >= 0; i--) {
      const parts = msgs[i].parts;
      if (!parts) continue;
      for (let j = parts.length - 1; j >= 0; j--) {
        if (parts[j].type === 'data-todos') return (parts[j] as any).data.todos;
      }
    }
    return null;
  };

  // Status-bar flashes (e.g. compaction) ride in as data-status parts —
  // same walk-back pattern as todos above. See status-flash.tsx.
  const getLatestStatus = (msgs: UIMessage[]) => {
    for (let i = msgs.length - 1; i >= 0; i--) {
      const parts = msgs[i].parts;
      if (!parts) continue;
      for (let j = parts.length - 1; j >= 0; j--) {
        if (parts[j].type === 'data-status') return (parts[j] as any).data as { text: string; at: number };
      }
    }
    return null;
  };

  const rawTodos = getLatestTodos(messages);
  const latestStatus = getLatestStatus(messages);
  // Stop rendering once every item is terminal — a finished list shouldn't linger.
  const todoItems = rawTodos ? (Object.values(rawTodos) as { status: string }[]) : [];
  const todos =
    todoItems.length > 0 && todoItems.every((t) => t.status === 'completed' || t.status === 'failed')
      ? null
      : rawTodos;

  return (
    <div className="relative flex flex-col min-w-0 h-dvh bg-background">
      <ChatHeader onOpenModal={setModal} />

      <Messages
        chatId={session?.project_dir || 'micro-cc'}
        status={status}
        messages={messages}
        todos={todos}
        setMessages={setMessages}
        onApproval={handleApproval}
      />

      <div className="absolute bottom-0 left-0 right-0 flex flex-col">
        {latestStatus && (
          <div className="mx-auto w-full md:max-w-3xl">
            <StatusFlash status={latestStatus} />
          </div>
        )}
        {todos && (
          <div className="mx-auto w-full md:max-w-3xl">
            <TodoTracker todos={todos} />
          </div>
        )}
        <div className="flex mx-auto px-2 pb-4 md:pb-6 gap-2 w-full md:max-w-3xl z-10">
          {question ? (
            <QuestionPanel
              pending={question}
              onSubmit={handleAnswer}
              onCancel={handleQuestionCancel}
            />
          ) : (
            <form className="flex w-full">
              <MultimodalInput
                chatId={session?.project_dir || 'micro-cc'}
                sendMessage={sendMessage}
                status={status}
                stop={customStop}
                messages={messages}
                setMessages={setMessages}
                onCommand={handleCommand}
              />
            </form>
          )}
        </div>
      </div>

      <CommandModal
        kind={modal}
        onClose={() => setModal(null)}
        onCleared={() => setMessages([])}
        onRewound={(msgs) => setMessages(msgs)}
      />
    </div>
  );
}
