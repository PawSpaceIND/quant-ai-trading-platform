"use client";
import { FormEvent, useEffect, useState } from "react";
import type {BrokerSelection} from "@/lib/broker-lifecycle";
import {CHAT_DEADLINE_MS} from "@/lib/chat-deadline";
import type { Conversation } from "@/lib/copilot";
export function CopilotPanel({
  draft,
  onDraft,
  configured,
  companyAsOf,
  brokerCapture,
  runComparisonSha256,
  onFullWorkspace,
}: {
  draft: string;
  onDraft: (value: string | ((current: string) => string)) => void;
  configured: boolean;
  companyAsOf?: string;
  brokerCapture?: BrokerSelection;
  runComparisonSha256?:string;
  onFullWorkspace?: () => void;
}) {
  const [history, setHistory] = useState<Conversation[]>([]);
  const [active, setActive] = useState<Conversation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dollarBudget, setDollarBudget] = useState<{status:string;limitUsd?:number;spentUsd?:number;reservedUsd?:number;remainingUsd:number}|null>(null);
  // The daily chat cap is enforced server-side and answered with 429. Showing the
  // remaining allowance means running out is expected rather than a mystery failure.
  const [allowance, setAllowance] = useState<{ remaining: number; limit: number } | null>(null);
  useEffect(() => {
    let alive = true;
    fetch("/api/copilot", { signal: AbortSignal.timeout(12000) })
      .then((r) => {
        if (!r.ok) throw new Error("Could not load conversation history");
        return r.json();
      })
      .then((d) => {
        if (!alive) return;
        setHistory(d.conversations);
        setDollarBudget(d.dollarBudget ?? null);
        const remaining = d.dailyRemaining, limit = d.dailyLimit;
        setAllowance(
          typeof remaining === "number" && typeof limit === "number" && limit > 0
            ? { remaining: Math.max(0, remaining), limit }
            : null,
        );
      })
      .catch((e) => {
        if (alive) setError(e.message);
      });
    const id = new URLSearchParams(window.location.search).get("chat");
    if (id)
      fetch(`/api/copilot/${encodeURIComponent(id)}`, {
        signal: AbortSignal.timeout(12000),
      })
        .then(async (r) => (r.ok ? await r.json() : {missing: r.status === 404}))
        .then((d) => {
          if (!alive || new URLSearchParams(window.location.search).get("chat") !== id) return;
          // A 404 turned into null and was swallowed, so a stale or shared link showed the
          // welcome screen with the dead id still in the address bar and no explanation.
          if (d?.missing) {
            setError("That saved conversation no longer exists.");
            const url = new URL(window.location.href);
            url.searchParams.delete("chat");
            window.history.replaceState(null, "", url);
            return;
          }
          if (d) setActive(d);
        })
        .catch(() => {
          if (alive)
            setError("Saved conversation could not be loaded. Try again.");
        });
    return () => {
      alive = false;
    };
  }, []);
  function choose(c: Conversation) {
    setActive(c);
    const url = new URL(window.location.href);
    url.searchParams.set("chat", c.id);
    window.history.replaceState(null, "", url);
  }
  // Every state but "available" refuses a paid call server-side, so the compose form must
  // say so rather than let the operator spend a daily question discovering it.
  const paidPaused = !!dollarBudget && dollarBudget.status !== "available";
  async function send(e: FormEvent) {
    e.preventDefault();
    if (!draft.trim() || busy) return;
    setBusy(true);
    setError("");
    const prompt = draft;
    let restoreHistory = false;
    onDraft("");
    try {
      const r = await fetch("/api/copilot", {
        method: "POST",
        signal: AbortSignal.timeout(CHAT_DEADLINE_MS),
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt,
          parentId: active?.id,
          id: crypto.randomUUID(),
          companyAsOf,
          brokerCapture,
          runComparisonSha256,
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error);
      setHistory((h) => [d, ...h]);
      choose(d);
      if (d.status === "error") onDraft(prompt);
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === "TimeoutError";
      setError(aborted
        ? "Atlas did not answer in time here. The question may still have completed and been charged; check Saved conversations before asking again."
        : e instanceof Error ? e.message : "Request failed");
      // Only restore the prompt if nothing was typed while waiting, so a failure cannot
      // overwrite the operator's next question.
      onDraft((current) => (current.trim() ? current : prompt));
      if (aborted) restoreHistory = true;
    } finally {
      setBusy(false);
      // Refresh the server allowance, including any bounded recovery attempt.
      void fetch("/api/copilot", { signal: AbortSignal.timeout(12000) })
        .then((r) => r.ok ? r.json() : null)
        .then((d) => {
          if (d) setDollarBudget(d.dollarBudget ?? null);
          if (d && typeof d.dailyRemaining === "number" && typeof d.dailyLimit === "number")
            setAllowance({ remaining: Math.max(0, d.dailyRemaining), limit: d.dailyLimit });
          // An answer that finished after the client gave up is still saved server side.
          // Re-list so it is reachable without a reload.
          if (d && restoreHistory && Array.isArray(d.conversations)) setHistory(d.conversations);
        }).catch(() => {});
    }
  }
  return (
    <section className="panel copilot">
      <div className="panel-title">
        <div>
          <span className="eyebrow">YOUR RESEARCH PARTNER</span>
          <h2>
            <span className="atlas-glyph">✳</span> Atlas copilot
          </h2>
        </div>
        <span className={`pill ${configured ? "green" : "amber"}`}>
          {configured ? "Claude configured" : "Setup needed"}
        </span>
      </div>
      <p className="muted">
        Ask about your portfolio, market evidence or launch readiness.
      </p>
      <div className="copilot-context">
        <span className="dot" /> {runComparisonSha256 ? "Selected paper versus replay report" : brokerCapture ? "Selected broker capture and preceding history" : companyAsOf ? "Company disclosures at selected cutoff" : "Portfolio + market + decision proofs"}{" "}
        <span>Read only</span>
      </div>
      {runComparisonSha256 && <div className="research-notice"><p>Only the selected historical run comparison is included. Current workspace and earlier chat are excluded.</p><button onClick={onFullWorkspace} disabled={busy}>Use full current workspace</button></div>}
      {brokerCapture && <div className="research-notice"><p>Broker capture #{brokerCapture.sequence}. Later captures, current workspace and earlier chat are excluded from this review.</p><button onClick={onFullWorkspace} disabled={busy}>Use full current workspace</button></div>}
      {companyAsOf && <div className="research-notice"><p>Company evidence through {new Date(companyAsOf).toLocaleString()}. Current workspace and earlier chat are excluded from this review.</p><button onClick={onFullWorkspace} disabled={busy}>Use full current workspace</button></div>}
      <div className="chat-body" aria-live="polite">
        {active ? (
          <>
            <div className="message user-message">
              <small>YOU</small>
              <p>{active.prompt}</p>
            </div>
            <div className="message">
              <small>ATLAS · {active.status}</small>
              {active.answer ? (
                <p>{active.answer}</p>
              ) : (
                <p className={active.error ? "error" : "muted"}>
                  {active.error ||
                    "Response pending. Refresh this conversation to check its status."}
                </p>
              )}
              {active.error && active.answer && <p className="error">{active.error}</p>}
              {active.status === "error" && (
                <button type="button" disabled={busy} onClick={() => onDraft(active.prompt)}>
                  Retry this question
                </button>
              )}
              <div className="footnote">
                {active.model} · {new Date(active.created_at).toLocaleString()}
              </div>
              {active.usage && (
                <div className="footnote">
                  Tokens: {JSON.parse(active.usage).inputTokens ?? "—"} in /{" "}
                  {JSON.parse(active.usage).outputTokens ?? "—"} out · Cost not
                  estimated
                </div>
              )}
              <a
                className="text-link"
                href={`/api/copilot/${active.id}`}
                target="_blank"
                rel="noreferrer"
              >
                View saved evidence ↗
              </a>
            </div>
          </>
        ) : (
          <div className="chat-welcome">
            <div className="atlas-orb">✳</div>
            <h3>Start with a better question.</h3>
            <p>Ground your next decision in the evidence you actually have.</p>
            {[
              "What is preventing pilot readiness?",
              "Explain my portfolio risk and data gaps.",
              "Compare our current strategy evidence with a simple baseline.",
            ].map((q) => (
              <button key={q} onClick={() => onDraft(q)}>
                {q}
                <span>↗</span>
              </button>
            ))}
          </div>
        )}
        {busy && (
          <div className="thinking" role="status">
            Atlas is reviewing the saved snapshot…
          </div>
        )}
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {dollarBudget && <p className={`${paidPaused ? "error" : "muted"} chat-dollar-budget`} role="status">
        {dollarBudget.status === "exhausted" ? `Combined AI limit: $${dollarBudget.limitUsd?.toFixed(2)}/day is spent. Paid calls are paused until it resets at 05:30 IST.`
          : dollarBudget.status === "unavailable" ? "AI dollar budget unavailable; paid calls paused."
          : dollarBudget.status === "activation_hold" ? `Combined AI limit: $${dollarBudget.limitUsd?.toFixed(2)}/day. Paid calls paused until 05:30 IST because earlier spending is unverified.`
          : `Combined AI limit: $${dollarBudget.limitUsd?.toFixed(2)}/day · estimated used $${dollarBudget.spentUsd?.toFixed(2)} · reserved $${dollarBudget.reservedUsd?.toFixed(2)} · available $${dollarBudget.remainingUsd.toFixed(2)}. Resets 05:30 IST.`}
      </p>}
      {allowance && (
        <p className={allowance.remaining > 0 ? "muted chat-allowance" : "error chat-allowance"} role="status">
          {allowance.remaining > 0
            ? `${allowance.remaining} of ${allowance.limit} Atlas questions left today.`
            : `Daily limit of ${allowance.limit} Atlas questions reached; it resets at 00:00 UTC.`}
        </p>
      )}
      <form onSubmit={send} className="chat-form">
        <label className="sr-only" htmlFor="copilot-question">
          Ask Atlas
        </label>
        <textarea
          id="copilot-question"
          placeholder="Ask Atlas about this workspace…"
          value={draft}
          maxLength={3000}
          rows={3}
          onChange={(e) => onDraft(e.target.value)}
        />
        <div>
          <span>{draft.length}/3000 · No order execution</span>
          <button className="primary" disabled={busy || paidPaused || !draft.trim()}>
            {busy ? "Thinking…" : "Send ↑"}
          </button>
        </div>
      </form>
      {history.length > 0 && (
        <details className="conversation-history">
          <summary>Saved conversations ({history.length})</summary>
          <button
            disabled={busy}
            onClick={() => {
              setActive(null);
              setError("");
              onDraft("");
              const url = new URL(window.location.href);
              url.searchParams.delete("chat");
              window.history.replaceState(null, "", url);
            }}
          >
            + New conversation
          </button>
          {history.map((c) => (
            <button key={c.id} disabled={busy} onClick={() => choose(c)}>
              <span>{c.prompt}</span>
              <small>{c.status}</small>
            </button>
          ))}
        </details>
      )}
      <p className="footnote">
        Questions and workspace context are sent to the configured Claude
        provider. Responses are saved and may be wrong; source freshness remains
        visible.
      </p>
    </section>
  );
}
