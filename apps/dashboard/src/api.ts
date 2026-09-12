export type Readiness = { mode: string; ready: boolean; blockers: string[] };
export type UsageRecord = { tenant_id: string; metric: string; count: number };
export type UsageResponse = { tenant_id: string; usage: UsageRecord[] };
export type Position = { tenant_id: string; symbol: string; quantity: number; market_value: string };
export type PortfolioResponse = { tenant_id: string; positions: Position[] };

export class QuantApi {
  constructor(private baseUrl: string, private apiKey: string) {}

  private async request<T>(path: string): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      headers: { "X-API-Key": this.apiKey }
    });
    if (!response.ok) {
      throw new Error(`${response.status}: ${await response.text()}`);
    }
    return response.json() as Promise<T>;
  }

  readiness() { return this.request<Readiness>("/v1/readiness"); }
  usage() { return this.request<UsageResponse>("/v1/usage"); }
  portfolio() { return this.request<PortfolioResponse>("/v1/portfolio"); }
}
