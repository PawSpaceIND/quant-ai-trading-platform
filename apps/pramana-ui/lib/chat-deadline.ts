/** One deadline across both attempts of an Atlas question, including the backoff between.
 *
 * It lives alone so the browser and the server can share it. The client used to keep its
 * own 45 seconds; when the server budget was raised to 120 the client was not, so it
 * became the binding limit, and because an abort does not cancel the server the answer
 * still completed, was billed and was saved while the operator saw a failure and asked
 * again. Anything the browser waits with must be this plus a margin, never less.
 */
export const REQUEST_DEADLINE_MS = 120_000;

/** What the browser waits, allowing for the round trip either side of the server budget. */
export const CHAT_DEADLINE_MS = REQUEST_DEADLINE_MS + 15_000;
