import type { Runtime } from "../lib/pilot";
import { readRegimeObservation, type RegimeFrame } from "../lib/regime-observation";

const reasons: Record<string,string> = {
  daily_classified: "Classified daily context takes priority",
  intraday_fallback: "Daily unclassified; using intraday context",
  neither_classified_most_bars_daily_tiebreak: "Neither classified; showing the most bars (daily wins ties)",
};
function Frame({frame}:{frame:RegimeFrame}) {
  return <><div>{frame.label}</div><div className="muted">{frame.barsUsed} / {frame.barsAvailable} bars</div></>;
}
export function RegimeContextTable({rows,now=Date.now()}:{rows:NonNullable<Runtime["watchlist"]>;now?:number}) {
  return <div aria-label="Last analysed regime context">
    <h3>Last analysed regime context</h3>
    <p className="footnote">Bars scored / available are reported separately for daily and 15-minute context. The technical count is one-minute bars, not regime bars. These are timestamped observations, not current signals or proof that risk gates are ready. After restart, no observation is claimed until analysis runs.</p>
    <div className="research-table-scroll" role="region" tabIndex={0} aria-label="Regime timeframe observations">
      <table><thead><tr><th scope="col">Instrument</th><th scope="col">Context time</th><th scope="col">Technical 1m bars</th><th scope="col">Daily 1d</th><th scope="col">Intraday 15m</th><th scope="col">Selected context</th></tr></thead>
        <tbody>{rows.map((row,index)=>{
          const context=readRegimeObservation(row.regimeContext,now);
          return <tr key={`${row.market}:${row.exchange}:${row.assetClass}:${row.symbol}:${index}`}>
            <th scope="row">{row.symbol}</th>
            {context?<>
              <td><time dateTime={context.observedAt}>{context.observedAt}</time><div className="muted">{Math.floor(context.ageSeconds)}s since context</div></td>
              <td>{context.technicalBars}</td>
              <td><Frame frame={context.daily}/></td><td><Frame frame={context.intraday}/></td>
              <td>{context.selectedLabel} · {context.selectedTimeframe}<div className="muted">{reasons[context.selectionReason]}</div><div className="muted">Minimum {context.minimumBars}; lookback {context.lookbackBars} bars</div></td>
            </>:<td colSpan={5}>No valid recorded regime context. Counts and labels are unknown; refresh after analysis.</td>}
          </tr>;
        })}</tbody>
      </table>
    </div>
  </div>;
}
