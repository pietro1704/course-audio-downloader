import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Connection,endpoint,pairing,stage} from './model.js';
test('pairing stays in URL fragment and endpoint always uses loopback',()=>{
 assert.deepEqual(pairing('#token=private&port=9000'),{token:'private',port:9000});
 assert.equal(endpoint(8765),'http://127.0.0.1:8765');
 for (const p of [0,65536,NaN,8765.1,'https://elsewhere.test']) assert.throws(()=>endpoint(p));
});
test('real request contract delegates operation and identity to CLI adapter',async()=>{
 const calls=[];const fetcher=async function(...args){assert.equal(this,globalThis);calls.push(args);return {ok:true,json:async()=>({id:'owned'})};};
 const c=new Connection(8765,'private-token',fetcher);
 const request={job_id:'one',operation:'check',options:{},request_id:'one-request'};
 assert.deepEqual(await c.start(request),{id:'owned'});await c.cancel('owned');await c.state();
 assert.equal(calls[0][0],'http://127.0.0.1:8765/api/runs');
 assert.deepEqual(JSON.parse(calls[0][1].body),request);
 assert.equal(calls[0][1].headers.Authorization,'Bearer private-token');
 assert.equal(calls[0][1].credentials,'omit');assert.equal(calls[0][1].redirect,'error');
 assert.equal(calls[0][1].cache,'no-store');
 assert.deepEqual(JSON.parse(calls[1][1].body),{id:'owned'});assert.equal(calls[2][1].method,'GET');
});
test('authentication, conflict and connection failures remain failures',async()=>{
 for (const status of [401,403,409,500]) {
  const c=new Connection(8765,'private',async()=>({ok:false,status}));
  await assert.rejects(c.state());
 }
 const c=new Connection(8765,'private',async()=>{throw new TypeError('offline');});
 await assert.rejects(c.state(),/offline/);
 assert.equal(stage('wait-for-upload'),'Aguardando confirmação do iCloud');
 assert.equal(stage('not-a-known-stage'),'CLI em execução');
});
