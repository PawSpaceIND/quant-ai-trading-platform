"use client";
import { useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { MarketSnapshot } from "@/lib/market";
const number = (v: number | undefined) =>
  v === undefined
    ? "—"
    : new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 }).format(v);
export function MarketWorkspace({
  data,
  favorites,
  onSave,
  onAsk,
  compact = false,
  readOnly = false,
}: {
  data: MarketSnapshot;
  favorites: string[];
  onSave: (v: string[]) => Promise<void>;
  onAsk: (s: string) => void;
  compact?: boolean;
  readOnly?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [savedOnly, setSavedOnly] = useState(false);
  const [sort, setSort] = useState("symbol");
  const [selected, setSelected] = useState("");
  const [range, setRange] = useState(30);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const rows = useMemo(
    () =>
      data.rows
        .filter(
          (r) =>
            r.symbol.toLowerCase().includes(query.toLowerCase()) &&
            (!savedOnly || favorites.includes(r.symbol)),
        )
        .sort((a, b) =>
          sort === "change"
            ? (b.change ?? -Infinity) - (a.change ?? -Infinity)
            : sort === "price"
              ? (b.price ?? 0) - (a.price ?? 0)
              : a.symbol.localeCompare(b.symbol),
        ),
    [data.rows, query, savedOnly, favorites, sort],
  );
  const detail = data.rows.find((r) => r.symbol === selected) || rows[0];
  const history = (detail?.history || []).slice(-range);
  async function favorite(symbol: string) {
    setSaving(true);
    setError("");
    try {
      await onSave(
        favorites.includes(symbol)
          ? favorites.filter((s) => s !== symbol)
          : [...favorites, symbol],
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save");
    } finally {
      setSaving(false);
    }
  }
  return (
    <section className="panel market-panel">
      <div className="panel-title">
        <div>
          <span className="eyebrow">MARKET INTELLIGENCE</span>
          <h2>
            Market watch <span className="count">{data.rows.length}</span>
          </h2>
        </div>
        <span className={`pill ${data.collectorStale ? "amber" : "neutral"}`}>
          {data.collectorStale
            ? "Collector stale"
            : data.session || "Session unknown"}
        </span>
      </div>
      <div className="market-tools">
        <label className="search">
          <span aria-hidden>⌕</span>
          <input
            aria-label="Search instruments"
            placeholder="Search instruments…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <button
          className={savedOnly ? "selected" : ""}
          onClick={() => setSavedOnly(!savedOnly)}
          aria-pressed={savedOnly}
        >
          ★ Saved ({favorites.length})
        </button>
        <select
          aria-label="Sort instruments"
          value={sort}
          onChange={(e) => setSort(e.target.value)}
        >
          <option value="symbol">Name A–Z</option>
          <option value="change">Change ↓</option>
          <option value="price">Price ↓</option>
        </select>
      </div>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <div className={compact ? "" : "market-split"}>
        <div className="table-scroll market-table">
          <table>
            <thead>
              <tr>
                <th aria-label="Save" />
                <th>Instrument</th>
                <th className="numeric">Last price</th>
                <th className="numeric">Change</th>
                <th>Trend</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const values = (row.history || []).map((v) => v.close);
                const low = Math.min(...values),
                  span = Math.max(...values) - low || 1;
                return (
                  <tr
                    key={row.symbol}
                    className={
                      detail?.symbol === row.symbol ? "active-row" : ""
                    }
                  >
                    <td>
                      <button
                        className="star"
                        disabled={saving || readOnly}
                        aria-label={`${favorites.includes(row.symbol) ? "Remove" : "Save"} ${row.symbol}`}
                        aria-pressed={favorites.includes(row.symbol)}
                        onClick={() => void favorite(row.symbol)}
                      >
                        {favorites.includes(row.symbol) ? "★" : "☆"}
                      </button>
                    </td>
                    <td>
                      <button
                        className="symbol-button"
                        onClick={() => setSelected(row.symbol)}
                      >
                        {row.symbol}
                        <small>{row.instrument?.exchange || "Venue unknown"} · {row.instrument?.currency || "Currency unknown"} · {row.instrument?.assetClass || "Asset unknown"}{row.instrument?.contract ? ` · ${row.instrument.contract}${row.instrument.expiry ? ` · ${row.instrument.expiry}` : ""}` : ""}</small>
                      </button>
                    </td>
                    <td className="numeric mono">
                      {row.available ? number(row.price) : "Unavailable"}
                    </td>
                    <td
                      className={`numeric mono ${(row.change ?? 0) >= 0 ? "positive" : "negative"}`}
                    >
                      {row.change == null
                        ? "—"
                        : `${row.change > 0 ? "+" : ""}${row.change.toFixed(2)}%`}
                    </td>
                    <td>
                      {values.length > 1 ? (
                        <svg
                          width="84"
                          height="28"
                          viewBox="0 0 84 28"
                          role="img"
                          aria-label={`${row.symbol} historical daily closes`}
                        >
                          <polyline
                            fill="none"
                            stroke={
                              values[values.length - 1] >= values[0]
                                ? "#65d9b5"
                                : "#eea090"
                            }
                            strokeWidth="1.6"
                            points={values
                              .map(
                                (v, i) =>
                                  `${(i * 84) / (values.length - 1)},${25 - ((v - low) / span) * 22}`,
                              )
                              .join(" ")}
                          />
                        </svg>
                      ) : (
                        <span className="muted">No history</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!rows.length && (
            <div className="empty">
              {query || savedOnly
                ? "No instruments match this filter."
                : "Waiting for the market collector. No synthetic quotes are displayed."}
            </div>
          )}
        </div>
        {!compact && detail && (
          <div className="instrument-detail">
            <div className="section-row">
              <div>
                <span className="eyebrow">INSTRUMENT DETAIL</span>
                <h2>{detail.symbol}</h2>
              </div>
              <button
                onClick={() =>
                  onAsk(
                    `Explain the available evidence and risks for ${detail.symbol}. Identify any stale or missing inputs.`,
                  )
                }
              >
                Ask Atlas ↗
              </button>
            </div>
            <div className="detail-price">
              {number(detail.price)} <small>{detail.instrument?.currency || "quote units"}</small>
            </div>
            <p className="muted">
              Historical daily closes · last available prices
            </p>
            <div className="segmented">
              {[10, 30, 90].map((n) => (
                <button
                  key={n}
                  className={range === n ? "selected" : ""}
                  aria-pressed={range === n}
                  onClick={() => setRange(n)}
                >
                  {n} sessions
                </button>
              ))}
            </div>
            <div className="chart detail-chart">
              {history.length > 1 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={history}>
                    <defs>
                      <linearGradient
                        id="market-fill"
                        x1="0"
                        y1="0"
                        x2="0"
                        y2="1"
                      >
                        <stop
                          offset="0%"
                          stopColor="#65d9b5"
                          stopOpacity={0.25}
                        />
                        <stop
                          offset="100%"
                          stopColor="#65d9b5"
                          stopOpacity={0}
                        />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="#26333c" vertical={false} />
                    <XAxis
                      dataKey="date"
                      tickFormatter={(v) => String(v).slice(5, 10)}
                      tick={{ fill: "#8497a5", fontSize: 11 }}
                      minTickGap={35}
                    />
                    <YAxis
                      domain={["auto", "auto"]}
                      tick={{ fill: "#8497a5", fontSize: 11 }}
                      width={60}
                    />
                    <Tooltip
                      contentStyle={{
                        background: "#18252d",
                        border: "1px solid #354852",
                        borderRadius: 8,
                      }}
                    />
                    <Area
                      type="monotone"
                      dataKey="close"
                      stroke="#65d9b5"
                      fill="url(#market-fill)"
                      strokeWidth={2}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <div className="empty">No historical series available</div>
              )}
            </div>
            <dl className="details">
              <div>
                <dt>Volume</dt>
                <dd>{number(detail.volume)}</dd>
              </div>
              <div>
                <dt>Last trade / exchange time</dt>
                <dd>
                  {detail.lastTrade ||
                    detail.exchangeTimestamp ||
                    "Not supplied"}
                </dd>
              </div>
              {detail.instrument?.contract && <div>
                <dt>Contract</dt>
                <dd>{detail.instrument.contract}{detail.instrument.expiry ? ` · expiry ${detail.instrument.expiry}` : ""}{detail.instrument.lotSize ? ` · lot ${detail.instrument.lotSize}` : ""}{detail.instrument.tickSize ? ` · tick ${detail.instrument.tickSize}` : ""}</dd>
              </div>}
              <div>
                <dt>Collector retrieved</dt>
                <dd>
                  {data.fetchedAt
                    ? new Date(data.fetchedAt).toLocaleString()
                    : "Unavailable"}
                </dd>
              </div>
            </dl>
            <p className="footnote">
              Saving a symbol changes your observation list. The engine trades
              only its separately approved pilot universe.
            </p>
          </div>
        )}
      </div>
      {data.coverage?.groups?.length ? (
        <div className="market-coverage callout">
          <div className="section-row">
            <div>
              <span className="eyebrow">INDIA COVERAGE</span>
              <h3>{data.coverage.scope}</h3>
            </div>
            <span className="pill neutral">Paper only</span>
          </div>
          <div className="coverage-grid">
            {data.coverage.groups.map((group) => (
              <article key={group.id} className="coverage-card">
                <div className="coverage-card-title">
                  <strong>{group.label}</strong>
                  <span className={`pill ${group.status === "observed" ? "green" : "amber"}`}>
                    {group.status === "observed" ? "Observed" : group.mode === "requires_contract" ? "Contract needed" : "Planned"}
                  </span>
                </div>
                <small>{group.exchange} · {group.currency} · {group.assetClasses.join(" / ")}</small>
                <p className="coverage-examples">{group.examples.join(" · ")}</p>
                <p className="footnote">{group.detail}</p>
              </article>
            ))}
          </div>
          <p className="footnote">{data.coverage.disclaimer}</p>
        </div>
      ) : null}
      <p className="panel-footnote">
        {data.source || "Market provider"} ·{" "}
        {data.note || "Collector freshness does not establish quote freshness."}
      </p>
    </section>
  );
}
