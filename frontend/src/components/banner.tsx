import { memo } from 'react';

// Straight transcription of the TUI banner (screens/window_overlay_.py).
// The `\` escapes matter — the art is full of quotes and backticks.
const ART = [
  '                   88',
  '                   ""',
  '',
  '88,dPYba,,adPYba,  88  ,adPPYba, 8b,dPPYba,  ,adPPYba,     ,adPPYba,  ,adPPYba,',
  '88P\'   "88"    "8a 88 a8"     "" 88P\'   "Y8 a8"     "8a   a8"     "" a8"     ""',
  '88      88      88 88 8b         88         8b       d8 · 8b         8b',
  '88      88      88 88 "8a,   ,aa 88         "8a,   ,a8"   "8a,   ,aa "8a,   ,aa',
  '88      88      88 88  `"Ybbd8"\' 88          `"YbbdP"\'     `"Ybbd8"\'  `"Ybbd8"\'',
];

// Same seven the TUI picks from (_BANNER_COLORS). Picked once per tab, like the
// TUI does per launch, then it never changes.
const COLORS = [
  '#4a8a68', // green (original)
  '#8fa8d9', // pastel blue
  '#c99bd9', // pastel lavender
  '#d98aa3', // pastel pink
  '#d9a789', // pastel peach
  '#7ac9c0', // pastel teal
  '#c9c17a', // pastel gold
];

const ACCENT = (() => {
  try {
    const saved = sessionStorage.getItem('mcc-accent');
    if (saved) return saved;
    const pick = COLORS[Math.floor(Math.random() * COLORS.length)];
    sessionStorage.setItem('mcc-accent', pick);
    return pick;
  } catch {
    return COLORS[Math.floor(Math.random() * COLORS.length)];
  }
})();

export const useBannerColor = () => ACCENT;

// The TUI's rich markup tints every alphanumeric run with the accent colour and
// leaves the punctuation (the commas, quotes and backticks that draw the
// letterform's shading) in grey42. That's the whole rule — no table needed.
function tint(line: string, accent: string) {
  const out: JSX.Element[] = [];
  let run = '';
  let runKind: 'accent' | 'grey' | null = null;

  const flush = (i: number) => {
    if (!run) return;
    out.push(
      <span key={i} style={runKind === 'accent' ? { color: accent } : undefined} className={runKind === 'grey' ? 'text-muted-foreground/50' : undefined}>
        {run}
      </span>
    );
    run = '';
  };

  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    const kind: 'accent' | 'grey' = /[A-Za-z0-9·]/.test(ch) ? 'accent' : 'grey';
    if (kind !== runKind) {
      flush(i);
      runKind = kind;
    }
    run += ch;
  }
  flush(line.length);
  return out;
}

function PureBanner() {
  const accent = useBannerColor();

  return (
    <div className="flex flex-col items-start gap-2 select-none">
      {/* The art has a fixed character width, so it scales by font-size rather
          than wrapping. clamp() keeps it readable from phone to desktop. */}
      <pre
        aria-label="micro-cc"
        className="font-mono leading-[1.15] whitespace-pre"
        style={{
          fontSize: 'clamp(5px, 1.35vw, 12px)',
          // The art is built from ", ', ` and quote pairs; a coding font with
          // ligatures would fuse them and bend the letterforms out of shape.
          fontVariantLigatures: 'none',
          fontFeatureSettings: '"liga" 0, "calt" 0',
        }}
      >
        {ART.map((line, i) => (
          <div key={i}>{tint(line, accent) as any}</div>
        ))}
      </pre>

      <p className="text-[11px] italic text-muted-foreground/70">
        Knowledge work is, by extension, a coding problem.
      </p>

      <p className="flex items-center gap-2 font-mono text-[10px] text-muted-foreground/60">
        <kbd style={{ color: accent }}>enter</kbd> submit
        <span className="opacity-40">·</span>
        <kbd style={{ color: accent }}>@</kbd> files
        <span className="opacity-40">·</span>
        <kbd style={{ color: accent }}>/</kbd> commands
      </p>
    </div>
  );
}

export const Banner = memo(PureBanner);

// Avatar-sized stand-in for the wordmark, same accent as the banner.
function PureAssistantMark() {
  const accent = useBannerColor();

  return (
    <div className="size-8 flex items-center justify-center rounded-full ring-1 shrink-0 ring-border bg-background">
      <span
        className="font-mono text-[13px] leading-none"
        style={{ color: accent }}
      >
        ◇
      </span>
    </div>
  );
}

export const AssistantMark = memo(PureAssistantMark);

// Header wordmark: the art reduced to a glyph + name, same accent as the banner.
function PureBannerMark() {
  const accent = useBannerColor();

  return (
    <span
      className="flex items-center gap-1.5 font-mono"
      style={{ color: accent }}
      title="micro-cc — on the metal"
    >
      <span className="text-[13px] leading-none">◇</span>
      <span className="tracking-tight">micro cc</span>
    </span>
  );
}

export const BannerMark = memo(PureBannerMark);
