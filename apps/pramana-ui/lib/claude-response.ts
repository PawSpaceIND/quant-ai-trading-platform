export type ChatMessage = { role: "user" | "assistant"; content: string };
type Attempt = {
  requestId: string | null;
  httpStatus: number;
  stopReason?: string | null;
  contentTypes?: string[];
  inputTokens?: number | null;
  outputTokens?: number | null;
};

/** One deadline across both attempts, including the backoff delay between them.
 *
 * 30s was too short for the questions this chat exists to answer. The system prompt carries
 * the whole evidence snapshot, the answer is allowed 1400 tokens, and a max_tokens stop
 * retries asking for double that - all inside this budget. Short questions finished and long
 * analytical ones timed out, which read as "Atlas is unreliable" rather than "the deadline is
 * too tight". Sized so two attempts at the doubled token count fit with room to spare.
 *
 * CONVERSATION_STALE_MS in lib/copilot.ts must stay well above this: that sweep marks any
 * still-pending row failed, so a shorter one would fail requests that are still running.
 */
export const REQUEST_DEADLINE_MS = 120_000;

/** Never store raw provider bodies: errors can contain submitted private context. */
export async function requestClaude(
  payload: { model: string; max_tokens: number; system: string; messages: ChatMessage[] },
  key: string,
  transport: typeof fetch = fetch,
  reserveRetry: () => boolean = () => false,
) {
  const attempts: Attempt[] = [];
  const signal = AbortSignal.timeout(REQUEST_DEADLINE_MS);
  let request = payload;
  let answer: string | null = null;
  let error: string | null = null;
  try {
    for (let index = 0; index < 2; index++) {
      const response = await transport("https://api.anthropic.com/v1/messages", {
        method: "POST", signal,
        headers: { "Content-Type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": key },
        body: JSON.stringify(request),
      });
      const attempt: Attempt = {
        requestId: response.headers.get("request-id"), httpStatus: response.status,
      };
      attempts.push(attempt);
      if (!response.ok) {
        const retryAfter = response.headers.get("retry-after");
        const delay = retryAfter === null ? 250 : /^\d+(\.\d+)?$/.test(retryAfter)
          ? Number(retryAfter) * 1000 : Date.parse(retryAfter) - Date.now();
        if (index === 0 && [429, 500, 502, 503, 504, 529].includes(response.status)
          && Number.isFinite(delay) && delay >= 0 && delay <= 2000 && reserveRetry()) {
          await response.body?.cancel();
          await new Promise((resolve) => setTimeout(resolve, delay));
          signal.throwIfAborted();
          continue;
        }
        throw new Error(`Claude request failed (${response.status}). ${response.status === 429 ? "Provider rate limit reached; please wait before retrying." : "Please retry later or ask the operator to check saved diagnostics."}`);
      }
      const data = await response.json();
      attempt.stopReason = typeof data?.stop_reason === "string" ? data.stop_reason : null;
      const blocks = Array.isArray(data?.content) ? data.content : [];
      attempt.contentTypes = blocks.map((b: { type?: unknown } | null) =>
        typeof b?.type === "string" ? b.type : "unknown");
      attempt.inputTokens = typeof data?.usage?.input_tokens === "number" ? data.usage.input_tokens : null;
      attempt.outputTokens = typeof data?.usage?.output_tokens === "number" ? data.usage.output_tokens : null;
      const text = blocks.filter((b: { type?: string; text?: unknown } | null) =>
        b?.type === "text" && typeof b.text === "string")
        .map((b: { text: string }) => b.text).join("\n").trim();
      if (["tool_use", "pause_turn"].includes(attempt.stopReason ?? ""))
        throw new Error("Claude requested a tool or continuation that this read-only chat does not support. No tool was executed.");
      if (text) {
        answer = text;
        if (["max_tokens", "model_context_window_exceeded"].includes(attempt.stopReason ?? ""))
          answer += "\n\n[Response reached its output or context limit and may be incomplete.]";
        break;
      }
      if (index === 0 && ["end_turn", "max_tokens"].includes(attempt.stopReason ?? "") && reserveRetry()) {
        request = {
          ...payload,
          max_tokens: attempt.stopReason === "max_tokens" ? payload.max_tokens * 2 : payload.max_tokens,
          messages: [...payload.messages, { role: "user", content: "Please answer the preceding question in concise visible text using the supplied evidence. If evidence is missing, explain what is missing." }],
        };
        continue;
      }
      throw new Error(attempt.stopReason === "refusal"
        ? "Claude declined this request. Try rephrasing the question."
        : `Claude returned no readable answer (stop reason: ${attempt.stopReason ?? "unknown"}). Diagnostics were saved; you can retry the question.`);
    }
  } catch (e) {
    error = signal.aborted || (e instanceof Error && ["TimeoutError", "AbortError"].includes(e.name))
      ? "Claude timed out. Provider completion and billing may be unknown; no automatic retry was made after the timeout."
      : e instanceof TypeError ? "Could not reach Claude or read its response. Please retry later."
      : e instanceof SyntaxError ? "Claude returned an unreadable response. Please retry later."
      : e instanceof Error ? e.message : "Copilot unavailable";
  }
  const total = (field: "inputTokens" | "outputTokens") => {
    const known = attempts.map(a => a[field]).filter((n): n is number => typeof n === "number");
    return known.length ? known.reduce((sum, n) => sum + n, 0) : null;
  };
  return { answer, error, usage: {
    inputTokens: total("inputTokens"), outputTokens: total("outputTokens"),
    estimatedCostUsd: null, costNote: "Pricing is not configured; known token usage retained across attempts.",
    attempts,
  } };
}
