import { Activity, BarChart3, Layers, ArrowRightLeft, Terminal, Radio } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EngineControls } from "@/components/trading/EngineControls";
import { StatsCards } from "@/components/trading/StatsCards";
import { MarketsBrowser } from "@/components/trading/MarketsBrowser";
import { PositionsTable } from "@/components/trading/PositionsTable";
import { TradeHistory } from "@/components/trading/TradeHistory";
import { ActivityLog } from "@/components/trading/ActivityLog";
import { SignalsFeed } from "@/components/trading/SignalsFeed";
import { OrderRelay } from "@/components/trading/OrderRelay";

const Index = () => {
  return (
    <div className="min-h-screen bg-background text-foreground flex flex-col">
      {/* Top Bar */}
      <header className="border-b border-border/50 bg-card/30 backdrop-blur-sm sticky top-0 z-50">
        <div className="flex items-center justify-between px-4 md:px-6 h-14">
          <div className="flex items-center gap-3">
            <Activity className="h-5 w-5 text-primary" />
            <h1 className="font-syne text-base md:text-lg font-bold tracking-tight">
              <span className="text-primary">POLYMARKET</span>
              <span className="text-muted-foreground ml-1.5 font-medium">
                AGENT
              </span>
            </h1>
          </div>
          <EngineControls />
        </div>
        <OrderRelay />
      </header>

      {/* Main Content */}
      <main className="flex-1 px-4 md:px-6 py-4 md:py-6 space-y-4 md:space-y-6 max-w-[1600px] mx-auto w-full">
        {/* Stats Row */}
        <StatsCards />

        {/* Tabbed Content */}
        <Tabs defaultValue="signals" className="space-y-4">
          <TabsList className="bg-secondary/50 border border-border/50 h-9">
            <TabsTrigger
              value="signals"
              className="text-xs font-mono uppercase tracking-wider gap-1.5 data-[state=active]:bg-primary/10 data-[state=active]:text-primary"
            >
              <Radio className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Signals</span>
            </TabsTrigger>
            <TabsTrigger
              value="markets"
              className="text-xs font-mono uppercase tracking-wider gap-1.5 data-[state=active]:bg-primary/10 data-[state=active]:text-primary"
            >
              <BarChart3 className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Markets</span>
            </TabsTrigger>
            <TabsTrigger
              value="positions"
              className="text-xs font-mono uppercase tracking-wider gap-1.5 data-[state=active]:bg-primary/10 data-[state=active]:text-primary"
            >
              <Layers className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Positions</span>
            </TabsTrigger>
            <TabsTrigger
              value="trades"
              className="text-xs font-mono uppercase tracking-wider gap-1.5 data-[state=active]:bg-primary/10 data-[state=active]:text-primary"
            >
              <ArrowRightLeft className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Trades</span>
            </TabsTrigger>
            <TabsTrigger
              value="logs"
              className="text-xs font-mono uppercase tracking-wider gap-1.5 data-[state=active]:bg-primary/10 data-[state=active]:text-primary"
            >
              <Terminal className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Logs</span>
            </TabsTrigger>
          </TabsList>

          <TabsContent value="signals" className="mt-4">
            <SignalsFeed />
          </TabsContent>

          <TabsContent value="markets" className="mt-4">
            <MarketsBrowser />
          </TabsContent>

          <TabsContent value="positions" className="mt-4">
            <PositionsTable />
          </TabsContent>

          <TabsContent value="trades" className="mt-4">
            <TradeHistory />
          </TabsContent>

          <TabsContent value="logs" className="mt-4">
            <ActivityLog />
          </TabsContent>
        </Tabs>
      </main>

      {/* Footer */}
      <footer className="border-t border-border/30 py-2 px-4 md:px-6">
        <p className="text-[10px] font-mono text-muted-foreground/50 text-center">
          Polymarket Trading Agent &middot; Prediction Markets
        </p>
      </footer>
    </div>
  );
};

export default Index;
