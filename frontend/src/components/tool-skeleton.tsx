export function LoadingSkeleton({ toolName }: { toolName: string }) {
  return (
    <div className="rounded-lg border border-border bg-muted/20 overflow-hidden animate-pulse">
      <div className="flex items-center gap-2 px-3 py-2">
        <span className="text-muted-foreground animate-spin text-xs">◌</span>
        <span className="font-semibold text-sm font-mono text-foreground">{toolName}</span>
        <div className="flex-1 h-3 bg-muted rounded ml-2" />
      </div>
    </div>
  );
}
