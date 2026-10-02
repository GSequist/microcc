// Shim for the multi-client envUtils the components were lifted from.
// micro-cc is single-tenant, English, and served from its own origin, so the
// client/locale branching collapses to constants. Kept as a module (rather
// than editing every import) so the copied components stay diff-able against
// their source.
import { greetingCopy } from './copy/greeting';
import { inputCopy } from './copy/input';
import { toolsCopy } from './copy/tools';
import { todoCopy } from './copy/todos';

export const getCopy = () => ({
  ...greetingCopy.en,
  ...inputCopy.en,
  ...toolsCopy.en,
  ...todoCopy.en,
});

// The TUI banner doubles as the assistant avatar — rendered as a glyph, not an
// image, so there's no asset to ship. See components/banner.tsx for the full
// colour-cycling wordmark.
export const getClientLogos = () => ({ logo: '', alt: 'micro-cc' });

// Hints are no longer hardcoded here — they come from the server's shared
// registry (micro_cc/utils/hints.py) via /api/session, so the TUI hint bar and
// the GUI placeholder can't drift apart. See useSession().hints.

export const getBaseUrl = () => '';
