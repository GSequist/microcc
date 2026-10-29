// Mirror of utils/subagent_notice_.parse: subagent wake turns -> card entries plus any trailing text.
// Old "[subagent checkpoint]" turns parse to the same shape, minus the result.

export type Notice = { name: string; status: string; summary: string; result: string; usage: string; transcript: string };
export type ParsedNotice = { notes: Notice[]; text: string };

const BLOCK = /<subagent-notification>([\s\S]*?)<\/subagent-notification>/g;
const COLD = /^\[this is the first check since boss \(re\)started[\s\S]*?\n\n/;
const LEGACY_HEAD = '[subagent checkpoint]';
const LEGACY_LINE = /^\S+ (.+?) — ([A-Z_]+): (.*?)(?: {2}\[\d+ unread mail\])?(?: {2}\[[\d,]+ tokens\])?$/;

const unesc = (v: string) => v.replace(/&lt;/g, '<').replace(/&amp;/g, '&');
const tag = (body: string, key: string) => unesc(body.match(new RegExp(`<${key}>([\\s\\S]*?)</${key}>`))?.[1].trim() ?? '');

// null unless the turn starts with a notification, so a pasted block stays a normal message.
export function parseNotices(text: string): ParsedNotice | null {
  const body = text.trim().replace(COLD, '');
  if (body.startsWith('<subagent-notification>')) {
    const notes = [...body.matchAll(BLOCK)].map(([, b]) => ({
      name: tag(b, 'name'), status: tag(b, 'status'), summary: tag(b, 'summary'),
      result: tag(b, 'result'), usage: tag(b, 'usage'), transcript: tag(b, 'transcript'),
    }));
    return { notes, text: body.replace(BLOCK, '').trim() };
  }
  if (!body.startsWith(LEGACY_HEAD)) return null;
  const notes: Notice[] = [];
  for (const line of body.slice(LEGACY_HEAD.length).trim().split('\n')) {
    const m = line.trim().match(LEGACY_LINE);
    if (!m) break;
    notes.push({ name: m[1], status: m[2], summary: m[3], result: '', usage: '', transcript: '' });
  }
  return notes.length ? { notes, text: '' } : null;
}
