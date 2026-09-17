import test, {type TestContext} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {createHash} from "node:crypto";
import {DatabaseSync} from "node:sqlite";
import {NextRequest} from "next/server";
import {GET} from "../app/api/paper-oms/route";
import {makeSession, SESSION_COOKIE} from "../lib/auth";
import {tenantId} from "../lib/db";
import {MAX_OMS_DETAILS, MAX_OMS_ORDERS, readPaperOms} from "../lib/paper-oms";

const digest = (raw: string) => createHash("sha256").update(raw).digest("hex");
const identity = '{"symbol":"INFY","market":"INDIA","asset_class":"EQUITY","currency":"INR","exchange":"NSE"}';
const instant = "2026-09-16T10:00:00+00:00";
function fixture(t: TestContext, tenant=tenantId) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pramana-oms-observation-"));
  const ledgerFile = path.join(dir, "paper.sqlite"), omsFile = path.join(dir, "oms.sqlite");
  const ledger = new DatabaseSync(ledgerFile), oms = new DatabaseSync(omsFile);
  ledger.exec(`CREATE TABLE paper_runtime_order_identity(tenant_id TEXT PRIMARY KEY,payload TEXT,sha256 TEXT);
    CREATE TABLE paper_ledger(tenant_id TEXT,order_id TEXT);`);
  oms.exec(`CREATE TABLE oms_meta(id INTEGER PRIMARY KEY, version INTEGER);
    INSERT INTO oms_meta VALUES(1,1);
    CREATE TABLE oms_orders(client_order_id TEXT PRIMARY KEY,tenant_id TEXT,symbol TEXT,market TEXT,
      asset_class TEXT,side TEXT,state TEXT,requested_quantity INTEGER,filled_quantity INTEGER,
      updated_at TEXT,instrument_identity TEXT);
    CREATE TABLE oms_events(client_order_id TEXT);
    CREATE TABLE oms_fills(client_order_id TEXT);`);
  const configuration = {schema: "pramana.runtime_order_identity.v1", mode: "bound_v1",
    instruments: {INFY: identity}, oms_path_sha256: digest(fs.realpathSync(omsFile))};
  const configure = (value: unknown) => {
    const raw = JSON.stringify(value);
    ledger.prepare("INSERT OR REPLACE INTO paper_runtime_order_identity VALUES(?,?,?)").run(tenant, raw, digest(raw));
  };
  configure(configuration);
  function order(id="OMS-pending", state="SUBMISSION_UNCERTAIN", owner=tenant, filled=0) {
    oms.prepare("INSERT INTO oms_orders VALUES(?,?,?,?,?,?,?,?,?,?,?)")
      .run(id, owner, "INFY", "INDIA", "EQUITY", "BUY", state, 10, filled, instant, identity);
  }
  order();
  t.after(() => {ledger.close(); oms.close(); fs.rmSync(dir, {recursive: true, force: true});});
  const inspect = () => readPaperOms({ledger: ledgerFile, oms: omsFile, tenant});
  return {dir,ledgerFile,omsFile,ledger,oms,configuration,configure,order,inspect,tenant};
}

test("observation shows pending state but never recovery or live authority", t => {
  const f=fixture(t), before=[fs.readFileSync(f.ledgerFile),fs.readFileSync(f.omsFile)];
  const r=f.inspect();
  assert.equal(r.status,"observed"); assert.equal(r.openOrders,1); assert.equal(r.totalOrders,1);
  assert.equal(r.orders[0].state,"SUBMISSION_UNCERTAIN");
  assert.equal(r.historyVerified,false); assert.equal(r.recoveryAuthorized,false); assert.equal(r.liveExecutionAuthorized,false);
  assert.deepEqual([fs.readFileSync(f.ledgerFile),fs.readFileSync(f.omsFile)],before);
  assert.doesNotMatch(JSON.stringify(r),new RegExp(f.dir));
});

test("inventory is tenant-scoped and does not disclose another tenant's state", t => {
  const f=fixture(t); f.order("OTHER-TENANT-PRIVATE","INVALID_OTHER_STATE","another");
  const r=f.inspect(); assert.equal(r.status,"observed"); assert.equal(r.totalOrders,1);
  assert.doesNotMatch(JSON.stringify(r),/OTHER-TENANT-PRIVATE|another|INVALID_OTHER_STATE/);
});

test("all nonterminal states count; terminal states are not presented as pending", t => {
  const f=fixture(t); f.oms.exec("DELETE FROM oms_orders");
  for (const [i,state] of ["CREATED","RISK_APPROVED","SUBMITTED","SUBMISSION_UNCERTAIN","PARTIALLY_FILLED","FILLED","REJECTED","CANCELLED"].entries())
    f.order(`OMS-${i}`,state,f.tenant,state==="FILLED"?10:state==="PARTIALLY_FILLED"?4:0);
  const r=f.inspect(); assert.equal(r.status,"observed"); assert.equal(r.openOrders,5); assert.equal(r.totalOrders,8);
  assert.equal(r.orders.find(x=>x.state==="PARTIALLY_FILLED")?.filledQuantity,4);
});

test("bounded detail list exposes truncation and full pending count", t => {
  const f=fixture(t);
  for(let i=1;i<=MAX_OMS_DETAILS;i++) f.order(`OMS-${i}`);
  const r=f.inspect(); assert.equal(r.status,"observed"); assert.equal(r.orders.length,MAX_OMS_DETAILS);
  assert.equal(r.openOrders,MAX_OMS_DETAILS+1); assert.equal(r.truncated,true);
});

test("oversized inventory refuses rather than inventing a complete count", t => {
  const f=fixture(t); f.oms.exec("BEGIN");
  for(let i=1;i<=MAX_OMS_ORDERS;i++) f.order(`OMS-${i}`);
  f.oms.exec("COMMIT"); const r=f.inspect();
  assert.equal(r.status,"unavailable"); assert.equal(r.totalOrders,null); assert.deepEqual(r.orders,[]);
});

for (const defect of ["unknown_state","negative_quantity","fractional_quantity","overfill","unfilled_terminal","wrong_contract","future_time","naive_time","wrong_market","wrong_side","bad_identifier"]) {
  test(`malformed selected order refuses: ${defect}`, t => {
    const f=fixture(t);
    const changes: Record<string,[string,string|number]> = {
      unknown_state:["state","UNKNOWN"], negative_quantity:["requested_quantity",-1], fractional_quantity:["requested_quantity",1.5],
      overfill:["filled_quantity",11], unfilled_terminal:["state","FILLED"], wrong_contract:["instrument_identity","{}"],
      future_time:["updated_at","2999-01-01T00:00:00Z"], naive_time:["updated_at","2026-09-16T10:00:00"],
      wrong_market:["market","US"], wrong_side:["side","OTHER"], bad_identifier:["client_order_id","bad\nidentity"],
    };
    const [column,value]=changes[defect]; f.oms.prepare(`UPDATE oms_orders SET ${column}=?`).run(value);
    const r=f.inspect(); assert.equal(r.status,"unavailable"); assert.equal(r.openOrders,null); assert.deepEqual(r.orders,[]);
  });
}

for (const defect of ["checksum","path","missing_binding","mode","missing_table","schema_version","same_file"]) {
  test(`mismatched account or storage is unavailable: ${defect}`, t => {
    const f=fixture(t);
    if(defect==="checksum") f.ledger.exec("UPDATE paper_runtime_order_identity SET sha256='wrong'");
    if(defect==="path") f.configure({...f.configuration,oms_path_sha256:"a".repeat(64)});
    if(defect==="missing_binding") f.ledger.exec("DELETE FROM paper_runtime_order_identity");
    if(defect==="mode") f.configure({...f.configuration,mode:"legacy_cash"});
    if(defect==="missing_table") f.oms.exec("DROP TABLE oms_events");
    if(defect==="schema_version") f.oms.exec("UPDATE oms_meta SET version=99");
    const r=defect==="same_file" ? readPaperOms({ledger:f.ledgerFile,oms:f.ledgerFile,tenant:f.tenant}) : f.inspect();
    assert.equal(r.status,"unavailable"); assert.equal(r.totalOrders,null);
  });
}

test("missing configured files are not created or treated as an empty account", t => {
  const f=fixture(t), missing=path.join(f.dir,"absent.sqlite");
  const r=readPaperOms({ledger:f.ledgerFile,oms:missing,tenant:f.tenant});
  assert.equal(r.status,"unavailable"); assert.equal(fs.existsSync(missing),false);
});

test("bound account without selected OMS path is unavailable", t => {
  const f=fixture(t); assert.equal(readPaperOms({ledger:f.ledgerFile,tenant:f.tenant}).status,"unavailable");
});

test("legacy account without bound configuration is explicitly not configured", t => {
  const f=fixture(t); f.ledger.exec("DELETE FROM paper_runtime_order_identity");
  const r=readPaperOms({ledger:f.ledgerFile,tenant:f.tenant});
  assert.equal(r.status,"not_configured"); assert.equal(r.openOrders,null);
});

test("empty OMS replacement with paper history cannot appear clean", t => {
  const f=fixture(t); f.oms.exec("DELETE FROM oms_orders");
  f.ledger.prepare("INSERT INTO paper_ledger VALUES(?,?)").run(f.tenant,"committed-paper-fill");
  assert.equal(f.inspect().status,"unavailable");
});

test("empty stored book remains only an observation, not verified reconciliation", t => {
  const f=fixture(t); f.oms.exec("DELETE FROM oms_orders"); const r=f.inspect();
  assert.equal(r.status,"observed"); assert.equal(r.openOrders,0); assert.equal(r.historyVerified,false);
});

for (const kind of ["symlink","hardlink"]) test(`file aliases refuse: ${kind}`, t => {
  const f=fixture(t), alias=path.join(f.dir,"alias.sqlite");
  if(kind==="symlink") fs.symlinkSync(f.omsFile,alias); else fs.linkSync(f.omsFile,alias);
  assert.equal(readPaperOms({ledger:f.ledgerFile,oms:alias,tenant:f.tenant}).status,"unavailable");
});

test("version-two audit counts remain tenant-scoped observations", t => {
  const f=fixture(t); f.oms.exec("UPDATE oms_meta SET version=2; CREATE TABLE oms_paper_recoveries(tenant_id TEXT)");
  f.oms.prepare("INSERT INTO oms_paper_recoveries VALUES(?)").run(f.tenant);
  f.oms.prepare("INSERT INTO oms_paper_recoveries VALUES(?)").run("another");
  const r=f.inspect(); assert.equal(r.status,"observed"); assert.equal(r.recordedRecoveryAudits,1);
  assert.equal(r.historyVerified,false);
});

test("missing audit table on recovered schema is unavailable", t => {
  const f=fixture(t); f.oms.exec("UPDATE oms_meta SET version=2"); assert.equal(f.inspect().status,"unavailable");
});

function routeEnvironment(t: TestContext, f: ReturnType<typeof fixture>) {
  for (const [key,value] of Object.entries({PRAMANA_DASHBOARD_SECRET:"synthetic-unit-session-secret-at-least-32-characters",
    PRAMANA_LEDGER_PATH:f.ledgerFile,PRAMANA_OMS_DB:f.omsFile})) {
    const previous=process.env[key]; process.env[key]=value;
    t.after(()=>{if(previous===undefined) delete process.env[key]; else process.env[key]=previous;});
  }
}
function request(query="", session?:string) {
  return new NextRequest(`http://localhost/api/paper-oms${query}`, {headers: session ? {cookie:`${SESSION_COOKIE}=${session}`} : {}});
}

for(const missing of ["session","bad_session","configuration"]) test(`route refuses before filesystem access: ${missing}`, async t => {
  const f=fixture(t); routeEnvironment(t,f);
  if(missing==="configuration") delete process.env.PRAMANA_DASHBOARD_SECRET;
  const spy=t.mock.method(fs,"lstatSync",()=>{throw new Error("must not access storage");});
  const response=await GET(request("",missing==="bad_session"?"invalid":undefined));
  assert.equal(response.status,missing==="configuration"?503:401); assert.equal(spy.mock.callCount(),0);
  assert.equal(response.headers.get("Cache-Control"),"no-store");
});

for(const query of ["?tenant=another","?database=/tmp/other","?action=recover"]) test(`authenticated route does not accept account/path/action override: ${query}`, async t => {
  const f=fixture(t); routeEnvironment(t,f);
  const spy=t.mock.method(fs,"lstatSync",()=>{throw new Error("must not access storage");});
  const response=await GET(request(query,makeSession())); assert.equal(response.status,400); assert.equal(spy.mock.callCount(),0);
});

test("authenticated API reads actual selected SQLite state without changing either database", async t => {
  const f=fixture(t); routeEnvironment(t,f);
  const before=[fs.readFileSync(f.ledgerFile),fs.readFileSync(f.omsFile)];
  const response=await GET(request("",makeSession())), body=await response.json();
  assert.equal(response.status,200); assert.equal(body.openOrders,1); assert.equal(body.tenantId,tenantId);
  assert.equal(body.orders[0].clientOrderId,"OMS-pending"); assert.equal(body.recoveryAuthorized,false);
  assert.deepEqual([fs.readFileSync(f.ledgerFile),fs.readFileSync(f.omsFile)],before);
});

test("authenticated storage errors are bounded and redacted", async t => {
  const f=fixture(t); routeEnvironment(t,f); f.oms.exec("DROP TABLE oms_orders");
  const response=await GET(request("",makeSession())), text=await response.text();
  assert.equal(response.status,503); assert.doesNotMatch(text,/SELECT|no such table|sqlite|\/tmp\/|\/Users\//);
});
