const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/convest/subtasks/static/app.js','utf8');
const fields={};const field=id=>fields[id]??=( {value:1,classList:{toggle(){}},replaceChildren(){},append(){}} );
const stages={coarse:{saved_at:1,revision:2,segments:[{start_frame:0,end_frame:10,label:'pickup',reviewed:true},{start_frame:10,end_frame:100,label:'harvest',reviewed:true}]},fine:{revision:0,coarse_binding_stale:true,segments:[{start_frame:0,end_frame:20,label:'pickup',reviewed:false},{start_frame:20,end_frame:100,label:'harvest',reviewed:false}]}};
const data={episode:{id:'003',episode_index:3,cameras:[],length:100},annotation:{source:{}},stages,locations:{},pair:{}};
const original=JSON.stringify(data),messages=[],jobs=[];
const c={state:{loadSequence:0,catalog:{episodes:[{id:'003'}]},mode:'fine',processMode:'fine',processingModes:['fine'],processWorkers:1},
  canLeave:()=>true,pause(){},api:async()=>JSON.parse(original),inVariant:()=>true,$:field,
  ReviewHistory:class{},semanticContent:s=>JSON.parse(JSON.stringify(s)),syncFine(){throw Error('load must not project fine');},
  shortDirectory:()=>'',inHandSource:()=>true,render(){},updateTime(){},renderEpisodeList(){},processingState:()=> 'pending',message:s=>messages.push(s),updateButtons(){},
  startJob:async(...args)=>jobs.push(args)};
vm.createContext(c);
vm.runInContext(source.slice(source.indexOf('async function load('),source.indexOf('function renderStatus(')),c);
for(const name of ['dirty','startProcessing'])vm.runInContext(source.split('\n').find(l=>l.startsWith((name==='dirty'?'function ':'async function ')+name+'(')),c);
(async()=>{
 await c.load('003',true);
 assert.equal(JSON.stringify(c.state.stages),JSON.stringify(stages));
 assert(!c.dirty());assert(messages.some(s=>s.includes('粗阶段已更新')));
 await c.startProcessing(['003']);assert.equal(jobs.length,1);
 // Real user edits still block generation; no silent loss of either layer.
 c.state.dirty.coarse=true;await assert.rejects(c.startProcessing(['003']),/请先保存/);
 c.state.dirty.coarse=false;c.state.dirty.fine=true;await assert.rejects(c.startProcessing(['003']),/请先保存/);
 assert.equal(jobs.length,1);
 console.log('stale fine load is read-only, generation unblocked, actual edits still protected');
})().catch(e=>{console.error(e);process.exitCode=1;});
