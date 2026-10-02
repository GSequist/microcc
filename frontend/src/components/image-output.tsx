
export function ImageOutput({ toolName, output }: { toolName: string; output: any }) {
    if (typeof output !== 'string') {
        return (
            <pre className="text-xs whitespace-pre-wrap break-all max-h-64 overflow-y-auto text-muted-foreground">
                {JSON.stringify(output, null, 2)}
            </pre>
        )
    }

    return (
        <div className="flex items-center justify-center p-4">
            <img
                src={output}
                alt={toolName}
                className="max-w-full max-h-96 object-contain rounded"
            />
        </div>
    )
}
