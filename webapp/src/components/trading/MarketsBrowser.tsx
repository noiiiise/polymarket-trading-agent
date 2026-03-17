import { useState } from "react";
import { Search, TrendingUp, BarChart3 } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useMarkets } from "@/hooks/use-polymarket";
import { MarketTradeDialog } from "./MarketTradeDialog";
import type { Market } from "@/types/polymarket";

export function MarketsBrowser() {
  const [search, setSearch] = useState<string>("");
  const [selectedMarket, setSelectedMarket] = useState<Market | null>(null);
  const { data: markets, isLoading } = useMarkets(
    search || undefined,
    20
  );

  return (
    <div className="space-y-4">
      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
        <Input
          placeholder="Search markets..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="pl-9 bg-secondary/50 border-border/50 font-mono text-sm h-10"
        />
      </div>

      {/* Markets Grid */}
      {isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Card key={i} className="p-4 bg-card/50 border-border/50">
              <Skeleton className="h-4 w-3/4 mb-3" />
              <Skeleton className="h-3 w-1/2 mb-4" />
              <div className="flex gap-2">
                <Skeleton className="h-8 w-20" />
                <Skeleton className="h-8 w-20" />
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {markets?.map((market) => (
            <MarketCard
              key={market.id}
              market={market}
              onClick={() => setSelectedMarket(market)}
            />
          ))}
          {markets?.length === 0 ? (
            <div className="col-span-full text-center py-12 text-muted-foreground">
              <BarChart3 className="h-8 w-8 mx-auto mb-2 opacity-40" />
              <p className="text-sm font-mono">No markets found</p>
            </div>
          ) : null}
        </div>
      )}

      {/* Trade Dialog */}
      {selectedMarket ? (
        <MarketTradeDialog
          market={selectedMarket}
          open={true}
          onOpenChange={(open) => {
            if (!open) setSelectedMarket(null);
          }}
        />
      ) : null}
    </div>
  );
}

function MarketCard({
  market,
  onClick,
}: {
  market: Market;
  onClick: () => void;
}) {
  const yesPrice = market.outcomePrices?.[0] ?? 0;
  const noPrice = market.outcomePrices?.[1] ?? 1 - yesPrice;

  const formatVolume = (v: number): string => {
    if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
    if (v >= 1_000) return `$${(v / 1_000).toFixed(0)}K`;
    return `$${v.toFixed(0)}`;
  };

  return (
    <Card
      className="p-4 bg-card/50 border-border/50 hover:border-primary/30 transition-all cursor-pointer group"
      onClick={onClick}
    >
      <div className="flex items-start gap-3">
        {market.image ? (
          <img
            src={market.image}
            alt=""
            className="h-10 w-10 rounded-md object-cover flex-shrink-0 bg-secondary"
          />
        ) : (
          <div className="h-10 w-10 rounded-md bg-secondary flex items-center justify-center flex-shrink-0">
            <BarChart3 className="h-5 w-5 text-muted-foreground" />
          </div>
        )}
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium leading-snug line-clamp-2 group-hover:text-primary transition-colors">
            {market.question}
          </p>
          <div className="flex items-center gap-2 mt-1.5">
            <TrendingUp className="h-3 w-3 text-muted-foreground" />
            <span className="text-[11px] font-mono text-muted-foreground">
              {formatVolume(market.volume)} vol
            </span>
          </div>
        </div>
      </div>

      {/* Prices */}
      <div className="flex gap-2 mt-3">
        <div className="flex-1 rounded-md bg-success/10 border border-success/20 px-3 py-1.5 text-center">
          <span className="text-[10px] font-mono uppercase text-success/70 block">
            Yes
          </span>
          <span className="text-sm font-mono font-semibold text-success">
            {(yesPrice * 100).toFixed(0)}c
          </span>
        </div>
        <div className="flex-1 rounded-md bg-danger/10 border border-danger/20 px-3 py-1.5 text-center">
          <span className="text-[10px] font-mono uppercase text-danger/70 block">
            No
          </span>
          <span className="text-sm font-mono font-semibold text-danger">
            {(noPrice * 100).toFixed(0)}c
          </span>
        </div>
      </div>

      {/* Tags */}
      {market.tags && market.tags.length > 0 ? (
        <div className="flex gap-1 mt-2 flex-wrap">
          {market.tags.slice(0, 3).map((tag) => (
            <Badge
              key={tag}
              variant="outline"
              className="text-[9px] font-mono px-1.5 py-0"
            >
              {tag}
            </Badge>
          ))}
        </div>
      ) : null}
    </Card>
  );
}
