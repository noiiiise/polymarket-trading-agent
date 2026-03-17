import { Layers } from "lucide-react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { usePositions } from "@/hooks/use-polymarket";

export function PositionsTable() {
  const { data: positions, isLoading } = usePositions();

  if (isLoading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-10 w-full" />
        ))}
      </div>
    );
  }

  if (!positions?.length) {
    return (
      <div className="text-center py-16">
        <Layers className="h-8 w-8 mx-auto mb-2 text-muted-foreground/40" />
        <p className="text-sm font-mono text-muted-foreground">
          No open positions
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-md border border-border/50 overflow-auto">
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent border-border/50">
            <TableHead className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
              Market
            </TableHead>
            <TableHead className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
              Side
            </TableHead>
            <TableHead className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground text-right">
              Size
            </TableHead>
            <TableHead className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground text-right">
              Entry
            </TableHead>
            <TableHead className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground text-right">
              Current
            </TableHead>
            <TableHead className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground text-right">
              P&L
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {positions.map((pos) => (
            <TableRow
              key={pos.id}
              className="hover:bg-secondary/30 border-border/30"
            >
              <TableCell className="text-sm max-w-[200px] truncate">
                <div>
                  <span className="block truncate">{pos.market}</span>
                  <span className="text-[10px] font-mono text-muted-foreground">
                    {pos.outcome}
                  </span>
                </div>
              </TableCell>
              <TableCell>
                <span
                  className={`text-xs font-mono uppercase font-semibold ${
                    pos.side === "long" ? "text-success" : "text-danger"
                  }`}
                >
                  {pos.side}
                </span>
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {pos.size}
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {(pos.avgEntryPrice * 100).toFixed(1)}c
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {(pos.currentPrice * 100).toFixed(1)}c
              </TableCell>
              <TableCell className="text-right">
                <div>
                  <span
                    className={`font-mono text-sm font-semibold ${
                      pos.pnl >= 0 ? "text-success" : "text-danger"
                    }`}
                  >
                    {pos.pnl >= 0 ? "+" : ""}${pos.pnl.toFixed(2)}
                  </span>
                  <span
                    className={`block text-[10px] font-mono ${
                      pos.pnlPct >= 0
                        ? "text-success/70"
                        : "text-danger/70"
                    }`}
                  >
                    {pos.pnlPct >= 0 ? "+" : ""}
                    {pos.pnlPct.toFixed(1)}%
                  </span>
                </div>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
