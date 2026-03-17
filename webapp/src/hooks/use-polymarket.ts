import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  EngineStatus,
  Market,
  Position,
  TradeRecord,
  LogEntry,
  TradeRequest,
  TradeSignal,
} from "@/types/polymarket";

export function useEngineStatus() {
  return useQuery<EngineStatus>({
    queryKey: ["polymarket", "status"],
    queryFn: () => api.get<EngineStatus>("/api/polymarket/status"),
    refetchInterval: 3000,
    retry: 1,
  });
}

export function useMarkets(query?: string, limit: number = 20) {
  const params = new URLSearchParams();
  if (query) params.set("query", query);
  params.set("limit", String(limit));

  return useQuery<Market[]>({
    queryKey: ["polymarket", "markets", query, limit],
    queryFn: () =>
      api.get<Market[]>(`/api/polymarket/markets?${params.toString()}`),
    retry: 1,
  });
}

export function usePositions() {
  const { data: status } = useEngineStatus();
  return useQuery<Position[]>({
    queryKey: ["polymarket", "positions"],
    queryFn: () => api.get<Position[]>("/api/polymarket/positions"),
    refetchInterval: status?.state === "running" ? 5000 : false,
    retry: 1,
  });
}

export function useTrades() {
  return useQuery<TradeRecord[]>({
    queryKey: ["polymarket", "trades"],
    queryFn: () => api.get<TradeRecord[]>("/api/polymarket/trades"),
    refetchInterval: 10000,
    retry: 1,
  });
}

export function useLogs(limit: number = 200) {
  const { data: status } = useEngineStatus();
  return useQuery<LogEntry[]>({
    queryKey: ["polymarket", "logs", limit],
    queryFn: () => api.get<LogEntry[]>(`/api/polymarket/logs?limit=${limit}`),
    refetchInterval: status?.state === "running" ? 5000 : false,
    retry: 1,
  });
}

export function useStartEngine() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<EngineStatus>("/api/polymarket/start"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["polymarket", "status"] });
    },
  });
}

export function useStopEngine() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<EngineStatus>("/api/polymarket/stop"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["polymarket", "status"] });
    },
  });
}

export function usePlaceTrade() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (trade: TradeRequest) =>
      api.post<TradeRecord>("/api/polymarket/trade", trade),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["polymarket", "trades"] });
      queryClient.invalidateQueries({ queryKey: ["polymarket", "positions"] });
      queryClient.invalidateQueries({ queryKey: ["polymarket", "status"] });
    },
  });
}

export function useSignals(limit: number = 50) {
  const { data: status } = useEngineStatus();
  return useQuery<TradeSignal[]>({
    queryKey: ["polymarket", "signals", limit],
    queryFn: () =>
      api.get<TradeSignal[]>(`/api/polymarket/signals?limit=${limit}`),
    refetchInterval: status?.state === "running" ? 5000 : false,
    retry: 1,
  });
}

export function useAddSignal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (signal: { market: string; note: string }) =>
      api.post<TradeSignal>("/api/polymarket/signals", signal),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["polymarket", "signals"] });
    },
  });
}
