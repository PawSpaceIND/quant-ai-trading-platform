import { test } from "node:test";
import assert from "node:assert/strict";
import { requestClaude, REQUEST_DEADLINE_MS } from "../lib/claude-response";
import { CONVERSATION_STALE_MS } from "../lib/copilot";

const payload = { model: "test-model", max_tokens: 1400, system: "Evidence only", messages: [{ role: "user" as const, content: "Explain feed status" }] };
function reply(content: unknown, stop_reason = "end_turn") {
  return Response.json({ content, stop_reason, usage: { input_tokens: 10, output_tokens: 20 } }, { headers: { "request-id": "req_test" } });
}
function mock(responses: Array<Response | Error>) {
  const sent: typeof payload[] = [];
  const transport = (async (_url: unknown, init: RequestInit) => {
    sent.push(JSON.parse(String(init.body)));
    const next = responses.shift();
    assert.ok(next, "Unexpected extra provider call");
    if (next instanceof Error) throw next;
    return next;
  }) as typeof fetch;
  return { transport, sent };
}
test("text is extracted safely and diagnostics retained", async () => {
  const m = mock([reply([null, { type: "thinking" }, { type: "text", text: "Feed stale" }, { type: "text", text: 1 }])]);
  const result = await requestClaude(payload, "test-key", m.transport);
  assert.equal(result.answer, "Feed stale");
  assert.equal(result.error, null);
  assert.equal(result.usage.attempts[0].requestId, "req_test");
  assert.equal(result.usage.outputTokens, 20);
});
test("empty end_turn recovers once with new instruction and budget reservation", async () => {
  const m = mock([reply([]), reply([{ type: "text", text: "Recovered" }])]);
  let reserved = 0;
  const result = await requestClaude(payload, "test-key", m.transport, () => { reserved++; return true; });
  assert.equal(result.answer, "Recovered");
  assert.equal(reserved, 1);
  assert.equal(m.sent[1].messages.length, 2);
  assert.equal(result.usage.inputTokens, 20);
});
test("thinking-only token exhaustion increases output budget once", async () => {
  const m = mock([reply([{ type: "thinking" }], "max_tokens"), reply([{ type: "text", text: "Recovered" }])]);
  await requestClaude(payload, "test-key", m.transport, () => true);
  assert.equal(m.sent[1].max_tokens, 2800);
});
test("empty responses stop after two attempts with diagnostics", async () => {
  const m = mock([reply([]), reply([])]);
  const result = await requestClaude(payload, "test-key", m.transport, () => true);
  assert.match(result.error!, /stop reason: end_turn/);
  assert.equal(result.usage.attempts.length, 2);
});
test("exhausted budget prevents recovery call", async () => {
  const m = mock([reply([])]);
  const result = await requestClaude(payload, "test-key", m.transport, () => false);
  assert.ok(result.error);
  assert.equal(m.sent.length, 1);
});
test("temporary provider failure retries within the budget", async () => {
  const m = mock([new Response(null, { status: 529, headers: { "retry-after": "0" } }), reply([{ type: "text", text: "OK" }])]);
  const result = await requestClaude(payload, "test-key", m.transport, () => true);
  assert.equal(result.answer, "OK");
  assert.equal(result.usage.attempts[0].httpStatus, 529);
});
test("long Retry-After is respected without immediate retry", async () => {
  const m = mock([new Response(null, { status: 429, headers: { "retry-after": "60" } })]);
  const result = await requestClaude(payload, "test-key", m.transport, () => true);
  assert.match(result.error!, /rate limit/);
  assert.equal(m.sent.length, 1);
});
test("auth failures are not retried and raw error bodies are not exposed", async () => {
  const m = mock([new Response("private-context", { status: 401 })]);
  const result = await requestClaude(payload, "test-key", m.transport, () => true);
  assert.match(result.error!, /401/);
  assert.ok(!JSON.stringify(result).includes("private-context"));
  assert.equal(m.sent.length, 1);
});
test("ambiguous network and timeout failures never automatically retry", async () => {
  for (const error of [new TypeError("fetch failed"), new DOMException("deadline", "TimeoutError")]) {
    const m = mock([error]);
    const result = await requestClaude(payload, "test-key", m.transport, () => true);
    assert.ok(result.error);
    assert.equal(m.sent.length, 1);
  }
});
test("refusals and unsupported tool turns do not retry", async () => {
  for (const reason of ["refusal", "tool_use", "pause_turn"]) {
    const m = mock([reply([], reason)]);
    const result = await requestClaude(payload, "test-key", m.transport, () => true);
    assert.ok(result.error);
    assert.equal(m.sent.length, 1);
  }
});
test("partial text is visibly labelled incomplete", async () => {
  const m = mock([reply([{ type: "text", text: "Partial" }], "max_tokens")]);
  const result = await requestClaude(payload, "test-key", m.transport);
  assert.match(result.answer!, /may be incomplete/);
});

test("the stale sweep outlives the request deadline, so a live request is never called failed", () => {
  // These two are a pair. The sweep cannot tell a request that died from one still waiting on
  // the provider, so it marks any row still pending past its threshold as failed. If that
  // threshold ever drops to or below the request deadline, a request that is still legitimately
  // running gets reported to the operator as an error - which is what "Atlas keeps failing"
  // looked like when the deadline was 30s and long answers could not finish inside it.
  assert.ok(
    CONVERSATION_STALE_MS > REQUEST_DEADLINE_MS,
    `sweep ${CONVERSATION_STALE_MS}ms must outlast the request deadline ${REQUEST_DEADLINE_MS}ms`,
  );
  // Two attempts plus the backoff between them have to fit, or the retry path is dead on arrival.
  assert.ok(REQUEST_DEADLINE_MS >= 60_000, "a 1400-token answer over a full snapshot needs room");
});
