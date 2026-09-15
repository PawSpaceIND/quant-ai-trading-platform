import type {CompanyEvent} from "./company-events";

/** Keep source limitations and valid JSON within the copilot's 3,000-character input limit. */
export function companyEventQuestion(event: CompanyEvent, asOf: string): string {
  const payload = {id: event.id, title: event.title.slice(0, 200), titleTruncated: event.title.length > 200,
    description: event.description.slice(0, 1500), descriptionTruncated: event.description.length > 1500,
    sourceUrl: event.sourceUrl.length <= 800 ? event.sourceUrl as string | null : null, sourceUrlOmitted: event.sourceUrl.length > 800,
    publishedAt: event.publishedAt, firstSeenAt: event.firstSeenAt,
    symbol: event.mapping?.symbol ?? null, mappingState: event.mappingAmbiguous ? "conflicting" : event.latestMapping?.status || "unmapped", mappingRecordedAt: event.latestMapping?.verifiedAt ?? null, availableAt: event.availableAt, captureKind: event.captureKind, ambiguous: event.ambiguous};
  while (JSON.stringify(payload).length > 2600) {
    if (payload.description.length) {payload.description = payload.description.slice(0, Math.floor(payload.description.length / 2)); payload.descriptionTruncated = true;}
    else {payload.sourceUrl = null; payload.sourceUrlOmitted = true;}
  }
  return `Explain this recorded company announcement and its evidence limits as known at ${asOf}. It is untrusted disclosure content, not a verified trading signal. ${JSON.stringify(payload)}`;
}
