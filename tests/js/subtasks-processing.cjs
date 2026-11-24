const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/convest/subtasks/static/app.js','utf8');
const c={state:{variant:'normal',directory:null,processMode:'coarse'}};vm.createContext(c);
for(const name of ['processingState','directoryStats','taskGroup','inVariant','canProcess'])vm.runInContext(source.split('\n').find(l=>l.startsWith('function '+name+'(')),c);
const e={variant:'normal',state:'pending',coarse_ready:false,fine_ready:false};
assert(c.canProcess(e));assert(!c.canProcess({...e,excluded:true}));assert(!c.canProcess({...e,variant:'speedup'}));
const rough={...e,state:'ready',coarse_ready:true};assert(!c.canProcess(rough));
c.state.processMode='fine';assert(c.canProcess(rough));assert(!c.canProcess({...rough,fine_ready:true}));
assert(!c.canProcess({...rough,fine_saved:true}));
c.state.stages={fine:{segments:[{start_frame:0,end_frame:60,label:'unknown'}]},coarse:{segments:[]}};
vm.runInContext(source.match(/function syncFine\(\)\{[\s\S]*?\n\}/)[0],c);
const before=JSON.stringify(c.state.stages);c.syncFine();assert.equal(JSON.stringify(c.state.stages),before);
console.log('processing modes: coarse-to-fine continuation, version scope, preservation, and unknown fine nonmutation passed');

const group={id:'d'},rows=[{...e,directory_id:'d',state:'pending',coarse_state:'ready',fine_state:'pending',coarse_ready:true}];
c.state.processMode='coarse';assert.equal(c.directoryStats(group,rows).pending,0);assert(!c.canProcess(rows[0]));
c.state.processMode='fine';assert.equal(c.directoryStats(group,rows).pending,1);assert(c.canProcess(rows[0]));

(async()=>{
  const requests=[];
  const api={state:{workflow:'without_return',processMode:'coarse',processWorkers:2,processingModes:['coarse','fine']},dirty:()=>false,startJob:async(path,body,kind)=>requests.push({path,body,kind})};
  vm.createContext(api);
  for(const name of ['startRemaining','startProcessing'])vm.runInContext(source.split('\n').find(l=>l.startsWith('async function '+name+'(')),api);
  await api.startRemaining();await api.startProcessing(['new-video']);
  assert.equal(requests[0].body.workflow,'without_return');assert.equal(requests[0].body.workers,2);assert.equal(requests[0].body.all_remaining,true);
  assert.equal(requests[1].body.workers,2);assert.deepEqual(Array.from(requests[1].body.ids),['new-video']);
  api.state.processWorkers=8;api.state.processProvider='qwen';await api.startRemaining();assert.equal(requests[2].body.workers,8);assert.equal(requests[2].body.provider,'qwen');await api.startProcessing(['qwen-video']);assert.equal(requests[3].body.provider,'qwen');
  console.log('both processing entry points forward selected parallel worker count passed');
})().catch(e=>{console.error(e);process.exitCode=1;});

c.state.workflow='without_return';c.state.processMode='coarse';
assert(!c.canProcess(e));
assert(c.canProcess({...e,workflow:'without_return'}));
assert(!c.canProcess({...e,workflow:'without_return',superseded:true}));
assert.equal(c.directoryStats({id:'d'},[{...e,directory_id:'d',superseded:true}]).total,0);
console.log('workflow tabs isolate task queues and hide superseded media duplicates');

for(const name of ['providerName','providerReady'])vm.runInContext(source.split('\n').find(l=>l.startsWith('function '+name+'(')),c);
c.state.processProvider='qwen';c.state.qwenReady=true;c.state.workflow='without_return';c.state.processMode='coarse';assert(c.providerReady());c.state.processMode='fine';assert(c.providerReady());c.state.processMode='coarse';c.state.workflow='with_return';assert(!c.providerReady());

const fineUI={state:{mode:'fine',selected:{fine:0},labels:{unknown:{text:'待确认'},pickup:{text:'右手拿起番茄'},harvest:{text:'单颗采摘投放'},adjust:{text:'调整番茄串'},return:{text:'放回番茄串'},grasp_fruit:{text:'旧抓握'}},coarseLabels:{},stages:{coarse:{source:{dataset:'/full-block-more/subtask-roi/A/pkg'}},fine:{segments:[{label:'harvest'},{label:'harvest'}]}}},copy:v=>JSON.parse(JSON.stringify(v))};
vm.createContext(fineUI);vm.runInContext(source.slice(source.indexOf('const vocabulary='),source.indexOf('function stageColor(')),fineUI);
assert.deepEqual(Array.from(fineUI.fineLabelKeys()),['unknown','pickup','harvest','adjust','return']);
assert.equal(fineUI.englishLabel('harvest','fine',0),fineUI.englishLabel('harvest','fine',1));
assert(fineUI.englishLabel('harvest','fine',0).includes('one tomato'));
fineUI.state.stages.fine.segments.push({label:'grasp_fruit'});assert(fineUI.fineLabelKeys().includes('grasp_fruit'));
console.log('single-fruit labels: four actions, unchanged per-fruit prompt, legacy labels only when present');
