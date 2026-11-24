const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/convest/subtasks/static/app.js','utf8');
const sent=[],loaded=[];
const c={state:{episode:{id:'current'},mode:'fine',dirty:{coarse:false,fine:true},stages:{fine:{proposal_key:'sha'}}},requireEpisode(){},hasPendingFields:()=>false,api:async(...args)=>sent.push(args),refreshCatalog:async()=>{},load:async(...args)=>loaded.push(args),message(){}};
vm.createContext(c);vm.runInContext(source.split('\n').find(l=>l.startsWith('async function discardQwenFine(')),c);
(async()=>{
 await c.discardQwenFine();assert.equal(sent[0][0],'/api/workspace/discard-qwen-fine');assert.equal(sent[0][1].proposal_key,'sha');assert.equal(loaded[0][0],'current');assert.equal(loaded[0][1],true);
 c.state.dirty.coarse=true;await assert.rejects(c.discardQwenFine(),/先保存粗阶段/);
 c.state.dirty.coarse=false;c.state.mode='coarse';c.hasPendingFields=()=>true;await assert.rejects(c.discardQwenFine(),/先保存粗阶段/);
 c.state.mode='fine';c.state.job={id:'job'};await assert.rejects(c.discardQwenFine(),/当前任务/);
 assert.equal(sent.length,1);
 console.log('discard only selected Qwen fine key, preserve pending coarse edits, no model request');
})().catch(e=>{console.error(e);process.exitCode=1;});
