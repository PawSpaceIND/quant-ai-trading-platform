import {test} from "node:test";
import assert from "node:assert/strict";
import {parseBenchmarkAttribution} from "../lib/benchmark-attribution";
import fs from "node:fs";
const valid=JSON.parse(fs.readFileSync(new URL("./fixtures/benchmark-attribution.json",import.meta.url),"utf8"));
test("Python report independently reconciles in UI reader",()=>{assert.equal(parseBenchmarkAttribution(JSON.stringify(valid),"example-portfolio").sourceQualified,false);});
test("altered report, source and portfolio are rejected",()=>{
 assert.throws(()=>parseBenchmarkAttribution(JSON.stringify(valid),"other"));
 for(const field of ["inputPayload","activeReturn","periodEnd"]){const r=structuredClone(valid);r[field]="invalid";assert.throws(()=>parseBenchmarkAttribution(JSON.stringify(r),"example-portfolio"));}
 const r=structuredClone(valid);r.sectors[0].allocation=".99";assert.throws(()=>parseBenchmarkAttribution(JSON.stringify(r),"example-portfolio"));
});
