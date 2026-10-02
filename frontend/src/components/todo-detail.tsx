import { useState } from "react";
import { Collapsible, CollapsibleTrigger, CollapsibleContent } from './ui/collapsible'
import { cn } from '../lib/utils/utils'

interface ToDos {
    todos: {
        [id: string]: {
            content: string;
            status: 'pending' | 'in_progress' | 'completed' | 'failed';
            activeForm?: string;
            tag?: string;
            notes?: string;
        };
        };
    }

export function TodoDetail({ todos }: ToDos ) {

    const [isOneToDoExpanded, setIsOneToDoExpanded] = useState<string | null>(null);


    const glyph = (s: string) =>
            s === 'completed'   ? <span className="text-muted-foreground">●</span>
        : s === 'in_progress' ? <span className="inline-block animate-spin text-foreground/70">◐</span>
        : s === 'failed'      ? <span className="text-destructive">●</span>
        : <span className="text-muted-foreground/40">○</span>; // pending / default


    return (
        <div className="p-3">
            <div className="flex flex-col gap-1">
                <div className="max-h-48 overflow-y-auto flex flex-col">
                    {Object.entries(todos).map(([id, t]) => (
                        <div
                            key={id}
                            className="flex items-start gap-2 text-xs py-1">
                            <span className="shrink-0 mt-1">{glyph(t.status)}</span>
                            <Collapsible
                                className="flex-1 min-w-0"
                                open={isOneToDoExpanded === id}
                                onOpenChange={(open) => setIsOneToDoExpanded(open ? id : null)}
                            >
                                <CollapsibleTrigger asChild>
                                    <button className={cn(
                                        "flex items-center justify-between gap-2 w-full text-left py-0.5 px-1.5 -ml-1.5 rounded-md",
                                        "hover:bg-accent/50 transition-colors duration-150",
                                        isOneToDoExpanded === id && "bg-accent/50",
                                        t.status === 'in_progress' && "font-medium"
                                    )}>
                                        <span className={cn(
                                            "min-w-0 break-words",
                                            t.status === 'completed' && 'line-through text-muted-foreground/50'
                                        )}>
                                            {t.content}
                                        </span>
                                        {t.notes && (
                                            <span className="text-muted-foreground text-xs shrink-0">
                                                {isOneToDoExpanded === id ? '▼' : '▶'}
                                            </span>
                                        )}
                                    </button>
                                </CollapsibleTrigger>

                                {t.notes && (
                                    <CollapsibleContent>
                                        <div className="text-xs italic text-muted-foreground/70 py-1.5 pl-1.5 mt-0.5 max-h-28 overflow-y-auto break-words whitespace-pre-wrap">
                                            {t.notes}
                                        </div>
                                    </CollapsibleContent>
                                )}
                            </Collapsible>
                        </div>
                    ))}
                </div>
            </div>
        </div>
    )
}
