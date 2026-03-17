import {
  Wallet,
  TrendingUp,
  BarChart3,
  Activity,
  ArrowUpRight,
  ArrowDownRight,
  Layers,
} from "lucide-react";
import { Card } from "@/components/ui/card";
import { useEngineStatus } from "@/hooks/use-polymarket";

interface StatCardProps {
  label: string;
  value: string;
  icon: React.ReactNode;
  trend?: "up" | "down" | "neutral";
}

function StatCard({ label, value, icon, trend = "neutral" }: StatCardProps) {
  return (
    <Card className="bg-card/50 border-border/50 p-4 hover:border-primary/20 transition-colors">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2 text-muted-foreground">
          {icon}
          <span className="text-[11px] font-mono uppercase tracking-wider">
            {label}
          </span>
        </div>
        {trend === "up" ? (
          <ArrowUpRight className="h-3.5 w-3.5 text-success" />
        ) : trend === "down" ? (
          <ArrowDownRight className="h-3.5 w-3.5 text-danger" />
        ) : null}
      </div>
      <p
        className={`mt-2 text-xl font-mono font-semibold tracking-tight ${
          trend === "up"
            ? "text-success"
            : trend === "down"
            ? "text-danger"
            : "text-foreground"
        }`}
      >
        {value}
      </p>
    </Card>
  );
}

function formatCurrency(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

function formatPercent(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(1)}%`;
}

export function StatsCards() {
  const { data: status } = useEngineStatus();

  const balance = status?.balance ?? 0;
  const exposure = status?.exposurePct ?? 0;
  const pnl = status?.pnl ?? 0;
  const positions = status?.positionCount ?? 0;
  const trades = status?.tradeCount ?? 0;

  const pnlTrend = pnl > 0 ? "up" : pnl < 0 ? "down" : "neutral";

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
      <StatCard
        label="Balance"
        value={formatCurrency(balance)}
        icon={<Wallet className="h-3.5 w-3.5" />}
      />
      <StatCard
        label="Exposure"
        value={formatPercent(exposure)}
        icon={<BarChart3 className="h-3.5 w-3.5" />}
        trend={exposure > 50 ? "down" : "neutral"}
      />
      <StatCard
        label="P&L"
        value={formatCurrency(pnl)}
        icon={<TrendingUp className="h-3.5 w-3.5" />}
        trend={pnlTrend as "up" | "down" | "neutral"}
      />
      <StatCard
        label="Positions"
        value={String(positions)}
        icon={<Layers className="h-3.5 w-3.5" />}
      />
      <StatCard
        label="Trades"
        value={String(trades)}
        icon={<Activity className="h-3.5 w-3.5" />}
      />
    </div>
  );
}
