import { useEffect, useRef } from "react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Terminal } from "lucide-react";
import { useLogs } from "@/hooks/use-polymarket";

const levelColors: Record<string, string> = {
  info: "text-muted-foreground",
  warn: "text-yellow-500",
  error: "text-danger",
  trade: "text-primary",
  debug: "text-muted-foreground/50",
};

const levelLabels: Record<string, string> = {
  info: "INF",
  warn: "WRN",
  error: "ERR",
  trade: "TRD",
  debug: "DBG",
};

export function ActivityLog() {
  const { data: logs, isLoading } = useLogs(200);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Terminal className="h-6 w-6 text-muted-foreground/40 animate-pulse" />
      </div>
    );
  }

  if (!logs?.length) {
    return (
      <div className="text-center py-16">
        <Terminal className="h-8 w-8 mx-auto mb-2 text-muted-foreground/40" />
        <p className="text-sm font-mono text-muted-foreground">
          No logs yet. Start the engine to see activity.
        </p>
      </div>
    );
  }

  const formatTimestamp = (ts: string): string => {
    const d = new Date(ts);
    return d.toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  };

  return (
    <ScrollArea className="h-[500px] rounded-md border border-border/50 bg-background/50">
      <div className="p-3 space-y-0.5">
        {logs.map((log) => (
          <div
            key={log.id}
            className="flex gap-2 text-xs font-mono leading-relaxed py-0.5 hover:bg-secondary/20 px-1 rounded-sm"
          >
            <span className="text-muted-foreground/50 flex-shrink-0">
              {formatTimestamp(log.timestamp)}
            </span>
            <span
              className={`flex-shrink-0 w-7 ${
                levelColors[log.level] ?? levelColors.info
              }`}
            >
              {levelLabels[log.level] ?? "INF"}
            </span>
            {log.source ? (
              <span className="text-accent/60 flex-shrink-0">
                [{log.source}]
              </span>
            ) : null}
            <span
              className={
                levelColors[log.level] ?? levelColors.info
              }
            >
              {log.message}
            </span>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </ScrollArea>
  );
}
