import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Radio, Eye, SkipForward, CheckCircle2, Plus, Send, X } from "lucide-react";
import { useSignals, useAddSignal } from "@/hooks/use-polymarket";
import type { TradeSignal } from "@/types/polymarket";

function timeAgo(ts: string): string {
  const diff = Date.now() - new Date(ts).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

const actionConfig: Record<string, { color: string; icon: React.ElementType; label: string }> = {
  watching: { color: "bg-primary", icon: Eye, label: "WATCH" },
  considering: { color: "bg-yellow-500", icon: Radio, label: "CONSIDER" },
  passed: { color: "bg-muted-foreground", icon: SkipForward, label: "PASS" },
  executed: { color: "bg-success", icon: CheckCircle2, label: "EXEC" },
};

const strategyBorder: Record<string, string> = {
  copy_trade: "border-primary/60 text-primary",
  volume_spike: "border-accent/60 text-accent",
  manual_signal: "border-yellow-500/60 text-yellow-500",
};

const strategyLabel: Record<string, string> = {
  copy_trade: "COPY",
  volume_spike: "VOL",
  manual_signal: "NOTE",
};

function SignalRow({ signal }: { signal: TradeSignal }) {
  const cfg = actionConfig[signal.action] ?? actionConfig.watching;
  const Icon = cfg.icon;

  return (
    <div className="flex items-center gap-2 px-2 py-1.5 hover:bg-secondary/20 rounded-sm group animate-in fade-in slide-in-from-top-1 duration-200">
      <div className={`h-2 w-2 rounded-full flex-shrink-0 ${cfg.color}`} />
      <Icon className="h-3 w-3 text-muted-foreground/60 flex-shrink-0" />

      <div className="flex-1 min-w-0 flex items-center gap-2">
        <span className="text-xs font-mono text-foreground truncate max-w-[140px] md:max-w-[220px]">
          {signal.market}
        </span>
        <span className="text-[11px] font-mono text-muted-foreground truncate flex-1">
          {signal.reason}
        </span>
      </div>

      <div className="flex items-center gap-1.5 flex-shrink-0">
        {signal.confidence != null ? (
          <div className="w-12 h-1 bg-secondary rounded-full overflow-hidden">
            <div
              className="h-full bg-primary/70 rounded-full"
              style={{ width: `${signal.confidence}%` }}
            />
          </div>
        ) : null}
        <Badge
          variant="outline"
          className={`text-[9px] px-1 py-0 h-4 font-mono ${strategyBorder[signal.strategy] ?? "border-muted-foreground/40 text-muted-foreground"}`}
        >
          {strategyLabel[signal.strategy] ?? signal.strategy}
        </Badge>
        <span className="text-[10px] font-mono text-muted-foreground/50 w-12 text-right">
          {timeAgo(signal.timestamp)}
        </span>
      </div>
    </div>
  );
}

export function SignalsFeed() {
  const { data: signals, isLoading } = useSignals();
  const addSignal = useAddSignal();
  const [showForm, setShowForm] = useState(false);
  const [market, setMarket] = useState("");
  const [note, setNote] = useState("");

  const handleSubmit = () => {
    if (!market.trim() || !note.trim()) return;
    addSignal.mutate({ market: market.trim(), note: note.trim() }, {
      onSuccess: () => {
        setMarket("");
        setNote("");
        setShowForm(false);
      },
    });
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Radio className="h-6 w-6 text-muted-foreground/40 animate-pulse" />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-mono uppercase tracking-widest text-muted-foreground">
            Trade Signals
          </h3>
          {signals?.length ? (
            <Badge variant="secondary" className="text-[10px] font-mono px-1.5 py-0 h-4">
              {signals.length}
            </Badge>
          ) : null}
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 text-xs font-mono gap-1 text-muted-foreground hover:text-primary"
          onClick={() => setShowForm((v) => !v)}
        >
          {showForm ? <X className="h-3 w-3" /> : <Plus className="h-3 w-3" />}
          {showForm ? "Cancel" : "Add Note"}
        </Button>
      </div>

      {/* Add Note Form */}
      {showForm ? (
        <div className="animate-in fade-in slide-in-from-top-2 duration-150">
          <div className="flex gap-2 p-2 rounded-md border border-yellow-500/30 bg-yellow-500/5">
            <Input
              placeholder="Market name"
              value={market}
              onChange={(e) => setMarket(e.target.value)}
              className="h-7 text-xs font-mono bg-background/50 border-border/50 flex-1"
            />
            <Input
              placeholder="Note from X / Twitter"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") handleSubmit(); }}
              className="h-7 text-xs font-mono bg-background/50 border-border/50 flex-[2]"
            />
            <Button
              size="sm"
              className="h-7 px-2"
              onClick={handleSubmit}
              disabled={addSignal.isPending || !market.trim() || !note.trim()}
            >
              <Send className="h-3 w-3" />
            </Button>
          </div>
        </div>
      ) : null}

      {/* Signal List */}
      {!signals?.length ? (
        <div className="text-center py-16">
          <Radio className="h-8 w-8 mx-auto mb-2 text-muted-foreground/40" />
          <p className="text-sm font-mono text-muted-foreground">
            No signals yet. Start the engine to see trade considerations.
          </p>
        </div>
      ) : (
        <ScrollArea className="h-[500px] rounded-md border border-border/50 bg-background/50">
          <div className="p-1">
            {signals.map((signal) => (
              <SignalRow key={signal.id} signal={signal} />
            ))}
          </div>
        </ScrollArea>
      )}
    </div>
  );
}
