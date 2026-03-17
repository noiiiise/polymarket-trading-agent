import { Play, Square, AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  useEngineStatus,
  useStartEngine,
  useStopEngine,
} from "@/hooks/use-polymarket";

export function EngineControls() {
  const { data: status } = useEngineStatus();
  const startEngine = useStartEngine();
  const stopEngine = useStopEngine();

  const isRunning = status?.state === "running";
  const isStarting = status?.state === "starting";
  const isError = status?.state === "error";
  const isStopped = status?.state === "stopped" || !status;

  const handleToggle = () => {
    if (isRunning || isStarting) {
      stopEngine.mutate();
    } else {
      startEngine.mutate();
    }
  };

  const formatUptime = (seconds: number): string => {
    if (!seconds) return "0s";
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  };

  return (
    <div className="flex items-center gap-3">
      {/* Status Indicator */}
      <div className="flex items-center gap-2">
        <div
          className={`h-2.5 w-2.5 rounded-full ${
            isRunning
              ? "bg-success animate-pulse-dot"
              : isStarting
              ? "bg-yellow-500 animate-pulse-dot"
              : isError
              ? "bg-danger"
              : "bg-muted-foreground/40"
          }`}
        />
        <span className="text-xs font-mono uppercase tracking-wider text-muted-foreground">
          {status?.state ?? "stopped"}
        </span>
      </div>

      {/* Uptime */}
      {isRunning && status?.uptime ? (
        <span className="text-xs font-mono text-muted-foreground hidden md:inline">
          {formatUptime(status.uptime)}
        </span>
      ) : null}

      {/* Paper Trading Badge */}
      {status?.paperTrading !== false ? (
        <Badge
          variant="outline"
          className="border-yellow-500/50 bg-yellow-500/10 text-yellow-500 text-[10px] uppercase tracking-wider font-mono"
        >
          Paper
        </Badge>
      ) : null}

      {/* Error indicator */}
      {isError && status?.lastError ? (
        <div className="flex items-center gap-1 text-danger">
          <AlertTriangle className="h-3.5 w-3.5" />
          <span className="text-xs font-mono truncate max-w-[120px]">
            {status.lastError}
          </span>
        </div>
      ) : null}

      {/* Start/Stop Button */}
      <Button
        size="sm"
        variant={isStopped || isError ? "default" : "destructive"}
        onClick={handleToggle}
        disabled={startEngine.isPending || stopEngine.isPending}
        className={`h-8 gap-1.5 text-xs font-mono uppercase tracking-wider ${
          isStopped || isError
            ? "bg-success/20 text-success border border-success/30 hover:bg-success/30"
            : "bg-danger/20 text-danger border border-danger/30 hover:bg-danger/30"
        }`}
      >
        {isStopped || isError ? (
          <>
            <Play className="h-3 w-3" />
            Start
          </>
        ) : (
          <>
            <Square className="h-3 w-3" />
            Stop
          </>
        )}
      </Button>
    </div>
  );
}
