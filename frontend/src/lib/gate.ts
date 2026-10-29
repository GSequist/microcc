// One authenticated tab: every /api call carries this tab's id, the server binds
// the session to the first tab that shows up, and a heartbeat notices when the
// terminal closes the GUI (esc) so the tab can close itself.
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

export type GateState = 'closed' | 'foreign' | 'denied';
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

  let misses = 0;
  setInterval(async () => {
    try {
      await window.fetch('/api/ping');
      misses = 0;
    } catch {
      if (++misses === 2) {
        emit('closed');
        window.close(); // only works for script-opened tabs; the overlay covers the rest
      }
    }
  }, 2000);
}
