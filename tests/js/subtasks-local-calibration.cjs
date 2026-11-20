const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/convest/subtasks/static/app.js','utf8');
const sent=[],rows=[{id:'fresh',local_calibration_eligible:true,workflow:'without_return',variant:'normal'},{id:'old',local_calibration_eligible:false,workflow:'without_return',variant:'normal'},{id:'speed',local_calibration_eligible:true,workflow:'without_return',variant:'speedup'}];
const c={state:{catalog:{episodes:rows},episode:{id:'fresh'},workflow:'without_return',variant:'normal'},dirty:()=>false,startJob:async(...args)=>sent.push(args)};vm.createContext(c);
for(const name of ['inVariant','taskGroup','localCalibrationRows'])vm.runInContext(source.split('\n').find(l=>l.startsWith('function '+name+'(')),c);
vm.runInContext(source.split('\n').find(l=>l.startsWith('async function startLocalCalibration(')),c);
(async()=>{
 assert.deepEqual(Array.from(c.localCalibrationRows(),e=>e.id),['fresh']);
 await c.startLocalCalibration();await c.startLocalCalibration(true);
 assert.equal(sent[0][0],'/api/workspace/refine-local-batch');assert.equal(sent[0][2],'local');assert.equal(sent[0][1].ids[0],'fresh');assert.equal(sent[1][1].all_remaining,true);
 c.dirty=()=>true;await assert.rejects(c.startLocalCalibration(),/未保存/);c.dirty=()=>false;
 c.state.episode.id='old';await assert.rejects(c.startLocalCalibration(),/已保存、已校准/);
 c.state.job={id:'running'};await assert.rejects(c.startLocalCalibration(true),/当前任务/);
 assert.equal(sent.length,2);console.log('local calibration controls: current/batch scope and dirty/protected/active-job guards passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
