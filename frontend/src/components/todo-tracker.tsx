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

export const TodoTracker = ({todos}: ToDos) => {

    const items  = Object.values(todos)         
    const total  = items.length
    const done   = items.filter(t => t.status === 'completed').length
    const active = items.find(t => t.status === 'in_progress')
    const label = active ? (active.activeForm ?? active.content)
        : done === total ? 'Done'
        : `${total - done} tasks`

    return (
        <div className="flex flex-col pl-5 pb-1">
            <div className="flex flex-row gap-2 items-center">
                <span className="inline-block animate-spin">⚙</span> 
                    <span className="inline-block bg-[length:250%_100%] bg-clip-text text-transparent bg-no-repeat animate-shimmer text-sm"
                            style={{
                            backgroundImage: 'linear-gradient(90deg, transparent 40%, hsl(var(--background)) 50%, transparent 60%), linear-gradient(hsl(var(--muted-foreground)), hsl(var(--muted-foreground)))',
                            }}
                        >
                        {label} 
                    </span>
            </div>
        </div>
        )
}