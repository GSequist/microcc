'use client';

import { useEffect, useState } from 'react';

// Webui counterpart to the TUI's _flash_status: a transient line shown
// above the input, same slot as TodoTracker. Backend sends data-status
// parts keyed by a stable id ("status") that AI SDK updates in place —
// `at` is a monotonic marker (the compaction checkpoint index) so this
// component can tell a genuinely new flash from a re-render of the same
// one and re-arm its own auto-hide timer accordingly.
const FLASH_DURATION_MS = 8000;

interface StatusFlashData {
  text: string;
  at: number;
}

export const StatusFlash = ({ status }: { status: StatusFlashData | null }) => {
  const [shown, setShown] = useState<StatusFlashData | null>(null);

  useEffect(() => {
    if (!status) return;
    setShown(status);
    const t = setTimeout(() => setShown(null), FLASH_DURATION_MS);
    return () => clearTimeout(t);
  }, [status?.at]);

  if (!shown) return null;

  return (
    <div className="flex flex-row gap-2 items-center pl-5 pb-1 text-sm text-amber-600 dark:text-amber-400">
      <span>{shown.text}</span>
    </div>
  );
};
