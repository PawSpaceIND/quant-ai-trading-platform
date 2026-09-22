"use client";
import { useCallback, useEffect, useState } from "react";
import type { Alert, AlertsState } from "@/lib/alerts";

const when = (value: string | null) =>
  value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "—";

/** CRITICAL and HIGH are the ones an operator is meant to act on; an unstated priority is not INFO. */
const tone = (priority: Alert["priority"]) =>
  priority === "CRITICAL" ? "red" : priority === "HIGH" ? "amber" : priority === "INFO" ? "neutral" : "amber";

/**
 * The engine's own alert log.
 *
 * Seven of the codes it raises had no path to any screen. The one that matters most is
 * MACRO_PROVIDER_UNAVAILABLE: when the macro provider refuses at boot every specialist
 * that reads macro reports zero confidence, the consensus sinks below its floor, and the
 * pilot holds all day with nothing visible to explain it.
 */
export function EngineAlerts() {
  const [state, setState] = useState<AlertsState | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    setBusy(true);
    try {
      const response = await fetch("/api/alerts", { cache: "no-store", signal: AbortSignal.timeout(12000) });
      if (response.status === 401) { window.location.assign("/login"); return; }
      const body = await response.json();
      if (!response.ok && !body.status) throw new Error(body.error || "The alert log could not be read");
      setState(body as AlertsState);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "The alert log could not be read");
    } finally {
      setBusy(false);
    }
  }, []);
  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 60_000);
    return () => clearInterval(timer);
  }, [load]);
  const alerts = state?.status === "available" ? state.alerts : [];
  return <section className="panel" aria-label="Engine alerts">
    <div className="panel-title">
      <div><span className="eyebrow">WHAT THE ENGINE RAISED</span><h2>Engine alerts</h2></div>
      <button onClick={() => void load()} disabled={busy} aria-label="Refresh engine alerts">↻ Refresh</button>
    </div>
    {error && <div className="banner error" role="alert">{error}. No claim is made that nothing was raised.</div>}
    {!state && !error && <div className="loading-state" role="status">Loading engine alerts…</div>}
    {state && state.status !== "available" && <div className="empty">{state.detail}</div>}
    {state?.status === "available" && (alerts.length
      ? <>
        <div className="table-scroll">
          <table aria-label="Engine alerts">
            <thead><tr><th>Raised</th><th>Priority</th><th>Alert</th><th>Detail</th></tr></thead>
            <tbody>{alerts.map(a => <tr key={a.id}>
              <td><small>{when(a.createdAt)}</small>{a.loggedAt && Date.parse(a.loggedAt) - Date.parse(a.createdAt) > 60_000
                && <small className="muted"> · written {when(a.loggedAt)}</small>}</td>
              <td><span className={`pill ${tone(a.priority)}`}>{a.priority ?? "UNSTATED"}</span></td>
              <td><strong>{a.label}</strong>{!a.code && <div className="muted">This build does not declare this code.</div>}</td>
              <td>{a.message}
                {Object.keys(a.metadata).length > 0 && <div className="muted">
                  {Object.entries(a.metadata).map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`).join(" · ")}
                </div>}</td>
            </tr>)}</tbody>
          </table>
        </div>
        <p className="footnote">
          Newest {alerts.length} of this workspace&apos;s alerts, from {state.scanned} scanned lines
          {state.truncated ? " at the end of the log" : " in the log"}. Older alerts stay in the log and are not shown here.
        </p>
      </>
      : <div className="empty">No alert in the readable tail belongs to this workspace. Older alerts may exist earlier in the log.</div>)}
    <p className="muted">
      These are what the engine raised, not the current state of anything. A halt reported here may since have been
      cleared, and an alert the engine never managed to write cannot appear.
    </p>
  </section>;
}
