import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { usePlaceTrade } from "@/hooks/use-polymarket";
import type { Market } from "@/types/polymarket";
import { toast } from "@/components/ui/use-toast";

interface MarketTradeDialogProps {
  market: Market;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function MarketTradeDialog({
  market,
  open,
  onOpenChange,
}: MarketTradeDialogProps) {
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [outcome, setOutcome] = useState<string>("Yes");
  const [price, setPrice] = useState<string>("");
  const [size, setSize] = useState<string>("");
  const placeTrade = usePlaceTrade();

  const yesPrice = market.outcomePrices?.[0] ?? 0.5;
  const noPrice = market.outcomePrices?.[1] ?? 0.5;

  const selectedPrice = outcome === "Yes" ? yesPrice : noPrice;
  const priceValue = price ? parseFloat(price) : selectedPrice;
  const sizeValue = size ? parseFloat(size) : 0;
  const estimatedCost = priceValue * sizeValue;

  const handleSubmit = () => {
    if (!sizeValue || !priceValue) return;
    placeTrade.mutate(
      {
        marketId: market.id,
        outcome,
        side,
        price: priceValue,
        size: sizeValue,
      },
      {
        onSuccess: () => {
          toast({
            title: "Trade submitted",
            description: `${side} ${sizeValue} ${outcome} @ ${(priceValue * 100).toFixed(0)}c`,
          });
          onOpenChange(false);
        },
        onError: (err) => {
          toast({
            title: "Trade failed",
            description: err.message,
            variant: "destructive",
          });
        },
      }
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="bg-card border-border/50 sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-sm font-medium leading-snug pr-6">
            {market.question}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4 mt-2">
          {/* Side Toggle */}
          <div className="grid grid-cols-2 gap-2">
            <Button
              variant={side === "BUY" ? "default" : "outline"}
              size="sm"
              onClick={() => setSide("BUY")}
              className={
                side === "BUY"
                  ? "bg-success/20 text-success border border-success/30 hover:bg-success/30"
                  : "text-muted-foreground"
              }
            >
              <span className="font-mono text-xs uppercase">Buy</span>
            </Button>
            <Button
              variant={side === "SELL" ? "default" : "outline"}
              size="sm"
              onClick={() => setSide("SELL")}
              className={
                side === "SELL"
                  ? "bg-danger/20 text-danger border border-danger/30 hover:bg-danger/30"
                  : "text-muted-foreground"
              }
            >
              <span className="font-mono text-xs uppercase">Sell</span>
            </Button>
          </div>

          {/* Outcome Toggle */}
          <div className="grid grid-cols-2 gap-2">
            <Button
              variant={outcome === "Yes" ? "default" : "outline"}
              size="sm"
              onClick={() => {
                setOutcome("Yes");
                setPrice("");
              }}
              className={
                outcome === "Yes"
                  ? "bg-success/10 text-success border border-success/20"
                  : "text-muted-foreground"
              }
            >
              <span className="font-mono text-xs">
                Yes {(yesPrice * 100).toFixed(0)}c
              </span>
            </Button>
            <Button
              variant={outcome === "No" ? "default" : "outline"}
              size="sm"
              onClick={() => {
                setOutcome("No");
                setPrice("");
              }}
              className={
                outcome === "No"
                  ? "bg-danger/10 text-danger border border-danger/20"
                  : "text-muted-foreground"
              }
            >
              <span className="font-mono text-xs">
                No {(noPrice * 100).toFixed(0)}c
              </span>
            </Button>
          </div>

          {/* Price */}
          <div>
            <label className="text-[11px] font-mono uppercase tracking-wider text-muted-foreground mb-1 block">
              Price (cents)
            </label>
            <Input
              type="number"
              placeholder={`${(selectedPrice * 100).toFixed(0)}`}
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              className="font-mono bg-secondary/50 border-border/50"
              min={1}
              max={99}
              step={1}
            />
          </div>

          {/* Size */}
          <div>
            <label className="text-[11px] font-mono uppercase tracking-wider text-muted-foreground mb-1 block">
              Size (shares)
            </label>
            <Input
              type="number"
              placeholder="0"
              value={size}
              onChange={(e) => setSize(e.target.value)}
              className="font-mono bg-secondary/50 border-border/50"
              min={1}
              step={1}
            />
          </div>

          {/* Cost estimate */}
          <div className="rounded-md bg-secondary/50 border border-border/50 p-3">
            <div className="flex justify-between text-xs font-mono">
              <span className="text-muted-foreground">Est. Cost</span>
              <span className="text-foreground">
                ${estimatedCost.toFixed(2)}
              </span>
            </div>
            <div className="flex justify-between text-xs font-mono mt-1">
              <span className="text-muted-foreground">Max Payout</span>
              <span className="text-success">
                ${sizeValue.toFixed(2)}
              </span>
            </div>
          </div>

          {/* Submit */}
          <Button
            onClick={handleSubmit}
            disabled={!sizeValue || placeTrade.isPending}
            className={`w-full font-mono uppercase tracking-wider text-xs ${
              side === "BUY"
                ? "bg-success hover:bg-success/80 text-white"
                : "bg-danger hover:bg-danger/80 text-white"
            }`}
          >
            {placeTrade.isPending
              ? "Submitting..."
              : `${side} ${outcome} @ ${(priceValue * 100).toFixed(0)}c`}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
