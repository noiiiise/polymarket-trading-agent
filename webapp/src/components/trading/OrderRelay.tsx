import { CheckCircle2 } from "lucide-react";

/**
 * Orders are now placed directly via the backend CLOB proxy.
 * No browser relay needed.
 */
export function OrderRelay() {
  return (
    <div className="flex items-center gap-2">
      <div className="flex items-center gap-1.5 text-[11px] font-mono text-green-400">
        <CheckCircle2 className="h-3 w-3" />
        <span>Direct Mode</span>
      </div>
      <span className="text-[10px] font-mono text-muted-foreground/70">
        Orders placed via CLOB proxy
      </span>
    </div>
  );
}
