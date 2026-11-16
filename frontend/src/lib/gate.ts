// One authenticated tab: every /api call carries this tab's id, the server binds
// the session to one tab on a lease, and a heartbeat notices when the terminal
// closes the GUI (esc) so the tab can close itself.
const KEY = 'mcc-tab';

function tabId(): string {
  try {
    let id = sessionStorage.getItem(KEY);
    if (!id) {
      id = crypto.randomUUID();
      sessionStorage.setItem(KEY, id);
    }
    return id;
  } catch {
    return crypto.randomUUID();
  }
}

// 'ok' clears the overlay: a failed ping across sleep or a lease handover is not final; only 'denied' is.
export type GateState = 'ok' | 'closed' | 'foreign' | 'denied';
const emit = (state: GateState) => window.dispatchEvent(new CustomEvent<GateState>('gui-gate', { detail: state }));

export function installGate() {
  const id = tabId();
  const realFetch = window.fetch.bind(window);

  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (!url.startsWith('/api/') && !url.startsWith(location.origin + '/api/')) return realFetch(input, init);
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    headers.set('X-Tab', id);
    const res = await realFetch(input, { ...init, headers });
    if (res.status === 409) emit('foreign');
    else if (res.status === 403) emit('denied');
    return res;
  };

  // Closing or reloading frees the session now, so the next tab doesn't wait out the lease.
  window.addEventListener('pagehide', () => navigator.sendBeacon(`/api/release?tab=${encodeURIComponent(id)}`));

  let misses = 0;
  let denied = false;
  window.addEventListener('gui-gate', (e) => {
    if ((e as CustomEvent<GateState>).detail === 'denied') denied = true;
  });
  const ping = async () => {
    if (denied) return;
    try {
      const res = await window.fetch('/api/ping');
      misses = 0;
      if (res.ok) {
        emit('ok');
        window.dispatchEvent(new Event('gui-online'));
      }
    } catch {
      if (++misses === 2) {
        emit('closed');
        window.close(); // only works for script-opened tabs; the overlay covers the rest
      }
    }
  };
  setInterval(ping, 2000);
  // Timers are frozen asleep and throttled in the background: check the moment we're back.
  document.addEventListener('visibilitychange', () => document.visibilityState === 'visible' && ping());
  window.addEventListener('online', ping);
}
