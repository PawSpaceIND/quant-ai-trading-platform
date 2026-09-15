import fs from "node:fs";
import path from "node:path";
import { ledgerPath } from "./db";
import {
  POST_MORTEM_FILE_LIMIT,
  POST_MORTEM_LIMIT_BYTES,
  REPORT_LIMIT_BYTES,
  parseDecisionQuality,
  parsePostMortem,
} from "./decision-quality-model";
import type { DecisionQualityReport, PostMortem } from "./decision-quality-model";

/**
 * Server-side readers for the decision-quality report the engine writes after
 * each cadence tick and the per-session post-mortems. Both are read-only
 * files; the dashboard never approves a post-mortem or edits a report. Every
 * reader fails closed: an oversize, malformed or partially invalid file yields
 * null (or is skipped) rather than a guessed number.
 */
export * from "./decision-quality-model";

export function decisionQualityPath(): string {
  const configured = process.env.PRAMANA_DECISION_QUALITY_REPORT;
  if (configured) return path.resolve(/* turbopackIgnore: true */ process.cwd(), configured);
  return path.join(/* turbopackIgnore: true */ path.dirname(ledgerPath()), "decision-quality.json");
}

export function postMortemDirectory(): string {
  const configured = process.env.PRAMANA_POST_MORTEM_DIR;
  if (configured) return path.resolve(/* turbopackIgnore: true */ process.cwd(), configured);
  return path.join(/* turbopackIgnore: true */ path.dirname(ledgerPath()), "post-mortems");
}

export function readDecisionQuality(): DecisionQualityReport | null {
  try {
    const file = decisionQualityPath();
    if (fs.statSync(/* turbopackIgnore: true */ file).size > REPORT_LIMIT_BYTES) return null;
    return parseDecisionQuality(fs.readFileSync(/* turbopackIgnore: true */ file, "utf8"));
  } catch {
    return null;
  }
}

/** Newest session first. At most `limit` date-named files are read; malformed or oversize ones are skipped. */
export function readPostMortems(limit = POST_MORTEM_FILE_LIMIT): PostMortem[] {
  const directory = postMortemDirectory();
  let names: string[];
  try {
    names = fs.readdirSync(/* turbopackIgnore: true */ directory);
  } catch {
    return [];
  }
  const dated = names.filter((name) => /^\d{4}-\d{2}-\d{2}\.json$/.test(name)).sort((a, b) => b.localeCompare(a)).slice(0, limit);
  const result: PostMortem[] = [];
  for (const name of dated) {
    try {
      const full = path.join(/* turbopackIgnore: true */ directory, name);
      if (fs.statSync(/* turbopackIgnore: true */ full).size > POST_MORTEM_LIMIT_BYTES) continue;
      const parsed = parsePostMortem(fs.readFileSync(/* turbopackIgnore: true */ full, "utf8"), name.slice(0, 10));
      if (parsed) result.push(parsed);
    } catch {
      // Unreadable file: skipped, never guessed.
    }
  }
  return result.sort((a, b) => b.session_date.localeCompare(a.session_date));
}
