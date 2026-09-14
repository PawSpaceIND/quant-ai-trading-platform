const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const filename = path.join(__dirname, "../components/research-lab-panel.tsx");
const source = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 },
}).outputText;
const compiled = { exports: {} };
new Function("require", "module", "exports", source)(require, compiled, compiled.exports);
const { isResearchSnapshot, ResearchLabPanel } = compiled.exports;
const snapshot = {
  schemaVersion: 1, paperOnly: true, generatedAt: "2026-09-14T10:00:00Z",
  modules: ["comparison", "simulation", "companyEvents"].map(id => ({
    id, title: id, status: "incomplete", observedAt: null, reason: "Evidence pending",
    rows: [{ name: "<script>alert(1)</script>", metrics: [{ label: "Equity", value: "Unavailable" }] }],
  })),
};
assert.equal(isResearchSnapshot(snapshot), true);
assert.equal(isResearchSnapshot({ ...snapshot, paperOnly: false }), false);
assert.equal(isResearchSnapshot({ ...snapshot, generatedAt: "2026-09-14T10:00:00" }), false);
assert.equal(isResearchSnapshot({ ...snapshot, modules: [snapshot.modules[0], snapshot.modules[0], snapshot.modules[2]] }), false);
assert.equal(isResearchSnapshot({ ...snapshot, modules: [{ ...snapshot.modules[0], rows: Array(51).fill(snapshot.modules[0].rows[0]) }, ...snapshot.modules.slice(1)] }), false);
let html = renderToStaticMarkup(React.createElement(ResearchLabPanel, { snapshot, now: "2026-09-14T10:04:00Z" }));
assert.ok(html.includes("Export is stale"));
assert.ok(html.includes("Unavailable"));
assert.ok(html.includes("&lt;script&gt;"));
assert.ok(!html.includes("<script>"));
assert.ok(!html.includes("<button"));
html = renderToStaticMarkup(React.createElement(ResearchLabPanel, { snapshot: {}, now: "2026-09-14T10:00:00Z" }));
assert.ok(html.includes("No result has been inferred"));
html = renderToStaticMarkup(React.createElement(ResearchLabPanel, { snapshot, now: "2026-09-14T09:00:00Z" }));
assert.ok(html.includes("Export is stale"));
console.log("Research panel: schema, boundaries, missing/stale/future data, escaping and read-only rendering passed.");
