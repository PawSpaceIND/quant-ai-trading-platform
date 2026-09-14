"use client";

import {useMemo, useState, type ReactNode} from "react";
import {CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis} from "recharts";
import type {PortfolioResearchState} from "../lib/research-portfolio";

const currency = new Intl.NumberFormat("en-IN", {style: "currency", currency: "INR", maximumFractionDigits: 2});
const money = (value: string | number | null) => value === null ? "Unavailable" : currency.format(Number(value));
const percent = (value: string | null) => value === null ? "Unavailable" : `${(Number(value) * 100).toFixed(2)}%`;
const timestamp = (value: string | null) => value ? new Date(value).toLocaleString() : "No observations";
const plainAxis = new Intl.NumberFormat("en-IN", {maximumFractionDigits: 2});
const compactAxis = new Intl.NumberFormat("en-IN", {notation: "compact", maximumFractionDigits: 1});

function ReplayTable({title, columns, rows}: {title: string; columns: string[]; rows: ReactNode[][]}) {
  const [requestedPage, setPage] = useState(0);
  const page = Math.min(requestedPage, Math.max(0, Math.ceil(rows.length / 25) - 1));
  const start = page * 25;
  return <div className="replay-table">
    <div className="research-table-scroll" role="region" tabIndex={0} aria-label={title}>
      <table><caption>{title}</caption><thead><tr>{columns.map(c => <th key={c} scope="col">{c}</th>)}</tr></thead>
        <tbody>{rows.slice(start, start + 25).map((row, i) => <tr key={start + i}>{row.map((cell, j) => j === 0 ? <th scope="row" key={j}>{cell}</th> : <td key={j}>{cell}</td>)}</tr>)}</tbody>
      </table>
    </div>
    {!rows.length ? <p className="muted">No records in this snapshot.</p> : <div className="replay-pagination"><span>{start + 1}–{Math.min(start + 25, rows.length)} of {rows.length}</span><button aria-label={`Previous ${title}`} disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button><button aria-label={`Next ${title}`} disabled={start + 25 >= rows.length} onClick={() => setPage(page + 1)}>Next</button></div>}
  </div>;
}

export function ResearchPortfolio({state, onAsk}: {state?: PortfolioResearchState; onAsk: (question: string) => void}) {
  const [choice, setChoice] = useState("");
  const [metric, setMetric] = useState<"equity" | "drawdown">("equity");
  const report = state?.report;
  const names = Object.keys(report?.books || {});
  const selected = names.includes(choice) ? choice : names[0];
  const book = report?.books[selected];
  const chart = useMemo(() => book?.curve.map(p => ({...p,
    equity: p.equity_inr === null ? null : Number(p.equity_inr),
    drawdown: p.drawdown_fraction === null ? null : Number(p.drawdown_fraction) * 100,
  })) || [], [book]);
  const equityRange = useMemo(() => {
    const values = chart.flatMap(point => point.equity === null ? [] : [point.equity]);
    return values.length ? Math.max(...values) - Math.min(...values) : 0;
  }, [chart]);
  return <section className="panel research-comparison portfolio-replay">
    <div className="panel-title"><div><span className="eyebrow">CONTINUOUS PORTFOLIO RESEARCH</span><h2>Portfolio replay</h2></div><span className="muted">Simulation</span></div>
    {!report || !book ? <p className="empty">{state?.detail || "No continuous portfolio replay has been published to this workspace."}</p> : <>
      <h3>{report.name}</h3>
      <p className="muted">Timeline ends {timestamp(report.as_of)} · Published {timestamp(report.generated_at)}</p>
      <p className="research-notice"><strong>Unqualified research evidence.</strong> These portfolios replay supplied events with simulated execution. Each candidate starts with {money(report.config.starting_cash_inr)}. Trading fees are included; model and infrastructure costs are excluded. This report does not approve a strategy.</p>
      <p>{report.event_count} events · {report.quote_events} quote events · {report.order_events} order events · {report.symbols.length} configured symbols</p>
      <ReplayTable title="Candidate portfolio summary" columns={["Candidate", "Latest equity", "Net return", "Observed max drawdown", "Unvalued events", "Fees", "Open holdings", "Entries"]}
        rows={names.map(name => {const b = report.books[name]; return [<button key={name} className="replay-candidate-button" onClick={() => setChoice(name)} aria-label={`Inspect portfolio ${name}`}>{name}</button>, money(b.current_equity_inr), percent(b.net_return_fraction), percent(b.max_observed_drawdown_fraction), b.unvalued_observations, money(b.fees_inr), b.holdings.length, b.halted ? "Halted in simulation" : "Subject to simulation limits"];})} />
      <div className="replay-selectors">
        <label>Candidate<select aria-label="Portfolio replay candidate" value={selected} onChange={e => setChoice(e.target.value)}>{names.map(name => <option key={name}>{name}</option>)}</select></label>
        <label>Chart<select aria-label="Portfolio replay chart" value={metric} onChange={e => setMetric(e.target.value as "equity" | "drawdown")}><option value="equity">Equity (INR)</option><option value="drawdown">Drawdown (%)</option></select></label>
      </div>
      <h3>{selected}</h3>
      <dl className="research-stats replay-metrics">
        <div><dt>Latest simulated equity</dt><dd>{money(book.current_equity_inr)}</dd></div>
        <div><dt>Net return</dt><dd>{percent(book.net_return_fraction)}</dd></div>
        <div><dt>Cash</dt><dd>{money(book.cash_inr)}</dd></div>
        <div><dt>Realized / unrealized P&amp;L</dt><dd>{money(book.realized_pnl_inr)} / {money(book.unrealized_pnl_inr)}</dd></div>
        <div><dt>Recorded trading fees</dt><dd>{money(book.fees_inr)}</dd></div>
        <div><dt>Current / observed max drawdown</dt><dd>{percent(book.current_drawdown_fraction)} / {percent(book.max_observed_drawdown_fraction)}</dd></div>
        <div><dt>Unvalued observations</dt><dd>{book.unvalued_observations} / {book.curve.length}</dd></div>
        <div><dt>Pending orders / cancelled remainders</dt><dd>{book.pending_orders.length} / {book.cancelled_orders.length}</dd></div>
      </dl>
      <p className="research-notice">{book.halted ? "Further buys are halted in this simulation. Exits remain possible; this does not prove broker protection." : "No permanent drawdown halt in this simulation. Other order and data limits still apply."}{book.stale_symbols.length > 0 && ` Latest valuation is unavailable: stale marks for ${book.stale_symbols.join(", ")}.`}</p>
      {chart.length ? <div className="portfolio-replay-chart" role="img" aria-label={`${selected} simulated ${metric}; ${book.unvalued_observations} valuation gaps across ${chart.length} events`}>
        <ResponsiveContainer width="100%" height="100%"><LineChart data={chart} margin={{top: 10, right: 12, left: 5, bottom: 12}}>
          <CartesianGrid stroke="#2a3b44" strokeDasharray="3 3" />
          <XAxis dataKey="event_index" type="number" domain={["dataMin", "dataMax"]} allowDecimals={false} tickFormatter={value => String(Number(value) + 1)} tick={{fill: "#a5b7c1", fontSize: 11}} label={{value: "Event sequence", position: "insideBottom", offset: -10, fill: "#a5b7c1", fontSize: 11}} />
          <YAxis domain={["auto", "auto"]} width={76} tick={{fill: "#a5b7c1", fontSize: 11}} tickFormatter={value => metric === "drawdown" ? `${Number(value).toFixed(1)}%` : (equityRange >= 10000 ? compactAxis : plainAxis).format(Number(value))} />
          <Tooltip contentStyle={{background: "#12202a", border: "1px solid #3b515e", color: "#f2f6f4"}} labelFormatter={value => {const point = chart[Number(value)]; return point ? `Event ${point.event_index + 1} · ${timestamp(point.at)}` : String(value);}} formatter={value => metric === "drawdown" ? `${Number(value).toFixed(2)}%` : money(Number(value))} />
          <Line type="stepAfter" dataKey={metric} name={metric === "drawdown" ? "Drawdown" : "Equity (INR)"} stroke={metric === "drawdown" ? "#eda18b" : "#a5e6c4"} strokeWidth={2} dot={chart.length === 1} connectNulls={false} isAnimationActive={false} />
        </LineChart></ResponsiveContainer>
      </div> : <p className="empty">No event observations have been replayed. Starting cash alone is not performance evidence.</p>}
      <p className="footnote">The horizontal axis follows recorded event order, with timestamps in the tooltip. Valuation gaps are not connected. Observed maximum drawdown can understate losses during those gaps.</p>
      <ReplayTable key={`${selected}:holdings`} title="Simulated holdings" columns={["Symbol", "Quantity", "Remaining cost", "Last bid", "Quote timestamp", "Mark", "Market value", "Unrealized P&L"]}
        rows={book.holdings.map(h => [h.symbol, h.quantity, money(h.cost_inr), money(h.last_bid), timestamp(h.quote_at), h.mark_fresh ? "Fresh on timeline" : "Stale / absent", money(h.market_value_inr), money(h.unrealized_pnl_inr)])} />
      <details><summary>Fills and outstanding orders</summary>
        <ReplayTable key={`${selected}:fills`} title="Simulated fills" columns={["Order", "Time", "Symbol", "Side", "Quantity", "Fill price", "Fee", "Quote"]}
          rows={book.fills.map(f => [f.order_id, timestamp(f.at), f.symbol, f.side, f.quantity, money(f.price), money(f.fee_inr), f.quote_id])} />
        <ReplayTable key={`${selected}:pending`} title="Pending simulation orders" columns={["Order", "Submitted", "Symbol", "Side", "Quantity"]}
          rows={book.pending_orders.map(o => [o.order_id, timestamp(o.submitted_at), o.symbol, o.side, o.quantity])} />
        <ReplayTable key={`${selected}:cancelled`} title="Cancelled simulation remainders" columns={["Order", "Symbol", "Side", "Quantity", "Reason"]}
          rows={book.cancelled_orders.map(o => [o.order_id, o.symbol, o.side, o.quantity, o.reason.replaceAll("_", " ")])} />
      </details>
      <details><summary>Replay assumptions and source evidence</summary>
        <p>Fees {report.config.fee_bps} bps · Adverse slippage {report.config.slippage_bps} bps · Position cap {percent(report.config.max_position_fraction)} · Gross cap {percent(report.config.max_gross_fraction)} · Drawdown halt {percent(report.config.max_drawdown_fraction)} · Quote age {report.config.max_quote_age_seconds}s · Order lifetime {report.config.order_ttl_seconds}s</p>
        <ul>{report.limitations.map((text, i) => <li key={i}>{text}</li>)}</ul>
        <p className="footnote">Journal evidence SHA-256: <code>{report.evidence_sha256}</code></p><p className="footnote">Simulator source SHA-256: <code>{report.implementation.source_sha256}</code> · Python {report.implementation.python_version}. Local hashes are not independent signatures or proof of source quality.</p>
      </details>
      <div className="research-actions"><button onClick={() => onAsk(`Explain the published continuous portfolio replay ${JSON.stringify(report.name)}, especially candidate ${JSON.stringify(selected)}. Compare equity, drawdown, fees, open holdings and pending orders. Identify valuation gaps and missing qualification; distinguish these simulations from the running paper account and independent-case results.`)}>Discuss portfolio replay with Atlas ↗</button><a href="/api/research/portfolio" download="pramana-portfolio-replay.json">Export portfolio replay ↓</a></div>
    </>}
  </section>;
}
