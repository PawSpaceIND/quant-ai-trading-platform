# Full Indian instrument discovery

The market monitor polls a bounded read-only quote list so one large broker master
cannot exhaust quote limits. It also refreshes Zerodha's exact instrument master
once per day through `KiteConnect.instruments()` and caches the response in the
shared data volume. Set `PRAMANA_ZERODHA_INSTRUMENTS_FILE` when an operator has
already downloaded a CSV/JSON master, or set
`PRAMANA_ZERODHA_INSTRUMENTS_CACHE` / `PRAMANA_INSTRUMENT_UNIVERSE_RECORDS`
to approved shared-volume paths.

The dashboard's **Complete available universe** panel reports counts by venue,
segment, asset class, instrument type and expiry, and links to
`/api/market/universe` for bounded filtering of exact records. This includes
all rows returned for NSE, BSE, NFO, BFO, MCX, CDS, BCD, NCDEX, MSEI and IFSC;
unsupported or non-India rows are excluded. A row is retained only when the
broker supplied an exact symbol. Derivatives carry expiry/strike/option type,
lot, tick, product and provider token whenever present.

Instrument-master visibility is evidence of availability only. It does not
establish broker entitlement, borrow/SLB availability, margin, settlement or
live-order approval. Options, futures, commodities, FX and short-side strategies
remain research/paper candidates until those independent checks and forward
paper evidence pass. The current execution gate remains paper-only and cash
equity/ETF scoped.
