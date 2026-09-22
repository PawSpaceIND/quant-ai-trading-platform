/**
 * Refusals the server raises before it contacts the recovery gateway.
 *
 * For these the gateway was never reached, so no request id exists there and no
 * reconciliation can be outstanding: the panel releases its saved attempt and says so.
 * Everything else keeps the lock, because an unknown outcome may still be in flight.
 * Kept out of the component so the rule can be tested against the client that raises
 * these codes, rather than restated by hand in two places.
 */
export const NOT_DISPATCHED: Record<string, string> = {
  recovery_context_changed: "The programme changed since you inspected it. Nothing was submitted. Inspect it again to review the current state.",
  recovery_configuration_changed: "The recovery configuration changed since you inspected it. Nothing was submitted. Inspect the programme again.",
  sign_in_required: "Your session expired before the request was sent. Nothing was submitted. Sign in again, then inspect the programme.",
  invalid_request_origin: "The request origin was rejected. Nothing was submitted.",
  invalid_recovery_request: "The request was rejected as invalid. Nothing was submitted.",
  recovery_credential_unavailable: "Recovery credentials are unavailable on this server. Nothing was submitted.",
  recovery_not_configured: "Recovery is not configured on this server. Nothing was submitted.",
  // The apply path re-inspects the programme before it sends anything, so an observation
  // that never arrived refused the request in front of the gateway, not behind it.
  recovery_observation_unavailable: "The programme could not be re-read before sending. Nothing was submitted. Inspect the programme again.",
};

/** Codes that mean the gateway was reached and the outcome is unknown. The lock holds. */
export const OUTCOME_UNKNOWN = ["recovery_outcome_unknown"] as const;
