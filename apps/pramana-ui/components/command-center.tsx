"use client";

import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

type Holding = {
  symbol: string;
  market: string;
  assetClass: string;
  quantity: number;
  averageEntry: number;
  markPrice: number;
  markSource: string;
  marketValue: number;
  unrealizedPnl: number;
};

type Portfolio = {
  status: string;
  reason?: string;
  markMode: string;
  markDisclaimer: string;
  cash: number;
  totalEquity: number;
  realizedPnl: number;
  unrealizedPnl: number;
  highWaterMark: number;
  drawdown: number;
  holdings: Holding[];
  equityCurve: Array<{ timestamp: string; equity: number }>;
  updatedAt: string | null;
};

type Agent = {
  agentId: string;
  domain: string;
  stance: string;
  confidence: number;
  expectedReturn: number;
  expectedRisk: number;
};

type Intelligence = {
  status: string;
  regime: string;
  regimeSource: string;
  consensus: string;
  agents: Agent[];
  proof: null | {
    file: string;
    decisionId: string | null;
    generatedAt: string | null;
    subject: string | null;
    rationale: string[];
    stress: Record<string, string>;
    risk: Record<string, string>;
  };
};

type Friction = {
  status: string;
  slippage: number;
  spread: number;
  statutoryFees: number;
  stt: number;
  sec: number;
  gst: number;
  byCode: Record<string, number>;
};

type Trade = {
  orderId: string;
  symbol: string;
  market: string;
  side: string;
  quantity: number;
  fillPrice: number;
  notional: number;
  status: string;
  createdAt: string;
  proofStatus: string;
  proof: null | {
    file: string;
    decisionId: string | null;
    rationale: string[];
    stress: Record<string, string>;
    risk: Record<string, string>;
  };
};

const EMPTY_PORTFOLIO: Portfolio = {
  status: "empty",
  markMode: "ledger_marked",
  markDisclaimer: "No ledger loaded.",
  cash: 0,
  totalEquity: 0,
  realizedPnl: 0,
  unrealizedPnl: 0,
  highWaterMark: 0,
  drawdown: 0,
  holdings: [],
  equityCurve: [],
  updatedAt: null,
};

export function CommandCenter() {
  const [portfolio, setPortfolio] = useState<Portfolio>(EMPTY_PORTFOLIO);
  const [intelligence, setIntelligence] = useState<Intelligence>({
    status: "empty", regime: "UNKNOWN", regimeSource: "not_persisted", consensus: "NO_PROOF", agents: [], proof: null,
  });
  const [friction, setFriction] = useState<Friction>({
    status: "empty", slippage: 0, spread: 0, statutoryFees: 0, stt: 0, sec: 0, gst: 0, byCode: {},
  });
  const [trades, setTrades] = useState<Trade[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [updated, setUpdated] = useState<Date | null>(null);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const [portfolioResponse, intelligenceResponse, frictionResponse, tradesResponse] = await Promise.all([
          fetch("/api/portfolio/mtm", { cache: "no-store" }),
          fetch("/api/intelligence/swarm", { cache: "no-store" }),
          fetch("/api/execution/friction", { cache: "no-store" }),
          fetch("/api/execution/trades", { cache: "no-store" }),
        ]);
        const [p, i, f, t] = await Promise.all([
          portfolioResponse.json(), intelligenceResponse.json(), frictionResponse.json(), tradesResponse.json(),
        ]);
        if (!active) return;
        setPortfolio(p);
        setIntelligence(i);
        setFriction(f);
        setTrades(t.trades ?? []);
        setUpdated(new Date());
      } finally {
        if (active) setLoading(false);
      }
    };
    void load();
    const timer = window.setInterval(load, 15_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const chartData = useMemo(
    () => portfolio.equityCurve.map((point) => ({
      ...point,
      label: new Date(point.timestamp).toLocaleString(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }),
    })),
    [portfolio.equityCurve],
  );

  return (
    <main className="min-h-screen px-4 py-5 md:px-7 xl:px-10">
      <div className="mx-auto max-w-[1800px] space-y-5">
        <header className="flex flex-col gap-4 border-b border-slate-800/90 pb-5 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.26em] text-cyan-300">
              <span className="h-2 w-2 rounded-full bg-cyan-300 shadow-[0_0_14px_rgba(34,211,238,0.9)]" />
              Read-only command center
            </div>
            <h1 className="text-3xl font-semibold tracking-tight text-slate-50">PRAMANA <span className="font-light text-slate-500">/ EXECUTIVE INTELLIGENCE</span></h1>
            <p className="mt-2 max-w-3xl text-sm text-slate-500">Evidence-first observability over paper execution, Multi-Agent System output, friction, and XAI proofs. No trade or risk-control write path exists in this application.</p>
          </div>
          <div className="flex items-center gap-3 text-xs text-slate-500">
            <span className="rounded border border-slate-800 bg-slate-950/70 px-3 py-2">AUTO REFRESH · 15S</span>
            <span className="rounded border border-slate-800 bg-slate-950/70 px-3 py-2">{loading ? "SYNCING" : `UPDATED ${updated?.toLocaleTimeString() ?? "—"}`}</span>
          </div>
        </header>

        <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <MetricCard label="Total Equity" value={money(portfolio.totalEquity)} sub={`Cash ${money(portfolio.cash)}`} accent="cyan" />
          <MetricCard label="Market Regime" value={intelligence.regime} sub={intelligence.regimeSource.replaceAll("_", " ")} accent={intelligence.regime === "UNKNOWN" ? "muted" : "cyan"} />
          <MetricCard label="Current Drawdown" value={percent(portfolio.drawdown)} sub={`HWM ${money(portfolio.highWaterMark)}`} accent={portfolio.drawdown > 0.1 ? "crimson" : "muted"} />
          <MetricCard label="Swarm Consensus" value={intelligence.consensus} sub={`${intelligence.agents.length} specialists reporting`} accent={intelligence.consensus === "BEARISH" ? "crimson" : intelligence.consensus === "BULLISH" ? "cyan" : "muted"} />
        </section>

        {portfolio.status !== "ok" && (
          <div className="rounded border border-amber-400/20 bg-amber-400/[0.04] px-4 py-3 text-sm text-amber-200/80">
            Ledger state: {portfolio.reason ?? portfolio.status}. Configure <code>PRAMANA_LEDGER_PATH</code> to the local paper ledger. Dashboard remains read-only.
          </div>
        )}

        <section className="grid gap-5 xl:grid-cols-[1.7fr_1fr]">
          <Panel title="Equity Curve" eyebrow="LEDGER-MARKED MTM" right={`${chartData.length} observations`}>
            <div className="h-[330px] w-full">
              {chartData.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartData} margin={{ top: 12, right: 12, bottom: 4, left: 0 }}>
                    <CartesianGrid stroke="#1c2735" strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="label" stroke="#526174" tick={{ fontSize: 10 }} tickLine={false} axisLine={false} minTickGap={48} />
                    <YAxis stroke="#526174" tick={{ fontSize: 10 }} tickFormatter={compactMoney} tickLine={false} axisLine={false} width={72} domain={["auto", "auto"]} />
                    <Tooltip contentStyle={{ background: "#091019", border: "1px solid #263445", borderRadius: 4, fontSize: 12 }} formatter={(value) => [money(Number(value)), "Equity"]} labelStyle={{ color: "#8ba0b8" }} />
                    <Line type="monotone" dataKey="equity" stroke="#22d3ee" strokeWidth={2} dot={false} activeDot={{ r: 3 }} />
                  </LineChart>
                </ResponsiveContainer>
              ) : <EmptyState label="No equity observations in the paper ledger" />}
            </div>
            <p className="mt-3 border-t border-slate-800/70 pt-3 text-[11px] leading-5 text-slate-600">{portfolio.markDisclaimer}</p>
          </Panel>

          <Panel title="Swarm Intelligence" eyebrow="LATEST XAI PROOF" right={intelligence.proof?.subject ?? "NO SUBJECT"}>
            <div className="space-y-4">
              {intelligence.agents.length ? intelligence.agents.map((agent) => <AgentBar key={agent.agentId} agent={agent} />) : <EmptyState label="No persisted agent proof available" />}
            </div>
            {intelligence.proof && (
              <div className="mt-5 border-t border-slate-800 pt-4">
                <p className="text-[10px] uppercase tracking-[0.18em] text-slate-600">Atlas rationale</p>
                <ul className="mt-2 space-y-1 text-xs leading-5 text-slate-400">
                  {intelligence.proof.rationale.slice(0, 4).map((item) => <li key={item}>— {item}</li>)}
                </ul>
              </div>
            )}
          </Panel>
        </section>

        <section className="grid gap-5 xl:grid-cols-[1.35fr_1fr]">
          <Panel title="Open Holdings" eyebrow="PAPER LEDGER" right={`${portfolio.holdings.length} positions`}>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-left text-xs">
                <thead className="border-b border-slate-800 text-[10px] uppercase tracking-wider text-slate-600">
                  <tr><th className="pb-3">Instrument</th><th className="pb-3">Qty</th><th className="pb-3">Avg</th><th className="pb-3">Mark</th><th className="pb-3">Value</th><th className="pb-3 text-right">Unrealized</th></tr>
                </thead>
                <tbody className="divide-y divide-slate-900">
                  {portfolio.holdings.map((item) => (
                    <tr key={`${item.market}:${item.symbol}`}>
                      <td className="py-3"><div className="font-medium text-slate-200">{item.symbol}</div><div className="mt-0.5 text-[10px] text-slate-600">{item.market} · {item.assetClass}</div></td>
                      <td className="py-3 text-slate-400">{item.quantity}</td><td className="py-3 text-slate-400">{money(item.averageEntry)}</td>
                      <td className="py-3"><div className="text-slate-300">{money(item.markPrice)}</div><div className="text-[9px] uppercase text-slate-700">{item.markSource.replaceAll("_", " ")}</div></td>
                      <td className="py-3 text-slate-300">{money(item.marketValue)}</td>
                      <td className={`py-3 text-right font-medium ${item.unrealizedPnl < 0 ? "text-crimson" : "text-cyan-300"}`}>{signedMoney(item.unrealizedPnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!portfolio.holdings.length && <EmptyState label="No open paper positions" />}
            </div>
          </Panel>

          <Panel title="Execution Friction" eyebrow="CUMULATIVE DRAG" right={`${Object.keys(friction.byCode).length} cost classes`}>
            <div className="grid grid-cols-2 gap-3">
              <CostBox label="Slippage" value={money(friction.slippage)} />
              <CostBox label="Spread" value={money(friction.spread)} />
              <CostBox label="Statutory" value={money(friction.statutoryFees)} />
              <CostBox label="Realized P&L" value={signedMoney(portfolio.realizedPnl)} negative={portfolio.realizedPnl < 0} />
            </div>
            <div className="mt-5 space-y-2 border-t border-slate-800 pt-4">
              {Object.entries(friction.byCode).sort((a, b) => b[1] - a[1]).map(([code, value]) => (
                <div key={code} className="flex items-center justify-between text-xs"><span className="text-slate-500">{code}</span><span className="font-mono text-slate-300">{money(value)}</span></div>
              ))}
              {!Object.keys(friction.byCode).length && <p className="text-xs text-slate-600">No friction ledger entries.</p>}
            </div>
          </Panel>
        </section>

        <Panel title="XAI Trade Feed" eyebrow="EXECUTION + PROOF" right={`${trades.length} recent fills`}>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1000px] text-left text-xs">
              <thead className="border-b border-slate-800 text-[10px] uppercase tracking-wider text-slate-600">
                <tr><th className="pb-3">Time</th><th className="pb-3">Instrument</th><th className="pb-3">Side</th><th className="pb-3">Qty</th><th className="pb-3">Fill</th><th className="pb-3">Notional</th><th className="pb-3">Proof</th><th className="pb-3 text-right">Detail</th></tr>
              </thead>
              <tbody className="divide-y divide-slate-900">
                {trades.map((trade) => (
                  <TradeRows key={trade.orderId} trade={trade} expanded={expanded === trade.orderId} onToggle={() => setExpanded(expanded === trade.orderId ? null : trade.orderId)} />
                ))}
              </tbody>
            </table>
            {!trades.length && <EmptyState label="No paper fills recorded" />}
          </div>
        </Panel>
      </div>
    </main>
  );
}

function MetricCard({ label, value, sub, accent }: { label: string; value: string; sub: string; accent: "cyan" | "crimson" | "muted" }) {
  const color = accent === "cyan" ? "text-cyan-300" : accent === "crimson" ? "text-crimson" : "text-slate-100";
  return <div className="rounded border border-slate-800 bg-panel/80 p-4 shadow-[0_10px_45px_rgba(0,0,0,0.18)]"><div className="text-[10px] font-medium uppercase tracking-[0.18em] text-slate-600">{label}</div><div className={`mt-2 truncate text-2xl font-semibold tracking-tight ${color}`}>{value}</div><div className="mt-1 text-[11px] text-slate-600">{sub}</div></div>;
}

function Panel({ title, eyebrow, right, children }: { title: string; eyebrow: string; right?: string; children: React.ReactNode }) {
  return <section className="rounded border border-slate-800/90 bg-panel/75 p-4 shadow-[0_14px_55px_rgba(0,0,0,0.2)] md:p-5"><div className="mb-5 flex items-end justify-between gap-4"><div><div className="text-[9px] font-semibold uppercase tracking-[0.2em] text-cyan-400/70">{eyebrow}</div><h2 className="mt-1 text-base font-medium text-slate-200">{title}</h2></div>{right && <div className="max-w-[45%] truncate text-[10px] uppercase tracking-wide text-slate-600">{right}</div>}</div>{children}</section>;
}

function AgentBar({ agent }: { agent: Agent }) {
  const bearish = agent.stance.includes("SELL") || agent.stance === "AVOID";
  return <div><div className="mb-1.5 flex items-center justify-between gap-3"><div><div className="text-xs font-medium text-slate-300">{agent.agentId}</div><div className="text-[9px] uppercase tracking-wider text-slate-700">{agent.domain}</div></div><div className={`text-[10px] font-semibold ${bearish ? "text-crimson" : agent.stance.includes("BUY") ? "text-cyan-300" : "text-slate-500"}`}>{agent.stance} · {percent(agent.confidence)}</div></div><div className="h-1.5 overflow-hidden rounded-full bg-slate-900"><div className={`h-full ${bearish ? "bg-crimson" : "bg-cyan-400"}`} style={{ width: `${Math.min(100, agent.confidence * 100)}%` }} /></div></div>;
}

function CostBox({ label, value, negative = false }: { label: string; value: string; negative?: boolean }) {
  return <div className="border border-slate-800 bg-slate-950/40 p-3"><div className="text-[9px] uppercase tracking-wider text-slate-600">{label}</div><div className={`mt-1 font-mono text-sm ${negative ? "text-crimson" : "text-slate-200"}`}>{value}</div></div>;
}

function TradeRows({ trade, expanded, onToggle }: { trade: Trade; expanded: boolean; onToggle: () => void }) {
  return <>
    <tr className="transition hover:bg-slate-950/45">
      <td className="py-3 text-slate-500">{new Date(trade.createdAt).toLocaleString()}</td><td className="py-3"><span className="font-medium text-slate-200">{trade.symbol}</span><span className="ml-2 text-[9px] text-slate-700">{trade.market}</span></td>
      <td className={`py-3 font-semibold ${trade.side === "BUY" ? "text-cyan-300" : "text-crimson"}`}>{trade.side}</td><td className="py-3 text-slate-400">{trade.quantity}</td><td className="py-3 font-mono text-slate-300">{money(trade.fillPrice)}</td><td className="py-3 font-mono text-slate-400">{money(trade.notional)}</td>
      <td className="py-3"><span className={`rounded border px-2 py-1 text-[9px] uppercase tracking-wide ${trade.proof ? "border-cyan-500/30 bg-cyan-500/5 text-cyan-300" : "border-slate-800 text-slate-600"}`}>{trade.proof ? "Exact proof" : "No exact link"}</span></td>
      <td className="py-3 text-right"><button onClick={onToggle} className="rounded border border-slate-800 px-2 py-1 text-[10px] uppercase tracking-wide text-slate-500 transition hover:border-slate-600 hover:text-slate-300">{expanded ? "Close" : "Inspect"}</button></td>
    </tr>
    {expanded && <tr><td colSpan={8} className="bg-slate-950/50 p-4"><ProofDetail trade={trade} /></td></tr>}
  </>;
}

function ProofDetail({ trade }: { trade: Trade }) {
  if (!trade.proof) return <div><div className="text-xs font-medium text-slate-300">Pramana Proof unavailable for this fill</div><p className="mt-2 max-w-3xl text-xs leading-5 text-slate-600">The current XAI schema does not persist the broker order ID, so this UI refuses to associate a rationale by timestamp or symbol guesswork. The fill remains visible, but no proof is claimed.</p><div className="mt-3 font-mono text-[10px] text-slate-700">{trade.orderId}</div></div>;
  return <div className="grid gap-5 lg:grid-cols-3"><div><ProofLabel>Declared rationale</ProofLabel><ul className="mt-2 space-y-1 text-xs leading-5 text-slate-400">{trade.proof.rationale.map((item) => <li key={item}>— {item}</li>)}</ul></div><div><ProofLabel>Stress verdict</ProofLabel><KeyValues values={trade.proof.stress} /></div><div><ProofLabel>Risk verdict</ProofLabel><KeyValues values={trade.proof.risk} /></div></div>;
}

function ProofLabel({ children }: { children: React.ReactNode }) { return <div className="text-[9px] font-semibold uppercase tracking-[0.18em] text-cyan-400/70">{children}</div>; }
function KeyValues({ values }: { values: Record<string, string> }) { return <div className="mt-2 space-y-1">{Object.entries(values).map(([key, value]) => <div key={key} className="flex gap-3 text-xs"><span className="min-w-28 text-slate-600">{key}</span><span className="font-mono text-slate-300">{value}</span></div>)}</div>; }
function EmptyState({ label }: { label: string }) { return <div className="flex min-h-24 items-center justify-center border border-dashed border-slate-800 text-xs text-slate-700">{label}</div>; }
function money(value: number) { return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2, minimumFractionDigits: 2 }).format(Number.isFinite(value) ? value : 0); }
function compactMoney(value: number) { return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(Number.isFinite(value) ? value : 0); }
function signedMoney(value: number) { return `${value >= 0 ? "+" : ""}${money(value)}`; }
function percent(value: number) { return `${(Number.isFinite(value) ? value * 100 : 0).toFixed(2)}%`; }
