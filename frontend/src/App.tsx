import { Toaster } from 'sonner';
import { Chat } from './components/chat';
import { GuiGate } from './components/gui-gate';
import { SessionProvider } from './contexts/SessionContext';
import { TooltipProvider } from './components/ui/tooltip';

export default function App() {
  return (
    <SessionProvider>
      {/* delayDuration: long enough not to fire on a mouse passing through,
          short enough that hovering to ask "what is this?" answers quickly. */}
      <TooltipProvider delayDuration={400} skipDelayDuration={300}>
        <Chat />
      </TooltipProvider>
      <GuiGate />
      <Toaster position="top-center" theme="dark" richColors />
    </SessionProvider>
  );
}
