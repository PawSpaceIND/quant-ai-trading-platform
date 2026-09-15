import {test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import {parseExternalAccount,readExternalAccount} from "../lib/external-account";
const path=new URL("./fixtures/external-account-snapshot.json",import.meta.url).pathname;
const fixture=JSON.parse(fs.readFileSync(path,"utf8"));
test("Python selected-account capture independently validates in Node",()=>{
 const r=parseExternalAccount(JSON.stringify(fixture),"default",fixture.accountRef);
 assert.equal(r.status,"consistent");assert.equal(r.positions[0].quantity,"2");
});
test("wrong account, altered funds and changing-status forgery fail closed",()=>{
 assert.throws(()=>parseExternalAccount(JSON.stringify(fixture),"default","a".repeat(64)));
 for(const change of [(r:typeof fixture)=>{r.funds.cash_balance="999";},(r:typeof fixture)=>{r.status="changing";},(r:typeof fixture)=>{r.positions[0].private_note="secret";}]){
  const r=structuredClone(fixture);change(r);assert.throws(()=>parseExternalAccount(JSON.stringify(r),"default",r.accountRef));
 }
});
test("configured reader labels old capture historical and rejects wrong selected ref",()=>{
 const oldFile=process.env.PRAMANA_EXTERNAL_ACCOUNT_SNAPSHOT,oldRef=process.env.PRAMANA_EXTERNAL_ACCOUNT_REF;
 try{process.env.PRAMANA_EXTERNAL_ACCOUNT_SNAPSHOT=path;process.env.PRAMANA_EXTERNAL_ACCOUNT_REF=fixture.accountRef;
  assert.equal(readExternalAccount(Date.parse(fixture.finishedAt)+120001).status,"stale");
  process.env.PRAMANA_EXTERNAL_ACCOUNT_REF="b".repeat(64);assert.equal(readExternalAccount().status,"invalid");
 }finally{
  if(oldFile===undefined)delete process.env.PRAMANA_EXTERNAL_ACCOUNT_SNAPSHOT;else process.env.PRAMANA_EXTERNAL_ACCOUNT_SNAPSHOT=oldFile;
  if(oldRef===undefined)delete process.env.PRAMANA_EXTERNAL_ACCOUNT_REF;else process.env.PRAMANA_EXTERNAL_ACCOUNT_REF=oldRef;
 }
});
