"use client";
import { FormEvent, useState } from "react";
export default function Login() {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const secret = new FormData(e.currentTarget).get("secret");
    try {
      const r = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ secret }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error);
      window.location.assign("/");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Connection failed");
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-shell">
      <div className="login-brand">
        P<span>PRAMANA</span>
      </div>
      <form onSubmit={submit} className="login-card">
        <span className="eyebrow">PRIVATE PAPER PILOT</span>
        <h1>
          Your trading research,
          <br />
          under control.
        </h1>
        <p>Sign in to your portfolio, market workspace and Atlas copilot.</p>
        <label htmlFor="secret">Workspace access key</label>
        <input
          id="secret"
          name="secret"
          type="password"
          autoComplete="current-password"
          required
          maxLength={1024}
        />
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <button className="primary" disabled={busy}>
          {busy ? "Signing in…" : "Open workspace →"}
        </button>
        <small>
          Paper execution only · Signed session cookie · 8-hour session
        </small>
      </form>
    </main>
  );
}
