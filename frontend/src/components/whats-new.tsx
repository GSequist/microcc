import { useBannerColor } from './banner';

export const WhatsNew = ({ note }: { note: { version: string; lines: string[] } }) => {
  const accent = useBannerColor();

  return (
    <div className="w-full px-4 font-mono text-[11px] text-muted-foreground select-none">
      <p>
        <span className="font-semibold" style={{ color: accent }}>✦ micro-cc {note.version} just landed ✦</span>{' '}
        <span className="opacity-60">what's new</span>
      </p>
      <ul className="mt-1 flex flex-col gap-0.5">
        {note.lines.map((line) => (
          <li key={line}>
            <span className="opacity-50">· </span>
            {line}
          </li>
        ))}
      </ul>
    </div>
  );
};
