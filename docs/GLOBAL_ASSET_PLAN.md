# Global Asset Plan

## Initial regions
India and United States are the first execution regions. The domain model is global so additional jurisdictions can be added through adapters rather than strategy rewrites.

## Asset adapters
- Equities / ETFs / indices
- Listed options
- Futures
- FX
- Commodities
- Gold, silver, and other metals
- Bonds and fixed income
- Mutual/managed funds where data/execution APIs permit
- Digital assets only where legally and operationally supported

## Execution rule
An asset being represented in the platform does not imply that real-money trading is enabled. Each combination of tenant, jurisdiction, broker, account type, asset class, and strategy must pass an explicit eligibility gate.
