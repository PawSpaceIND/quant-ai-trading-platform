"use client";
import { providerFailures } from "@/lib/specialist-participation";
import {ExternalAccountPanel} from "./external-account";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {ageWorkspace,sourceAge,within} from "@/lib/freshness";
import {EngineFeedStatus} from "./engine-feed-status";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { portfolioRisk } from "@/lib/portfolio-risk";
import { MarketWorkspace } from "./market-workspace";
import { CopilotPanel } from "./copilot-panel";
import {BenchmarkAttributionPanel} from "./benchmark-attribution";
import { ResearchComparison } from "./research-comparison";
import { ResearchLabPanel } from "./research-lab-panel";
import {instrumentIdentity} from "../lib/symbols";
import {CompanyEventsPanel} from "./company-events";
import { ResearchPortfolio } from "./research-portfolio";
import {PaperContribution} from "./paper-contribution";
import type {BrokerSelection} from "@/lib/broker-lifecycle";
import {BrokerObservation} from "./broker-observation";
import {PaperOmsPanel} from "./paper-oms";
import {InstitutionalRecoveryPanel} from "./institutional-recovery";
import {RunComparison} from "./run-comparison";
import {AccountBenchmark} from "./account-benchmark";
import {HistoricalRisk} from "./historical-risk";
import {DecisionQuality} from "./decision-quality";
import {GateRefusals, ProtectiveState} from "./protective-state";
import {protectionAlert} from "@/lib/protection-sweep";
import type { Workspace, Portfolio, Trade, Friction, DecisionProvenance } from "@/lib/types";
const hosted = process.env.NEXT_PUBLIC_PRAMANA_HOSTED === "true";
const sections = [
  { id: "overview", name: "Overview", icon: "◫" },
  { id: "markets", name: "Markets", icon: "⌁" },
  { id: "portfolio", name: "Portfolio", icon: "▥" },
  { id: "risk", name: "Risk lab", icon: "◇" },
  { id: "research", name: "Research", icon: "◴" },
  { id: "quality", name: "Decision quality", icon: "◔" },
  { id: "activity", name: "Activity", icon: "≡" },
];
const money = (v: number | undefined) =>
  v === undefined
    ? "—"
    : new Intl.NumberFormat("en-IN", {
        maximumFractionDigits: 2,
        minimumFractionDigits: 2,
      }).format(v);
const pct = (v: number | undefined | null) =>
  v == null ? "—" : `${(v * 100).toFixed(2)}%`;
async function api(url: string, options?: RequestInit) {
  const r = await fetch(url, {
    cache: "no-store",
    signal: AbortSignal.timeout(12000),
    ...options,
  });
  if (r.status === 401) {
    window.location.assign("/login");
    throw new Error("Session expired");
  }
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || "Request failed");
  return d;
}
function download(name: string, content: string, type = "application/json") {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  // An anchor that was never inserted is ignored by embedded browsers that only honour
  // in-document activation, so the click silently produces no download at all.
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function PilotWorkspace() {
  const [view, setView] = useState("overview");
  const [snapshot, setData] = useState<Workspace | null>(null);
  const [clock,setClock]=useState(()=>Date.now());
  const data=useMemo(()=>snapshot?ageWorkspace(snapshot,clock):null,[snapshot,clock]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [friction, setFriction] = useState<Friction | null>(null);
  const [favorites, setFavorites] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [chat, setChat] = useState(false);
  const [draft, setDraft] = useState("");
  const [brokerCapture,setBrokerCapture]=useState<BrokerSelection|undefined>(undefined);
  const [runComparisonSha256,setRunComparisonSha256]=useState<string|undefined>(undefined);
  const [companyAsOf, setCompanyAsOf] = useState<string | undefined>();
  const [halt, setHalt] = useState(false);
  const [reason, setReason] = useState("");
  const [controlBusy, setControlBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [notice, setNotice] = useState("");
  const [signingOut, setSigningOut] = useState(false);
  const [controlError, setControlError] = useState("");
  async function signOut() {
    if (signingOut) return;
    setSigningOut(true);
    try {
      const response = await fetch("/api/session", { method: "DELETE", signal: AbortSignal.timeout(12000) });
      if (!response.ok) throw new Error("Sign out failed");
      window.location.assign("/login");
    } catch {
      setNotice("Sign out could not be confirmed. Your session may still be active; try Sign out again.");
      setSigningOut(false);
    }
  }
  const inFlight = useRef(false);
  const haltTrigger = useRef<HTMLButtonElement>(null);
  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setRefreshing(true);
    try {
      const results = await Promise.allSettled([
        api("/api/workspace"),
        api("/api/execution/trades"),
        api("/api/execution/friction"),
        api("/api/watchlist"),
      ]);
      const [w,t,f,p]=results;
      if(w.status==="fulfilled")setData(w.value);
      if(t.status==="fulfilled")setTrades(t.value.trades);
      if(f.status==="fulfilled")setFriction(f.value);
      if(p.status==="fulfilled")setFavorites(p.value.symbols);
      const labels=["Workspace","Trade history","Execution costs","Watchlist"];
      setError(results.flatMap((r,i)=>r.status==="rejected"?[`${labels[i]}: ${r.reason instanceof Error?r.reason.message:"refresh failed"}`]:[]).join(". "));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Connection failed");
    } finally {
      setLoading(false);
      setRefreshing(false);
      inFlight.current = false;
    }
  }, []);
  useEffect(() => {
    const v = new URLSearchParams(window.location.search).get("view");
    if (v && sections.some((s) => s.id === v)) setView(v);
    if (!hosted && new URLSearchParams(window.location.search).has("chat"))
      setChat(true);
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [refresh]);
  useEffect(()=>{
    const wall=Date.now(),monotonic=performance.now();
    const advance=()=>setClock(previous=>Math.max(previous,Date.now(),wall+Math.floor(performance.now()-monotonic)));
    const timer=setInterval(advance,1000);
    document.addEventListener("visibilitychange",advance);
    return ()=>{clearInterval(timer);document.removeEventListener("visibilitychange",advance);};
  },[]);
  function navigate(id: string) {
    setView(id);
    const url = new URL(window.location.href);
    url.searchParams.set("view", id);
    window.history.replaceState(null, "", url);
  }
  function ask(q: string, eventCutoff?: string, broker?:BrokerSelection, comparisonSha?:string) {
    setRunComparisonSha256(comparisonSha);
    setCompanyAsOf(eventCutoff);
    setBrokerCapture(broker);
    if (hosted) {
      setNotice(
        "This hosted snapshot is read-only. Use the authenticated engine workspace for Atlas and operator controls.",
      );
      return;
    }
    setDraft(q);
    setChat(true);
  }
  async function save(symbols: string[]) {
    if (hosted)
      throw new Error(
        "Saved watchlist editing is available in the engine workspace.",
      );
    const r = await api("/api/watchlist", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols }),
    });
    setFavorites(r.symbols);
  }
  async function requestHalt() {
    setControlBusy(true);
    setControlError("");
    try {
      const r = await api("/api/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "halt", reason }),
      });
      setNotice(r.message);
      setHalt(false);
      setReason("");
      await refresh();
    } catch (e) {
      setControlError(
        `${e instanceof Error ? e.message : "Request failed"} The halt was not acknowledged. Entries are not halted.`,
      );
    } finally {
      setControlBusy(false);
    }
  }
  useEffect(() => {
    if (!halt) return;
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !controlBusy) setHalt(false);
      if (event.key !== "Tab") return;
      const items = Array.from(
        document.querySelectorAll<HTMLElement>(
          '[role="dialog"] button:not(:disabled), [role="dialog"] textarea',
        ),
      );
      const first = items[0],
        last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", handler);
    return () => {
      document.removeEventListener("keydown", handler);
      haltTrigger.current?.focus();
    };
  }, [halt, controlBusy]);
  const title = sections.find((s) => s.id === view)?.name || "Overview";
  const p = data?.portfolio;
  // Recomputed on the same clock the rest of the workspace ages against, so a sweep that
  // has fallen out of its ten-second window reads as unverified here too rather than
  // keeping a stale all-clear on screen.
  const protection = useMemo(() => data ? protectionAlert(data.runtime, data.tenantId, clock) : null, [data, clock]);
  return (
    <div className="app-shell">
      <aside className="sidebar" inert={halt}>
        <a className="brand" href="/">
          <span className="brand-symbol">P</span>
          <span>
            PRAMANA<small>RESEARCH WORKSPACE</small>
          </span>
        </a>
        <div className="workspace-label">
          <span className="workspace-avatar">F</span>
          <div>
            Founder workspace<small>Private pilot</small>
          </div>
          <span className="pill green">PAPER</span>
        </div>
        <nav aria-label="Main navigation">
          {sections.map((s) => (
            <button
              key={s.id}
              className={view === s.id ? "active" : ""}
              onClick={() => navigate(s.id)}
              aria-current={view === s.id ? "page" : undefined}
            >
              <span aria-hidden>{s.icon}</span>
              {s.name}
              {s.id === "research" && <small>GATES</small>}
            </button>
          ))}
          {!hosted && <button className="mobile-sign-out" disabled={signingOut} onClick={() => void signOut()}>{signingOut ? "Signing out…" : "↪ Sign out"}</button>}
        </nav>
        <div className="sidebar-bottom">
          <div className="pilot-card">
            <span className="eyebrow">CAPITAL PRESERVATION FIRST</span>
            <strong>Observe. Validate. Improve.</strong>
            <p>
              Real market evidence.
              <br />
              Simulated execution.
            </p>
          </div>
          <button
            hidden={hosted}
            className="sign-out"
            disabled={signingOut}
            onClick={() => void signOut()}
          >
            {signingOut ? "Signing out…" : "↪ Sign out"}
          </button>
          <small>PRAMANA · PILOT EDITION</small>
        </div>
      </aside>
      <div className="main-shell" inert={halt}>
        <header className="topbar">
          <div className="breadcrumb">
            Workspace <span>/</span> <strong>{title}</strong>
          </div>
          <div className="topbar-actions">
            <span className="pill neutral">INR · NSE CASH</span>
            <button
              onClick={() => void refresh()}
              aria-label="Refresh workspace"
              disabled={refreshing}
            >
              ↻
            </button>
            <button
              className={chat ? "selected" : ""}
              onClick={() => setChat(!chat)}
              disabled={hosted}
              title={
                hosted
                  ? "Copilot runs in the authenticated engine workspace"
                  : undefined
              }
              aria-expanded={chat}
            >
              ✳ Atlas copilot
            </button>
            <button
              disabled={hosted}
              title={
                hosted
                  ? "Operator controls run in the engine workspace"
                  : undefined
              }
              ref={haltTrigger}
              className="halt-button"
              onClick={() => {
                setControlError("");
                setHalt(true);
              }}
            >
              Halt entries
            </button>
          </div>
        </header>
        <main id="main-content">
          <div className="page-heading">
            <div>
              <span className="eyebrow">
                {new Date().toLocaleDateString("en-IN", {
                  day: "numeric",
                  month: "long",
                  year: "numeric",
                })}{" "}
                · PAPER TRADING
              </span>
              <h1>
                {view === "overview"
                  ? "A clear view of your next move."
                  : title}
              </h1>
              <p>
                {view === "overview"
                  ? "Your portfolio, market evidence and risk controls. In one place."
                  : view === "markets"
                    ? "Explore your market universe. Save instruments and investigate the evidence."
                    : view === "risk"
                      ? "Explore hypothetical shocks before making a decision."
                      : view === "research"
                        ? "Measure what works. Keep unverified claims out of your launch."
                        : view === "quality"
                          ? "Judge the paper AI by calibration and outcomes, not by eye."
                          : view === "activity"
                            ? "Trace paper fills back to the decisions and controls behind them."
                            : "Position-level valuations with visible sources and freshness."}
              </p>
            </div>
            <div className="connection">
              <span
                className={`dot ${data?.runtime.status === "running" && !error ? "" : "amber-dot"}`}
              />
              {error
                ? "Connection degraded"
                : data?.runtime.status === "running"
                  ? "Protection heartbeat active"
                  : "Engine not verified"}
              <small>
                {data
                  ? `Updated ${new Date(data.generatedAt).toLocaleTimeString()}`
                  : "Connecting…"}
              </small>
            </div>
          </div>
          {error && (
            <div className="banner error" role="alert">
              {error}. Last displayed data may be stale.{" "}
              <button onClick={() => void refresh()} disabled={refreshing}>
                {refreshing ? "Retrying…" : "Retry"}
              </button>
            </div>
          )}
          {snapshot&&!within(sourceAge(snapshot.generatedAt,clock),30,5)?<div className="banner warning" role="status">Workspace evidence expired. Current readiness and valuation claims are withheld until a successful refresh. Historical research remains dated evidence.</div>:null}
          {notice && (
            <div className="banner" role="status">
              {notice}
              <button
                aria-label="Dismiss notification"
                onClick={() => setNotice("")}
              >
                ×
              </button>
            </div>
          )}
          {data && (data.haltRequested || data.runtime.halted) && (
            <div className="banner warning">
              {data.runtime.halted
                ? `${hosted ? "Last published halt" : "Engine halt acknowledged"}: ${data.runtime.haltReason || "operator halt"}`
                : "Halt requested — waiting for engine acknowledgement."}{" "}
              {hosted ? "Current engine state is not verified by this snapshot." : "Protective exits remain enabled. Resume requires operator review through the CLI."}
            </div>
          )}
          {protection?.severity === "exposed" && (
            <div className="banner warning protection-banner" role="alert">
              <strong>{protection.headline}.</strong> {protection.detail}
            </div>
          )}
          {loading && !data ? (
            <div className="loading-state" role="status">
              <span className="atlas-orb">✳</span>Loading your workspace…
            </div>
          ) : !data ? (
            <div className="empty">
              Workspace unavailable. Check server storage and try again.
            </div>
          ) : (
            <div className={chat ? "content-with-chat" : "content-full"}>
              <div className="workspace-content">
                {(view === "overview" || view === "portfolio") && (
                  <>
                    {p!.status === "invalid" ? <section className="panel"><h2>Portfolio totals withheld</h2><p className="research-notice">{p!.markDisclaimer}</p><p className="muted">Current equity, P&amp;L and risk totals cannot be established from these records.</p></section> : <>
                    <div className="metric-grid">
                      <Metric
                        label="Portfolio equity"
                        value={money(p!.totalEquity)}
                        unit={p!.currency || "ACCOUNT"}
                        note={
                          p!.status === "ok"
                            ? p!.markMode.replaceAll("_", " ")
                            : "Valuation not current"
                        }
                      />
                      <Metric
                        label="Realized P&L"
                        value={`${p!.realizedPnl >= 0 ? "+" : ""}${money(p!.realizedPnl)}`}
                        positive={p!.realizedPnl >= 0}
                        note="After recorded cash fees"
                      />
                      <Metric
                        label="Unrealized P&L"
                        value={`${p!.unrealizedPnl >= 0 ? "+" : ""}${money(p!.unrealizedPnl)}`}
                        positive={p!.unrealizedPnl >= 0}
                        note="Based on displayed marks"
                      />
                      <Metric
                        label="Drawdown"
                        value={pct(p!.drawdown)}
                        note={`From peak ${money(p!.highWaterMark)}`}
                      />
                    </div>
                    <div className="overview-grid">
                      <EquityChart portfolio={p!} />
                      <section className="panel risk-summary">
                        <span className="eyebrow">PORTFOLIO GUARDRAILS</span>
                        <h2>Risk at a glance</h2>
                        <RiskBar
                          label="Drawdown"
                          value={p!.drawdown}
                          limit={data.runtime.limits?.drawdown ?? 0.1}
                        />
                        <RiskBar
                          label="Gross exposure"
                          value={
                            p!.totalEquity > 0
                              ? p!.holdings.reduce(
                                  (s, h) => s + h.marketValue,
                                  0,
                                ) / p!.totalEquity
                              : 0
                          }
                          limit={data.runtime.limits?.grossExposure ?? 0.6}
                        />
                        <div className="details">
                          <div>
                            <dt>Open positions</dt>
                            <dd>
                              {p!.holdings.length} /{" "}
                              {data.runtime.limits?.maxPositions ?? "—"}
                            </dd>
                          </div>
                          <div>
                            <dt>Cash reserve</dt>
                            <dd>{money(p!.cash)}</dd>
                          </div>
                        </div>
                        <button
                          className="wide-button"
                          onClick={() => navigate("risk")}
                        >
                          Open risk lab →
                        </button>
                      </section>
                    </div>
                    </>}
                  </>
                )}
                {view === "overview" && (
                  <>
                    {protection && <ProtectiveState runtime={data.runtime} tenant={data.tenantId} alert={protection} />}
                    <GateRefusals state={data.gateRefusals} />
                    <MarketWorkspace
                      readOnly={hosted}
                      compact
                      data={data.market}
                      favorites={favorites}
                      onSave={save}
                      onAsk={ask}
                    />
                    <div className="overview-grid">
                      <IntelligencePanel data={data} onAsk={ask} />
                      <News data={data} />
                    </div>
                  </>
                )}
                {view === "markets" && (
                  <>
                    {!hosted && <EngineFeedStatus runtime={data.runtime} onAsk={ask} />}
                    {!hosted && <StreamIntegrity data={data} onAsk={ask} />}
                    <MarketWorkspace
                      readOnly={hosted}
                      data={data.market}
                      favorites={favorites}
                      onSave={save}
                      onAsk={ask}
                    />
                    <News data={data} />
                    {!hosted && <CompanyEventsPanel state={data.companyEvents} symbols={data.market.rows.map(instrumentIdentity).filter(s => /^NSE:[A-Z0-9][A-Z0-9&._-]{0,35}$/.test(s))} favorites={favorites} onAsk={ask} onRefresh={refresh} />}
                  </>
                )}
                {view === "portfolio" && (
                  <>
                    {!hosted && <PaperContribution state={data.paperContribution} onAsk={ask} />}
                    {!hosted && <AccountBenchmark state={data.benchmarkPerformance} onAsk={ask} />}
                    {p!.status !== "invalid" && <><Holdings portfolio={p!} onAsk={ask} /><CostPanel friction={friction} /></>}
                  </>
                )}
                {view === "risk" && <RiskLab data={data} onAsk={ask} />}
                {view === "research" && (
                  <>
                    <ResearchPanel data={data} />
                    <BenchmarkAttributionPanel state={data.benchmarkAttribution} onAsk={ask} />
                    <ResearchComparison state={data.researchLab} onAsk={ask} />
                    <ResearchLabPanel snapshot={data.researchDashboard?.snapshot} now={data.generatedAt} />
                    <ResearchPortfolio state={data.researchPortfolio} onAsk={ask} />
                    {!hosted && <RunComparison key={data.runComparison?.sha256} state={data.runComparison} onAsk={(prompt,sha)=>ask(prompt,undefined,undefined,sha)} />}
                    <TradeEvidencePanel data={data} onAsk={ask} />
                    <section className="panel">
                      <div className="panel-title">
                        <div>
                          <span className="eyebrow">ACTUAL PAPER EQUITY</span>
                          <h2>Performance evidence</h2>
                        </div>
                        <button
                          onClick={() =>
                            download(
                              "pramana-evidence-snapshot.json",
                              JSON.stringify(
                                {
                                  asOf: data.generatedAt,
                                  performance: data.performance,
                                  checks: data.checks,
                                  runtime: data.runtime,
                                },
                                null,
                                2,
                              ),
                            )
                          }
                        >
                          Export evidence ↓
                        </button>
                      </div>
                      {data.performance.status === "invalid_observations" ? (
                        <p className="footnote" role="status">Performance unavailable: retained observations contain invalid clocks, payloads or starting balances, or exceed the supported history bounds. Review the source history before using these metrics.</p>
                      ) : null}
                      <div className="metric-grid research-metrics">
                        <Metric
                          label="Observed days"
                          value={String(data.performance.days)}
                          note="Completed sessions with ≥300 fresh minutes"
                        />
                        <Metric
                          label="Net return"
                          value={pct(data.performance.netReturn)}
                          note="Since recorded starting capital"
                        />
                        <Metric
                          label="Sharpe"
                          value={data.performance.sharpe?.toFixed(2) ?? "—"}
                          note="Requires 20 qualifying daily returns"
                        />
                        <Metric
                          label="Sortino"
                          value={data.performance.sortino?.toFixed(2) ?? "—"}
                          note="Requires downside observations"
                        />
                      </div>
                      <p className="footnote">
                        Daily account-return metrics, annualized using 252
                        sessions and a zero risk-free assumption. Limited
                        history and serial dependence affect reliability. Agent
                        confidence is not a calibrated win probability.
                      </p>
                    </section>
                    <section className="panel">
                      <div className="panel-title">
                        <div>
                          <span className="eyebrow">
                            EVIDENCE BEFORE ACTIVATION
                          </span>
                          <h2>Pilot readiness</h2>
                        </div>
                        {(() => {
                          const engineering = data.checks.filter((c) => !c.external);
                          const external = data.checks.filter((c) => c.external);
                          const verified = data.checks.filter((c) => c.pass).length;
                          const externalPending = external.filter((c) => !c.pass).length;
                          return (
                            <div className="readiness-summary" aria-label="Pilot readiness summary">
                              <span className={`pill ${externalPending || verified !== data.checks.length ? "amber" : "green"}`}>
                                {externalPending || verified !== data.checks.length ? "Launch blocked" : "Pilot gates clear"}
                              </span>
                              <small>{engineering.filter((c) => c.pass).length}/{engineering.length} engineering · {external.filter((c) => c.pass).length}/{external.length} external</small>
                            </div>
                          );
                        })()}
                      </div>
                      <p className="readiness-explanation">
                        Engineering checks show what this build verifies. External checks require reviewed evidence from the real feed, target host and forward paper results.
                      </p>
                      <div className="check-list">
                        {data.checks.map((c) => (
                          <div key={c.id}>
                            <span
                              className={c.pass ? "check-pass" : "check-wait"}
                            >
                              {c.pass ? "✓" : "○"}
                            </span>
                            <div>
                              <strong>{c.title}{c.external ? " · External gate" : ""}</strong>
                              <p>{c.detail}</p>
                            </div>
                            <span
                              className={`pill ${c.pass ? "green" : "amber"}`}
                            >
                              {c.pass ? "Observed" : "Not verified"}
                            </span>
                          </div>
                        ))}
                      </div>
                      <div className="callout">
                        This is an operational evidence view, not a
                        certification. Live trading remains disabled. A
                        successful build cannot satisfy provider, recovery or
                        strategy-performance gates.
                      </div>
                    </section>
                    <IntelligencePanel data={data} onAsk={ask} />
                    <ProviderPanel data={data} />
                  </>
                )}
                {view === "quality" && <DecisionQuality />}
                {view === "activity" && (
                  <>
                    {!hosted && <ExternalAccountPanel state={data.externalAccount} onAsk={ask} />}
                    {!hosted && <BrokerObservation state={data.brokerObservation} onAsk={(prompt,selection)=>ask(prompt,undefined,selection)} />}
                    {!hosted && <PaperOmsPanel />}
                    {!hosted && <InstitutionalRecoveryPanel />}
                    <TradeFeed trades={trades} />
                    <CostPanel friction={friction} />
                    <section className="panel">
                      <span className="eyebrow">OPERATOR AUDIT</span>
                      <h2>Workspace activity</h2>
                      {data.audit.length ? (
                        data.audit.map((a, i) => (
                          <div className="audit-row" key={i}>
                            <span className="dot" />
                            <div>
                              <strong>{a.action}</strong>
                              <p>{a.detail}</p>
                            </div>
                            <time>{new Date(a.at).toLocaleString()}</time>
                          </div>
                        ))
                      ) : (
                        <div className="empty">
                          No operator actions recorded
                        </div>
                      )}
                    </section>
                  </>
                )}
                <footer className="workspace-footer">
                  <span>PRAMANA · {data.tenantId}</span>
                  <span>
                    Paper only · No guaranteed returns · Source freshness
                    matters
                  </span>
                </footer>
              </div>
              {chat && (
                <aside className="copilot-dock">
                  <button
                    className="mobile-chat-close"
                    onClick={() => setChat(false)}
                  >
                    Close copilot ×
                  </button>
                  <CopilotPanel
                    draft={draft}
                    companyAsOf={companyAsOf}
                    brokerCapture={brokerCapture}
                    runComparisonSha256={runComparisonSha256}
                    onFullWorkspace={() => {setCompanyAsOf(undefined);setBrokerCapture(undefined);setRunComparisonSha256(undefined);}}
                    onDraft={setDraft}
                    configured={data.copilotConfigured}
                  />
                </aside>
              )}
            </div>
          )}
        </main>
      </div>
      {halt && (
        <div className="modal-backdrop">
          <section
            role="dialog"
            aria-modal="true"
            aria-labelledby="halt-title"
            className="modal"
          >
            <span className="eyebrow">OPERATOR CONTROL</span>
            <h2 id="halt-title">Halt new paper entries?</h2>
            <p>
              The engine will acknowledge this request on its independent
              protection loop. Protective exits continue. A stopped engine
              cannot acknowledge a request.
            </p>
            <label htmlFor="halt-reason">Reason</label>
            <textarea
              autoFocus
              id="halt-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              maxLength={500}
              rows={3}
              placeholder="For example: reviewing data freshness"
            />
            {controlError && (
              <p className="banner error" role="alert">
                {controlError}
              </p>
            )}
            <div className="modal-actions">
              <button
                onClick={() => {
                  setControlError("");
                  setHalt(false);
                }}
                disabled={controlBusy}
              >
                Cancel
              </button>
              <button
                className="danger"
                disabled={controlBusy || reason.trim().length < 3}
                onClick={() => void requestHalt()}
              >
                {controlBusy ? "Requesting…" : "Request halt"}
              </button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
function Metric({
  label,
  value,
  note,
  unit,
  positive,
}: {
  label: string;
  value: string;
  note: string;
  unit?: string;
  positive?: boolean;
}) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong
        className={
          positive === undefined ? "" : positive ? "positive" : "negative"
        }
      >
        {value}
        {unit && <small>{unit}</small>}
      </strong>
      <p>{note}</p>
    </div>
  );
}
function EquityChart({ portfolio }: { portfolio: Portfolio }) {
  const [range, setRange] = useState("all");
  const points = portfolio.equityCurve;
  const end = points.length
    ? Date.parse(points[points.length - 1].timestamp)
    : 0;
  const series = points.filter(
    (p) =>
      range === "all" ||
      Date.parse(p.timestamp) >= end - (range === "day" ? 86400000 : 604800000),
  );
  return (
    <section className="panel">
      <div className="panel-title">
        <div>
          <span className="eyebrow">PORTFOLIO PERFORMANCE</span>
          <h2>Equity curve</h2>
        </div>
        <div className="segmented">
          {[
            ["day", "1D"],
            ["week", "1W"],
            ["all", "All"],
          ].map(([id, label]) => (
            <button
              key={id}
              className={range === id ? "selected" : ""}
              onClick={() => setRange(id)}
              aria-pressed={range === id}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="chart equity-chart">
        {series.length > 1 ? (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={series}>
              <defs>
                <linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#65d9b5" stopOpacity={0.25} />
                  <stop offset="100%" stopColor="#65d9b5" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#26333c" vertical={false} />
              <XAxis
                dataKey="timestamp"
                tickFormatter={(v) =>
                  end - Date.parse(points[0]?.timestamp || "") < 86400000
                    ? new Date(v).toLocaleTimeString("en-IN", {
                        hour: "2-digit",
                        minute: "2-digit",
                      })
                    : new Date(v).toLocaleDateString("en-IN", {
                        month: "short",
                        day: "numeric",
                      })
                }
                minTickGap={60}
                tick={{ fill: "#91a1af", fontSize: 11 }}
              />
              <YAxis
                domain={["auto", "auto"]}
                tick={{ fill: "#91a1af", fontSize: 11 }}
                tickFormatter={(v) => `${(v / 1000).toFixed(1)}k`}
                width={55}
              />
              <Tooltip
                labelFormatter={(v) => new Date(String(v)).toLocaleString()}
                contentStyle={{
                  background: "#18252d",
                  border: "1px solid #354852",
                  borderRadius: 8,
                }}
              />
              <Area
                type="linear"
                dataKey="equity"
                connectNulls={false}
                stroke="#65d9b5"
                fill="url(#equity-fill)"
                strokeWidth={2}
              />
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <div className="empty chart-empty">
            <span>⌁</span>
            <strong>Your equity history will appear here</strong>
            <p>At least two persisted valuations are needed.</p>
          </div>
        )}
      </div>
      <p className="panel-footnote">{portfolio.markDisclaimer} Gaps indicate unavailable valuations; they are not zero equity.</p>
    </section>
  );
}
function RiskBar({
  label,
  value,
  limit,
}: {
  label: string;
  value: number;
  limit: number;
}) {
  return (
    <div className="risk-bar">
      <div>
        <span>{label}</span>
        <strong>
          {pct(value)} <small>/ {pct(limit)}</small>
        </strong>
      </div>
      <div className="bar-track">
        <span
          style={{
            width: `${Math.min(Math.max((value / limit) * 100, 0), 100)}%`,
            background: value >= limit ? "#ec9184" : "#65d9b5",
          }}
        />
      </div>
    </div>
  );
}
function Holdings({
  portfolio,
  onAsk,
}: {
  portfolio: Portfolio;
  onAsk: (q: string) => void;
}) {
  return (
    <section className="panel">
      <div className="panel-title">
        <div>
          <span className="eyebrow">POSITION BOOK</span>
          <h2>Open holdings</h2>
        </div>
        <span className="pill neutral">
          {portfolio.holdings.length} positions
        </span>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Instrument</th>
              <th className="numeric">Qty</th>
              <th className="numeric">Entry</th>
              <th className="numeric">Mark</th>
              <th className="numeric">Unrealized</th>
              <th>Protection</th>
              <th>Source</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {portfolio.holdings.map((h) => (
              <tr key={`${h.market}:${h.symbol}`}>
                <td>
                  <strong>{h.symbol}</strong>
                  <small>
                    {h.market} · {h.assetClass}
                  </small>
                </td>
                <td className="numeric mono">{h.quantity}</td>
                <td className="numeric mono">{money(h.averageEntry)}</td>
                <td className="numeric mono">{money(h.markPrice)}</td>
                <td
                  className={`numeric mono ${h.unrealizedPnl >= 0 ? "positive" : "negative"}`}
                >
                  {money(h.unrealizedPnl)}
                </td>
                <td>
                  <small>Stop {h.stopPrice ? money(h.stopPrice) : "—"}</small>
                  <small>
                    Target {h.takeProfitPrice ? money(h.takeProfitPrice) : "—"}
                  </small>
                </td>
                <td>
                  <span className={`pill ${h.fresh ? "green" : "amber"}`} title={h.markSource.replaceAll("_", " ")}>
                    {h.fresh ? "Fresh tick" : "Not current"}
                  </span>
                  <small>{h.markTimestamp ? new Date(h.markTimestamp).toLocaleString() : "Timestamp unrecorded"}</small>
                </td>
                <td>
                  <button
                    onClick={() =>
                      onAsk(
                        `Explain the risks and current protective levels for my ${h.symbol} position.`,
                      )
                    }
                  >
                    Ask ↗
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!portfolio.holdings.length && (
          <div className="empty">
            No open paper positions. A no-trade decision is a valid outcome.
          </div>
        )}
      </div>
    </section>
  );
}
function RiskLab({
  data,
  onAsk,
}: {
  data: Workspace;
  onAsk: (q: string) => void;
}) {
  const [shock, setShock] = useState(-5);
  const [overrides, setOverrides] = useState<Record<string, number>>({});
  const p = data.portfolio;
  const risk = portfolioRisk(p, shock, overrides);
  if (risk.status !== "ok") return <section className="panel"><h2>Portfolio risk unavailable</h2><p>{risk.reason}</p></section>;
  const { exposure, pnl } = risk;
  return (
    <>
      <section className="panel">
        <div className="panel-title">
          <div>
            <span className="eyebrow">HYPOTHETICAL · NO ORDERS</span>
            <h2>Portfolio shock scenario</h2>
          </div>
          <span className="pill neutral">Cash-equity scenarios</span>
        </div>
        <p className="muted">
          Set a common shock or override individual holdings below. This linear
          scenario excludes fills, fees, liquidity and stop execution.
        </p>
        <div className="scenario-controls">
          <label htmlFor="shock">
            Price shock{" "}
            <strong className={shock < 0 ? "negative" : "positive"}>
              {shock > 0 ? "+" : ""}
              {shock}%
            </strong>
          </label>
          <input
            id="shock"
            type="range"
            min={-30}
            max={30}
            step={1}
            value={shock}
            onChange={(e) => setShock(Number(e.target.value))}
          />
          <div>
            <span>−30%</span>
            <span>0%</span>
            <span>+30%</span>
          </div>
        </div>
        <div className="table-scroll">
          <table>
            <thead><tr><th>Holding</th><th>Weight of equity</th><th>Price shock %</th><th>Scenario P&amp;L</th><th>Stop status</th></tr></thead>
            <tbody>{risk.rows.map(row => <tr key={row.key}>
              <td><strong>{row.symbol}</strong><div className="muted">{row.fresh ? "Fresh mark" : "Stale / snapshot mark"}</div></td>
              <td>{pct(row.weight)}</td>
              <td><input style={{ width: "6rem" }} aria-label={`${row.symbol} price shock percent`} type="number" min={-100} max={100} step={1} value={row.shock}
                onChange={e => { const value = Number(e.target.value); if (Number.isFinite(value)) setOverrides(old => ({...old, [row.key]: Math.max(-100, Math.min(100, value))})); }} /></td>
              <td className={row.pnl < 0 ? "negative" : "positive"}>{money(row.pnl)}</td>
              <td>{row.stopState === "missing" ? "Missing" : row.stopState === "at_or_breached" ? "At / beyond stop" : `Downside ${money(row.stopDownside ?? 0)}`}</td>
            </tr>)}</tbody>
          </table>
        </div>
        {!risk.rows.length && <p className="empty">No holdings. Cash has no price-shock exposure in this scenario.</p>}
        <button onClick={() => setOverrides({})}>Reset holdings to common shock</button>
        <div className="metric-grid research-metrics">
          <Metric
            label="Marked exposure"
            value={money(exposure)}
            note="Sum of displayed holdings"
          />
          <Metric
            label="Scenario P&L"
            value={money(pnl)}
            positive={pnl >= 0}
            note="Hypothetical, not a forecast"
          />
          <Metric
            label="Scenario equity"
            value={money(p.totalEquity + pnl)}
            note="No execution assumed"
          />
          <Metric
            label="Equity impact"
            value={pct(p.totalEquity ? pnl / p.totalEquity : 0)}
            note="Using current displayed valuation"
          />
        </div>
        <button
          onClick={() =>
            onAsk(
              `Explain this hypothetical cash-equity scenario using displayed marks. Per-holding [symbol, shock percent]: ${JSON.stringify(risk.rows.map(r => [r.symbol,r.shock]))}. Total scenario P&L ${pnl}; equity impact ${risk.equityImpact}. Missing stops: ${risk.missingStops}; at/beyond stops: ${risk.breachedStops}. These are mark-based estimates, not guaranteed exit prices. Discuss costs, gaps and data freshness.`,
            )
          }
        >
          Discuss this scenario with Atlas ↗
        </button>
      </section>
      <section className="panel">
        <span className="eyebrow">CONCENTRATION &amp; RECORDED PROTECTION</span>
        <h2>What drives the portfolio exposure</h2>
        <div className="metric-grid research-metrics">
          <Metric label="Largest holding / equity" value={pct(risk.largestEquityWeight)} note="Denominator includes cash" />
          <Metric label="Gross exposure / equity" value={pct(risk.grossEquityWeight)} note="Displayed cash-equity holdings" />
          <Metric label="Effective holding count" value={risk.effectiveHoldings?.toFixed(2) ?? "—"} note="Inverse sum of squared invested weights" />
          <Metric label="Downside to recorded stops" value={risk.recordedStopDownside === null ? "Unavailable" : money(risk.recordedStopDownside)} note="Partial if stops are missing; excludes gaps and costs" />
        </div>
        <p className="footnote">{risk.missingStops} missing stops · {risk.breachedStops} at or beyond stop · {risk.staleMarks} stale or snapshot marks. Portfolio valuation status: {p.status}.</p>
        <p className="muted">Effective holding count measures position concentration only; correlated holdings can still fall together. Recorded-stop downside is not a maximum-loss estimate. A breached stop showing zero remaining distance does not prove execution. Sector/factor exposure appears below when reviewed metadata is complete; options Greeks and margin remain unavailable. The separate historical diagnostic below estimates correlations only when its data-coverage checks pass.</p>
      </section>
      <section className="panel">
        <span className="eyebrow">PORTFOLIO ATTRIBUTION</span>
        <h2>Sector and factor exposure</h2>
        {data.attribution?.status === "available" ? <>
          <p className="muted">Reviewed metadata as of {data.attribution.asOf}. These are exposure diagnostics, not realized return attribution or a margin model.</p>
          <div className="table-scroll"><table><thead><tr><th>Sector</th><th>Market value</th><th>Weight</th></tr></thead><tbody>{data.attribution.sectors?.map(row => <tr key={row.name}><td>{row.name}</td><td>{money(row.marketValue)}</td><td>{pct(row.weight)}</td></tr>)}</tbody></table></div>
          <div className="tag-list">{data.attribution.factors?.map(row => <span className="pill neutral" key={row.name}>{row.name}: {row.exposure.toFixed(2)}</span>)}</div>
          <div className="research-actions">
            <a href="/api/portfolio/attribution" download="pramana-portfolio-attribution.json">Download attribution ↗</a>
            <button onClick={() => onAsk(`Explain the reviewed sector and factor exposure snapshot dated ${data.attribution?.asOf}. Sectors: ${JSON.stringify(data.attribution?.sectors)}. Factors: ${JSON.stringify(data.attribution?.factors)}. State clearly that this is marked exposure, not realized return attribution, margin, Greeks or a forecast.`)}>Discuss with Atlas ↗</button>
          </div>
        </> : <p className="empty">{data.attribution?.detail || "Attribution is unavailable until reviewed metadata is configured."}</p>}
        <p className="footnote">{data.attribution?.detail}</p>
      </section>
      {!hosted && <HistoricalRisk state={data.historicalRisk} onAsk={onAsk} />}
      <Holdings portfolio={p} onAsk={onAsk} />
      <ProviderPanel data={data} />
    </>
  );
}
function ProviderPanel({ data }: { data: Workspace }) {
  return (
    <section className="panel">
      <span className="eyebrow">SOURCE TRANSPARENCY</span>
      <h2>Data & provider health</h2>
      <div className="provider-grid">
        {Object.entries(data.market.providers || {}).map(([name, status]) => (
          <div key={name}>
            <span className="eyebrow">{name}</span>
            <p>{status}</p>
          </div>
        ))}
        {!Object.keys(data.market.providers || {}).length && (
          <div className="empty">No collector provider report available</div>
        )}
      </div>
      <p className="footnote">
        Collector-reported status. Engine adapters:{" "}
        {Object.entries(data.runtime.providers || {})
          .map(([k, v]) => `${k}: ${v}`)
          .join(" · ") || "not reported"}
        . Configuration alone does not prove provider availability.
      </p>
    </section>
  );
}
function StreamIntegrity({data, onAsk}: {data: Workspace; onAsk: (q: string) => void}) {
  const report = data.runtime.marketDataIntegrity;
  const reasons: Record<string, string> = {future_tick: "Future timestamp", out_of_order_tick: "Older than latest quote",
    duplicate_tick: "Duplicate update", invalid_tick_values: "Invalid price or volume", crossed_tick_quotes: "Crossed bid and ask",
    invalid_tick_timestamp: "Invalid timestamp", invalid_zerodha_payload: "Invalid Zerodha packet", unmapped_tick: "Unmapped instrument"};
  const counts = report?.rejected && typeof report.rejected === "object" ? Object.entries(report.rejected) : [];
  const valid = report?.schema === "pramana.tick_integrity.v1" && Number.isSafeInteger(report.accepted) && report.accepted >= 0
    && counts.length <= 20 && counts.every(([key, value]) => key in reasons && Number.isSafeInteger(value) && value >= 0);
  if (!valid || !report) return <section className="panel"><h2>Engine quote integrity</h2><p className="muted">Stream diagnostics unavailable. Quote freshness must be checked separately.</p></section>;
  const total = counts.reduce((sum, [, count]) => sum + count, 0);
  const last = report.lastRejection;
  return <section className="panel">
    <div className="panel-title"><div><span className="eyebrow">ENGINE STREAM OBSERVATIONS</span><h2>Engine quote integrity</h2></div>
      <span className="pill neutral">{data.runtime.status === "running" ? "Current engine report" : "Last recorded report"}</span></div>
    <dl className="details"><div><dt>Accepted updates</dt><dd>{report.accepted.toLocaleString()}</dd></div>
      <div><dt>Ignored updates</dt><dd>{total.toLocaleString()}</dd></div></dl>
    {counts.length > 0 && <dl className="details">{counts.map(([reason, count]) => <div key={reason}><dt>{reasons[reason]}</dt><dd>{count.toLocaleString()}</dd></div>)}</dl>}
    {last && <p className="footnote" style={{overflowWrap: "anywhere"}}>Last ignored: {last.symbol} · {reasons[last.reason] || "Unclassified"} · received {last.receivedAt}. Source time: {last.observedAt || "unavailable"}.</p>}
    <p className="muted">Counts cover this engine process and reset on restart. Ignored updates never refresh the last accepted quote. Same-second distinct updates retain arrival order; exchange sequence reconstruction and complete tick capture are not established. The market-watch collector is a separate source.</p>
    <button className="wide-button" onClick={() => onAsk("Explain the engine quote-integrity diagnostics and rejected updates. Distinguish current quote freshness, stream ordering, source coverage and whether more evidence is required before pilot entries.")}>Discuss quote quality with Atlas ↗</button>
  </section>;
}
function DecisionSource({ value, compact = false }: { value?: DecisionProvenance; compact?: boolean }) {
  if (!value || value.mode === "unrecorded") return <p className="footnote">Decision source not recorded for this proof.</p>;
  return <div className="footnote">
    <p>{value.mode === "deterministic" ? "Decision source: deterministic rules. No model call." :
      `Decision source: ${value.transport === "injected_client" ? "Test/custom transport · " : ""}${value.provider ?? "unverified provider"} · ${value.status.replaceAll("_", " ")}. Requested: ${value.requestedModel ?? "unrecorded"}. Returned: ${value.resolvedModel ?? "identity unavailable"}.`}</p>
    {value.failureCode && Object.hasOwn(providerFailures, value.failureCode) && <p>Recorded issue: {providerFailures[value.failureCode]}</p>}
    {!compact && <dl className="details">
      <div><dt>Request fingerprint</dt><dd style={{overflowWrap:"anywhere"}}>{value.requestSha256 ?? "Not recorded"}</dd></div>
      <div><dt>Atlas configuration fingerprint</dt><dd style={{overflowWrap:"anywhere"}}>{value.configurationSha256 ?? "Not recorded"}</dd></div>
    </dl>}
  </div>;
}
function IntelligencePanel({
  data,
  onAsk,
}: {
  data: Workspace;
  onAsk: (q: string) => void;
}) {
  const i = data.intelligence;
  return (
    <section className="panel">
      <div className="panel-title">
        <div>
          <span className="eyebrow">LATEST PERSISTED DECISION</span>
          <h2>Swarm intelligence</h2>
        </div>
        <span className="pill neutral">{i.consensus.replaceAll("_", " ")}</span>
      </div>
      {i.agents.length ? (
        <>
          <p className="muted">
            {i.proof?.subject} ·{" "}
            {i.proof?.generatedAt
              ? new Date(i.proof.generatedAt).toLocaleString()
              : "Time unavailable"}
          </p>
          {i.agents.map((a) => (
            <div key={a.agentId}>
            <div className="agent-row">
              <span>{a.agentId}</span>
              <span className="agent-bar">
                <span
                  style={{
                    width: `${Math.min(Math.max(a.confidence * 100, 0), 100)}%`,
                  }}
                />
              </span>
              <strong>{a.participation?.role === "reference" ? "—" : pct(a.confidence)}</strong>
              <small>{a.stance}</small>
            </div>
            <p className="footnote" style={{marginTop: 4}}>
              <strong>{a.participation?.label ?? "Reason not recorded"}</strong>
              {a.participation?.role === "gate" ? " · Risk control" : a.participation?.role === "reference" ? " · No vote" : ""}
              {" · "}{a.participation?.reason ?? "This proof does not explain specialist participation."}
            </p>
            </div>
          ))}
          <p className="footnote">
            Confidence scores come from configured specialist rules and evidence quality;
            they are not measured success probabilities. Risk controls and references
            do not count as directional votes. This is the recorded decision, not a live provider-health check.
          </p>
          <DecisionSource value={i.proof?.provenance} compact />
          <button
            onClick={() =>
              onAsk(
                "Explain the latest recorded swarm decision, dissent and risk veto. Distinguish estimates from observed outcomes.",
              )
            }
          >
            Explain this decision ↗
          </button>
        </>
      ) : (
        <div className="empty">
          No recorded decision proof yet. Decisions appear after an eligible
          analysis cycle.
        </div>
      )}
    </section>
  );
}
function News({ data }: { data: Workspace }) {
  return (
    <section className="panel">
      <span className="eyebrow">CONTEXT, NOT A SIGNAL</span>
      <h2>Market briefing</h2>
      {data.market.news?.length ? (
        data.market.news.slice(0, 6).map((n, i) => (
          <article className="news-row" key={i}>
            <span>{String(i + 1).padStart(2, "0")}</span>
            <div>
              <p>{n.headline}</p>
              <small>
                {n.source} · {new Date(n.publishedAt).toLocaleString()}
              </small>
            </div>
          </article>
        ))
      ) : (
        <div className="empty">No dated headlines available</div>
      )}
    </section>
  );
}
function CostPanel({ friction }: { friction: Friction | null }) {
  return (
    <section className="panel">
      <span className="eyebrow">AFTER-COST ACCOUNTABILITY</span>
      <h2>Execution friction</h2>
      <div className="provider-grid">
        {Object.entries(friction?.byCode || {}).map(([code, value]) => (
          <div key={code}>
            <span className="eyebrow">{code}</span>
            <strong className="cost-value">{money(value)}</strong>
          </div>
        ))}
      </div>
      {!Object.keys(friction?.byCode || {}).length && (
        <div className="empty">No execution costs recorded</div>
      )}
      <p className="footnote">
        Spread and slippage are already reflected in fill prices. Do not deduct
        them twice from ledger P&L.
      </p>
    </section>
  );
}
function TradeFeed({ trades }: { trades: Trade[] }) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const visible = trades.filter((t) =>
    t.symbol.toLowerCase().includes(query.toLowerCase()),
  );
  return (
    <section className="panel">
      <div className="panel-title">
        <div>
          <span className="eyebrow">EXECUTION JOURNAL</span>
          <h2>Paper fills & proofs</h2>
        </div>
        <button
          onClick={() =>
            download(
              "pramana-paper-fills.json",
              JSON.stringify(visible, null, 2),
            )
          }
        >
          Export fills ↓
        </button>
      </div>
      <label className="search">
        <input
          aria-label="Filter fills"
          placeholder="Filter by instrument…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </label>
      <div className="table-scroll proof-table-scroll" ref={scroll}>
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Instrument</th>
              <th>Side</th>
              <th className="numeric">Qty</th>
              <th className="numeric">Fill</th>
              <th>Evidence</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {visible.map((t) => (
              <TradeRows
                key={t.orderId}
                trade={t}
                expanded={expanded === t.orderId}
                onToggle={() => {
                  setExpanded(expanded === t.orderId ? null : t.orderId);
                  scroll.current?.scrollTo({ left: 0 });
                }}
              />
            ))}
          </tbody>
        </table>
        {!visible.length && (
          <div className="empty">No paper fills match this view</div>
        )}
      </div>
    </section>
  );
}
function TradeRows({
  trade: t,
  expanded,
  onToggle,
}: {
  trade: Trade;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr>
        <td>
          <small>{new Date(t.createdAt).toLocaleString()}</small>
        </td>
        <td>
          <strong>{t.symbol}</strong>
        </td>
        <td>
          <span className={`pill ${t.side === "BUY" ? "green" : "amber"}`}>
            {t.side}
          </span>
        </td>
        <td className="numeric">{t.quantity}</td>
        <td className="numeric mono">{money(t.fillPrice)}</td>
        <td>
          <span className={`pill ${t.proof ? "green" : "amber"}`}>
            {t.proof?.kind === "protective_exit" ? "Protective exit" : t.proof ? "Exact proof" : "Missing proof"}
          </span>
        </td>
        <td>
          <button aria-expanded={expanded} onClick={onToggle}>
            {expanded ? "Close" : "Inspect"}
          </button>
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={7}>
            <div className="proof-detail">
              <span className="eyebrow">{t.orderId}</span>
              {t.proof ? (
                <>
                  <ul>
                    {t.proof.rationale.map((r, i) => (
                      <li key={i}>{r}</li>
                    ))}
                  </ul>
                  {t.proof.kind !== "protective_exit" && <DecisionSource value={t.proof.provenance} />}
                  <div className="provider-grid">
                    {[t.proof.risk, t.proof.stress].map((obj, i) => (
                      <dl className="details" key={i}>
                        {Object.entries(obj).map(([k, v]) => (
                          <div key={k}>
                            <dt>{k}</dt>
                            <dd>{v}</dd>
                          </div>
                        ))}
                      </dl>
                    ))}
                  </div>
                </>
              ) : (
                <p>
                  No proof with this exact order ID was found. No guessed
                  association is shown.
                </p>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function TradeEvidencePanel({ data, onAsk }: { data: Workspace; onAsk: (prompt: string) => void }) {
  const report = data.runtime.tradeEvidence;
  const strategy = data.runtime.strategyEvidence;
  const summary = report?.status === "ok" ? report.summary : undefined;
  const numeric = (value: string | null | undefined) => value != null && Number.isFinite(Number(value)) ? Number(value) : undefined;
  return <section className="panel">
    <span className="eyebrow">RECONCILED PAPER LEDGER</span>
    <h2>Completed-trade evidence</h2>
    {!summary ? <p className="empty">{report?.reason || "No current trade-episode report. The engine checks at startup and before entries; new fills require a new report."}</p> : <>
      <div className="metric-grid research-metrics">
        <Metric label="Completed trades" value={String(summary.completedTrades)} note={`${report!.fillCount} fills · ${summary.openEpisodes} open episodes`} />
        <Metric label="Net closed-trade P&L" value={money(numeric(summary.netPnl))} note="INR · recorded cash fees deducted" />
        <Metric label="Average P&L per trade" value={money(numeric(summary.expectancy))} note="Historical sample mean, not forecast expectancy" />
        <Metric label="Win rate" value={pct(numeric(summary.winRate))} note={`${summary.wins} wins · ${summary.losses} losses · ${summary.breakeven} flat`} />
        <Metric label="Profit factor" value={numeric(summary.profitFactor)?.toFixed(2) ?? "—"} note={summary.profitFactorState === "no_observed_losses" ? "No observed losses; ratio undefined" : "Net gains / absolute net losses"} />
        <Metric label="Closed / open cash fees" value={`${money(numeric(summary.closedCashFees))} / ${money(numeric(summary.openCashFees))}`} note="Open-episode fees stay outside closed-trade statistics" />
      </div>
      <p className="footnote">Ledger {report!.ledgerId} · Generated {report!.generatedAt} · {report!.currency}. Source hash: {report!.sourceSha256.slice(0, 16)}… Full hash is included in the evidence export.</p>
    </>}
    <div className="panel-title"><div><span className="eyebrow">ACTIVE CONFIGURATION ONLY</span><h3>Strategy-linked evidence</h3></div></div>
    {strategy?.status === "ok" ? <>
      <div className="metric-grid research-metrics">
        <Metric label="Linked completed trades" value={String(strategy.summary.completedTrades)} note={`${strategy.summary.openEpisodes} linked open episodes`} />
        <Metric label="Configuration-qualified days" value={String(data.strategyObservation?.days ?? 0)} note="At least 300 distinct fresh minute samples per day" />
        <Metric label="Linked net closed P&L" value={money(numeric(strategy.summary.netPnl))} note="INR · recorded cash fees deducted" />
        <Metric label="Unresolved episodes" value={String(strategy.unresolvedEpisodes)} note="Mixed or unproven episodes in the observation period" />
        <Metric label="Foreign / unproven open episodes" value={String(strategy.foreignOpenEpisodes)} note="These prevent acceptance and qualifying observations" />
        <Metric label="Unlinked account trades" value={String(strategy.unlinkedAccountCompletedTrades)} note="Included in account totals; excluded from strategy metrics" />
      </div>
      <p className="footnote">Configuration {strategy.strategySha256.slice(0,16)}… · Evidence {strategy.evidenceSha256.slice(0,16)}… · Ledger {strategy.ledgerId}. Full hashes are in the evidence export. Every fill, including protective exits, must link to the same intact configuration. A review must match these after-fee metrics; linkage does not establish genuine market data, AI calibration or profitability.</p>
    </> : <p className="empty">No current strategy-linked report. Legacy or missing proof cannot establish configuration ownership.</p>}
    <button onClick={() => onAsk("Explain the active configuration's strategy-linked evidence and unresolved episodes. Compare linked results with account totals and explain the missing qualification requirements. Do not infer profitability from these counts.")}>Discuss this evidence with Atlas ↗</button>
    <p className="muted">The account totals include every flat-to-flat episode, including scaling and partial exits. Open episodes are excluded. The separate strategy figures require configuration linkage. Neither set establishes calibrated confidence or verified forward performance. Recorded fees are included; AI/infrastructure costs and data-quality uncertainty remain outside these figures.</p>
  </section>;
}

function ResearchPanel({ data }: { data: Workspace }) {
  const r = data.research;
  return (
    <section className="panel">
      <span className="eyebrow">REPRODUCIBLE STRATEGY COMPARISON</span>
      <h2>Research experiments</h2>
      {r ? (
        <>
          <p className="muted">
            {r.scope} · {new Date(r.created_at).toLocaleString()}
          </p>
          <div className="metric-grid research-metrics">
            <Metric
              label="Holdout net return"
              value={pct(r.holdout.net_return)}
              note={`${r.holdout.observations} observations`}
            />
            <Metric
              label="Buy & hold"
              value={pct(r.buy_and_hold.net_return)}
              note="Same held-out interval"
            />
            <Metric
              label="Holdout drawdown"
              value={pct(r.holdout.max_drawdown)}
              note="Observed in this experiment"
            />
            <Metric
              label="Bootstrap drawdown p95"
              value={pct(r.path_stress.max_drawdown_p95)}
              note="Hypothetical resampled paths"
            />
          </div>
          <p className="footnote">
            {r.candidate_trials} candidate evaluations.{" "}
            {r.limitations.join(" ")}
          </p>
          <button
            onClick={() =>
              download(
                "pramana-research-report.json",
                JSON.stringify(r, null, 2),
              )
            }
          >
            Export research report ↓
          </button>
        </>
      ) : (
        <div className="empty">
          No research report has been published. Holdout, walk-forward and
          after-cost baseline evidence is still required.
        </div>
      )}
    </section>
  );
}
