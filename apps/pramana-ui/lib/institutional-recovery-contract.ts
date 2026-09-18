/** Browser-safe bounded contracts. No service key, database path or raw source text. */
export const RECOVERY_CONFIRMATION = "reconcile_recorded_paper_bookkeeping_only";
export const recoveryId = (value: unknown): value is string =>
  typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,179}$/.test(value);
const hash = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
export type RecoveryPreview = {tenant_id: string; program_id: string; context_sha256: string;
  program_state: string; slice_states: string[]; source_revision_sha256: string;
  execution_authorized: false; confirmation_required: string; canApply: boolean};
export type RecoveryOutcome = {tenant_id: string; request_id: string; program_id: string;
  actor_key_id: string; context_sha256: string; status: "REQUESTED" | "RETURNED" | "FAILED";
  recorded_at: string; replayed: boolean; execution_authorized: false;
  result: null | {program_state?: string; recovery_stage?: string; committed_order_ids?: string[];
    recovered_sequences?: number[]; execution_authorized?: false; reason_code?: string; code?: string}};
const programStates = ["PLANNED", "ACTIVE", "COMPLETE", "FAILED", "CANCELLED"];
const sliceStates = ["PENDING", "DISPATCHING", "FILLED_UNACCOUNTED", "EXECUTED", "FAILED", "CANCELLED"];
export function recoveryPreview(value: unknown, tenant: string, program: string, canApply: boolean): RecoveryPreview {
  if (!record(value) || value.tenant_id !== tenant || value.program_id !== program ||
      value.execution_authorized !== false || !hash(value.context_sha256) ||
      !hash(value.source_revision_sha256) || !programStates.includes(String(value.program_state)) ||
      !Array.isArray(value.slice_states) || value.slice_states.length !== 1 ||
      !value.slice_states.every(v => typeof v === "string" && sliceStates.includes(v)) ||
      value.confirmation_required !== RECOVERY_CONFIRMATION) throw new Error("Invalid recovery preview");
  return {tenant_id: tenant, program_id: program, context_sha256: value.context_sha256,
    program_state: String(value.program_state), slice_states: value.slice_states as string[],
    source_revision_sha256: value.source_revision_sha256, confirmation_required: RECOVERY_CONFIRMATION,
    execution_authorized: false, canApply};
}
export function recoveryOutcome(value: unknown, tenant: string, request: string,
    expected?: {program: string; context: string}): RecoveryOutcome {
  if (!record(value) || value.tenant_id !== tenant || value.request_id !== request ||
      !recoveryId(value.program_id) || !recoveryId(value.actor_key_id) || !hash(value.context_sha256) ||
      value.execution_authorized !== false || typeof value.replayed !== "boolean" ||
      typeof value.recorded_at !== "string" || value.recorded_at.length > 40 ||
      !/(Z|[+-]\d{2}:\d{2})$/.test(value.recorded_at) || !Number.isFinite(Date.parse(value.recorded_at)) ||
      (expected && (expected.program !== value.program_id || expected.context !== value.context_sha256)) ||
      !["REQUESTED", "RETURNED", "FAILED"].includes(String(value.status))) throw new Error("Invalid recovery outcome");
  let result: RecoveryOutcome["result"] = null;
  if (value.status === "REQUESTED") { if(value.result !== null) throw new Error("Invalid pending record"); }
  else if (value.status === "FAILED") {
    if(!record(value.result) || value.result.code !== "institutional_reconciliation_requires_review") throw new Error("Invalid failure");
    result = {code: "institutional_reconciliation_requires_review"};
  } else {
    const r = value.result;
    if(!record(r) || r.tenant_id !== tenant || r.program_id !== value.program_id || r.execution_authorized !== false ||
       !programStates.includes(String(r.program_state)) || !["READY", "COMPLETE", "FAILED", "RECOVERY_REQUIRED"].includes(String(r.recovery_stage)) ||
       !["recorded_complete", "review_required"].includes(String(r.reason_code)) || !hash(r.source_revision_sha256) ||
       !Array.isArray(r.committed_order_ids) || r.committed_order_ids.length > 1 || !r.committed_order_ids.every(recoveryId) ||
       !Array.isArray(r.recovered_sequences) || r.recovered_sequences.length > 1 || !r.recovered_sequences.every(n => n === 1))
      throw new Error("Invalid recorded result");
    result = {program_state:String(r.program_state), recovery_stage:String(r.recovery_stage),
      committed_order_ids:r.committed_order_ids as string[], recovered_sequences:r.recovered_sequences as number[],
      reason_code:String(r.reason_code), execution_authorized:false};
  }
  return {tenant_id:tenant, request_id:request, program_id:value.program_id, actor_key_id:value.actor_key_id,
    context_sha256:value.context_sha256, status:value.status as RecoveryOutcome["status"], recorded_at:value.recorded_at,
    replayed:value.replayed, execution_authorized:false, result};
}
export function recoveryPayload(value: unknown): {program_id: string; request_id: string;
    expected_context_sha256: string; confirmation: string} {
  if(!record(value) || Object.keys(value).sort().join(",") !== "confirmation,expected_context_sha256,program_id,request_id" ||
     !recoveryId(value.program_id) || !recoveryId(value.request_id) || !hash(value.expected_context_sha256) ||
     value.confirmation !== RECOVERY_CONFIRMATION) throw new Error("Invalid recovery confirmation");
  return {program_id:value.program_id, request_id:value.request_id,
    expected_context_sha256:value.expected_context_sha256, confirmation:RECOVERY_CONFIRMATION};
}
