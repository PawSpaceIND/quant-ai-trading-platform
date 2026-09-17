/** Observations only: never a recovery approval or an account-readiness verdict. */
export type PaperOmsRow = {
  clientOrderId: string; symbol: string; side: "BUY" | "SELL"; state: string;
  requestedQuantity: number; filledQuantity: number; updatedAt: string;
};
export type PaperOmsObservation = {
  schema: "pramana.paper_oms_observation.v1";
  status: "not_configured" | "unavailable" | "observed";
  reason: string; observedAt: string; tenantId: string;
  totalOrders: number | null; openOrders: number | null;
  recordedRecoveryAudits: number | null; orders: PaperOmsRow[]; truncated: boolean;
  historyVerified: false; recoveryAuthorized: false; liveExecutionAuthorized: false;
};
