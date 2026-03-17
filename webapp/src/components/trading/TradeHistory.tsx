import { ArrowRightLeft } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { useTrades } from "@/hooks/use-polymarket";

const strategyColors: Record<string, string> = {
  copy_trade: "border-accent/50 bg-accent/10 text-accent",
  volume_spike: "border-primary/50 bg-primary/10 text-primary",
  manual: "border-muted-foreground/50 bg-muted/50 text-muted-foreground",
  mean_reversion: "border-yellow-500/50 bg-yellow-500/10 text-yellow-500",
  momentum: "border-blue-500/50 bg-blue-500/10 text-blue-500",
};

export function TradeHistory() {
  const { data: trades, isLoading } = useTrades();

  if (isLoading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-10 w-full" />
        ))}
      </div>
    );
  }

  if (!trades?.length) {
    return (
      <div className="text-center py-16">
        <ArrowRightLeft className="h-8 w-8 mx-auto mb-2 text-muted-foreground/40" />
        <p className="text-sm font-mono text-muted-foreground">
          No trades yet
        </p>
      </div>
    );
  }

  const formatTime = (ts: string): string => {
    const d = new Date(ts);
    return d.toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  };

  const formatDate = (ts: string): string => {
    const d = new Date(ts);
    return d.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
    });
  };

  return (
    <div className="rounded-md border border-border/50 overflow-auto">
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent border-border/50">
            <TableHead className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
              Time
            </TableHead>
            <TableHead className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
              Market
            </TableHead>
            <TableHead className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
              Side
            </TableHead>
            <TableHead className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground text-right">
              Price
            </TableHead>
            <TableHead className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground text-right">
              Size
            </TableHead>
            <TableHead className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
              Strategy
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {trades.map((trade) => (
            <TableRow
              key={trade.id}
              className="hover:bg-secondary/30 border-border/30"
            >
              <TableCell className="font-mono text-xs text-muted-foreground whitespace-nowrap">
                <div>
                  <span className="block">{formatTime(trade.timestamp)}</span>
                  <span className="text-[10px] opacity-60">
                    {formatDate(trade.timestamp)}
                  </span>
                </div>
              </TableCell>
              <TableCell className="text-sm max-w-[180px] truncate">
                <div>
                  <span className="block truncate">{trade.market}</span>
                  <span className="text-[10px] font-mono text-muted-foreground">
                    {trade.outcome}
                  </span>
                </div>
              </TableCell>
              <TableCell>
                <span
                  className={`text-xs font-mono uppercase font-semibold ${
                    trade.side === "BUY" ? "text-success" : "text-danger"
                  }`}
                >
                  {trade.side}
                </span>
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {(trade.price * 100).toFixed(1)}c
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {trade.size}
              </TableCell>
              <TableCell>
                <Badge
                  variant="outline"
                  className={`text-[9px] font-mono uppercase tracking-wider ${
                    strategyColors[trade.strategy] ?? strategyColors.manual
                  }`}
                >
                  {trade.strategy.replace("_", " ")}
                </Badge>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
