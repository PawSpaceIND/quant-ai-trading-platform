import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { QuantApi, type PortfolioResponse, type Readiness, type UsageResponse } from "./api";
import "./styles.css";

function App() {
  const [baseUrl, setBaseUrl] = useState("http://localhost:8000");
  const [apiKey, setApiKey] = useState("");
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [portfolio, setPortfolio] = useState<PortfolioResponse | null>(null);
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const api = useMemo(() => new QuantApi(baseUrl.replace(/\/$/, ""), apiKey), [baseUrl, apiKey]);

  async function refresh() {
    if (!apiKey) return;
    setLoading(true);
    setError("");
    try {
      const [nextReadiness, nextPortfolio, nextUsage] = await Promise.all([
        api.readiness(), api.portfolio(), api.usage()
      ]);
      setReadiness(nextReadiness);
      setPortfolio(nextPortfolio);
      setUsage(nextUsage);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load platform state");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (apiKey) void refresh();
  }, []);

  const totalValue = portfolio?.positions.reduce((sum, item) => sum + Number(item.market_value), 0) ?? 0;
  const totalRequests = usage?.usage.reduce((sum, item) => sum + item.count, 0) ?? 0;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">Q</span><div><strong>Quant AI</strong><small>Control Center</small></div></div>
        <nav>
          <a className="active" href="#overview">Overview</a>
          <a href="#portfolio">Portfolio</a>
          <a href="#risk">Risk & Readiness</a>
          <a href="#usage">Usage</a>
        </nav>
        <div className="safety-card">
          <span className="dot" /> PAPER / SHADOW ONLY
          <p>Live order submission is unavailable.</p>
        </div>
      </aside>

      <main>
        <header className="topbar">
          <div><p className="eyebrow">GLOBAL MULTI-ASSET RESEARCH PLATFORM</p><h1>Decision & Risk Console</h1></div>
          <button onClick={refresh} disabled={!apiKey || loading}>{loading ? "Refreshing…" : "Refresh"}</button>
        </header>

        <section className="connection-card">
          <label>API base URL<input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} /></label>
          <label>Tenant API key<input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="Paste tenant key" /></label>
          <button onClick={refresh} disabled={!apiKey || loading}>Connect</button>
        </section>

        {error && <div className="error-banner">{error}</div>}

        <section id="overview" className="metric-grid">
          <article><span>Platform mode</span><strong>{readiness?.mode ?? "PAPER"}</strong><small>Live disabled</small></article>
          <article><span>Readiness</span><strong>{readiness ? (readiness.ready ? "READY" : "BLOCKED") : "—"}</strong><small>{readiness?.blockers.length ?? 0} blockers</small></article>
          <article><span>Portfolio value</span><strong>{totalValue.toLocaleString(undefined, { maximumFractionDigits: 2 })}</strong><small>Tenant scoped</small></article>
          <article><span>API usage</span><strong>{totalRequests}</strong><small>Metered requests</small></article>
        </section>

        <div className="two-column">
          <section id="portfolio" className="panel">
            <div className="panel-head"><div><p className="eyebrow">CAPITAL</p><h2>Portfolio</h2></div><span>{portfolio?.positions.length ?? 0} positions</span></div>
            <div className="table">
              <div className="row header"><span>Symbol</span><span>Qty</span><span>Market value</span></div>
              {(portfolio?.positions ?? []).map((position) => (
                <div className="row" key={position.symbol}><strong>{position.symbol}</strong><span>{position.quantity}</span><span>{Number(position.market_value).toLocaleString()}</span></div>
              ))}
              {!portfolio?.positions.length && <div className="empty">No positions returned for this tenant.</div>}
            </div>
          </section>

          <section id="risk" className="panel">
            <div className="panel-head"><div><p className="eyebrow">SAFETY</p><h2>Readiness Gates</h2></div></div>
            <div className={`status-block ${readiness?.ready ? "ready" : "blocked"}`}>
              <span className="status-orb" /><div><strong>{readiness?.ready ? "Paper system ready" : "Integration blockers remain"}</strong><p>Deterministic risk controls remain authoritative over AI signals.</p></div>
            </div>
            <ul className="blocker-list">
              {(readiness?.blockers ?? []).map((blocker) => <li key={blocker}>{blocker}</li>)}
              {readiness?.ready && <li className="clear">No paper-mode blockers reported.</li>}
            </ul>
          </section>
        </div>

        <section id="usage" className="panel">
          <div className="panel-head"><div><p className="eyebrow">COMMERCIAL</p><h2>Usage Metering</h2></div><span>{usage?.tenant_id ?? "No tenant connected"}</span></div>
          <div className="usage-grid">
            {(usage?.usage ?? []).map((record) => <div key={record.metric}><span>{record.metric.replaceAll("_", " ")}</span><strong>{record.count}</strong></div>)}
            {!usage?.usage.length && <div className="empty">Usage will appear after authenticated activity.</div>}
          </div>
        </section>
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
