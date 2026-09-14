export type ResearchSnapshot = {
  schemaVersion: 1;
  paperOnly: true;
  generatedAt: string;
  modules: Array<{
    id: "comparison" | "simulation" | "companyEvents";
    title: string;
    status: "available" | "incomplete" | "unavailable";
    observedAt: string | null;
    reason: string;
    rows: Array<{ name: string; metrics: Array<{ label: string; value: string }> }>;
  }>;
};

/** Runtime validation before crossing an authenticated API/Worker boundary. */
export function isResearchSnapshot(value: unknown): value is ResearchSnapshot {
  if (!value || typeof value !== "object") return false;
  const data = value as Record<string, unknown>;
  const text = (v: unknown, max: number) => typeof v === "string" && v.length <= max;
  const time = (v: unknown) => text(v, 80) && Number.isFinite(Date.parse(v as string)) && /(?:Z|[+-]\d\d:\d\d)$/.test(v as string);
  if (data.schemaVersion !== 1 || data.paperOnly !== true || !time(data.generatedAt) || !Array.isArray(data.modules) || data.modules.length !== 3) return false;
  const ids = new Set<string>();
  for (const m of data.modules) {
    if (!m || typeof m !== "object" || !["comparison", "simulation", "companyEvents"].includes(m.id) || ids.has(m.id)) return false;
    ids.add(m.id);
    if (!["available", "incomplete", "unavailable"].includes(m.status) || !text(m.title, 120) || !text(m.reason, 500) || !(m.observedAt === null || time(m.observedAt)) || !Array.isArray(m.rows) || m.rows.length > 50) return false;
    for (const row of m.rows) {
      if (!row || !text(row.name, 80) || !Array.isArray(row.metrics) || row.metrics.length > 20) return false;
      if (row.metrics.some((metric: { label?: unknown; value?: unknown } | null) => !metric || !text(metric.label, 100) || !text(metric.value, 160))) return false;
    }
  }
  return true;
}

/** Read-only presentational module. The parent owns authentication and refresh. */
export function ResearchLabPanel({ snapshot, now }: { snapshot: unknown; now: string }) {
  if (!isResearchSnapshot(snapshot)) {
    return <section aria-label="Research lab" className="rounded-xl border border-slate-700 p-5"><h2 className="text-xl font-semibold">Research lab</h2><p role="status">Research evidence is unavailable. No result has been inferred.</p></section>;
  }
  const age = Date.parse(now) - Date.parse(snapshot.generatedAt);
  const stale = !Number.isFinite(age) || age < -60_000 || age > 180_000;
  return <section aria-label="Research lab" className="min-w-0 space-y-5 text-slate-100">
    <header><h2 className="text-xl font-semibold">Research lab</h2><p className="text-sm text-slate-300">Paper research evidence. These results do not approve a model or authorize trading.</p></header>
    <p role="status" className={stale ? "rounded border border-amber-500 p-3 text-amber-200" : "text-sm text-slate-300"}>{stale ? "Export is stale or its timestamp is invalid. Showing historical evidence only." : "Research export received. Check each source timestamp below."}</p>
    <p className="break-words text-xs text-slate-400">Exported: {snapshot.generatedAt}. Export time is not market-data time.</p>
    {snapshot.modules.map(module => <article key={module.id} className="min-w-0 rounded-xl border border-slate-700 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold">{module.title}</h3><span className="rounded border border-slate-600 px-2 py-1 text-xs">{module.status === "available" ? "Evidence available" : module.status === "incomplete" ? "Evidence incomplete" : "Unavailable"}</span></div>
      <p className="mt-2 text-sm text-slate-300">{module.reason}</p>
      <p className="mt-2 break-words text-xs text-slate-400">Last source observation: {module.observedAt ?? "Unavailable"}</p>
      {module.rows.length ? <div className="mt-4 grid gap-4 lg:grid-cols-2">{module.rows.map((row, index) => <div key={`${row.name}-${index}`} className="min-w-0 rounded border border-slate-700 p-3"><h4 className="break-words font-medium">{row.name}</h4><dl className="mt-3 space-y-2">{row.metrics.map((metric, i) => <div key={`${metric.label}-${i}`} className="grid grid-cols-2 gap-3 text-sm"><dt className="break-words text-slate-400">{metric.label}</dt><dd className="break-words text-right tabular-nums">{metric.value}</dd></div>)}</dl></div>)}</div> : <p className="mt-4 text-sm text-slate-400">No recorded evidence is available for this module.</p>}
    </article>)}
  </section>;
}
