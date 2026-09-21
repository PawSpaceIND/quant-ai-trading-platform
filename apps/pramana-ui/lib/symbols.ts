import type {MarketInstrument} from "./market";

/** Exchange-qualified instrument identity, e.g. `NSE:INFY`.
 *
 * The market feed stores a bare `symbol` and keeps the exchange on the instrument, while
 * every reviewed company-event mapping is stored exchange-qualified. Anything comparing a
 * market row against a stored mapping has to qualify the row first; comparing the two
 * namespaces directly never matches. */
export const instrumentIdentity = (row: {symbol: string; instrument?: MarketInstrument}) =>
  `${row.instrument?.exchange || "UNKNOWN"}:${row.symbol}`;

/** True when a reviewed mapping symbol is on the operator's watchlist.
 *
 * The watchlist stores the feed's bare symbol (`INFY`); a mapping is stored qualified
 * (`NSE:INFY`). Accept either spelling so the filter can match. */
export const watchlisted = (symbol: string, favorites: readonly string[]) =>
  favorites.some(favorite => favorite === symbol || `NSE:${favorite}` === symbol);
