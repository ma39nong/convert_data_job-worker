const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/convest/subtasks/static/app.js','utf8');
const reviewed={saved_at:1,revision:2,inherited:false,segments:[{label:'harvest',reviewed:true}]};
function harness(){
 const calls=[],messages=[],elements={};
 const c={$:id=>elements[id]??=( {} ),state:{busy:false,workflow:'without_return',catalog:{episodes:[]}},dirty:()=>false,updateButtons(){},message:(...v)=>messages.push(v),taskGroup:e=>e.workflow,refreshCatalog:async()=>{},load:async()=>{throw Error('must not reload active editor');},requireEpisode(){},renderLocations(){},setTimeout(){},calls,messages,elements};
 c.api=async(path,body)=>{calls.push({path,body});if(path.endsWith('speedup-sync'))return {sync:{id:'background',state:'running',active_ids:['n','s'],checked:0,total:1,completed:0,skipped:0,failures:[]}};return {result:{path:'/annotations/episode_000000.json'}};};
 vm.createContext(c);vm.runInContext(source.split('// Begin combined review actions.')[1].split('// End combined review actions.')[0],c);return c;
}
(async()=>{
 const c=harness();assert(c.reviewedLayer(reviewed));assert(!c.reviewedLayer({...reviewed,inherited:true}));assert(!c.reviewedLayer({...reviewed,segments:[{label:'harvest',reviewed:false}]}));
 await c.syncReviewedSpeedup();assert.equal(c.calls.length,1);assert.equal(c.calls[0].path,'/api/workspace/speedup-sync');assert.equal(c.calls[0].body.workflow,'without_return');assert.equal(c.state.busy,false);assert(c.syncActive());
 c.state.episode={id:'other'};assert(!c.syncCurrentLocked());c.state.episode.id='n';assert(c.syncCurrentLocked());
 const editor={unsaved:'keep'};c.state.stages=editor;c.dirty=()=>true;c.api=async()=>({sync:{...c.state.speedupSync,state:'completed',checked:1,completed:1}});await c.pollSpeedupSync();assert.strictEqual(c.state.stages,editor);assert(!c.syncActive());assert(c.elements.syncBanner.textContent.includes('同步结束'));
 const single=harness();single.state.episode={id:'n'};single.state.locations={host:'28',records:{}};single.state.stages={coarse:reviewed};single.saveStage=async(stage)=>single.calls.push({save:stage});await single.approveAndExport('coarse');assert.equal(single.calls[0].save,'coarse');assert.equal(single.calls[1].path,'/api/workspace/export-direct');assert(single.state.locations.records.coarse);assert(single.messages.at(-1)[0].includes('无需再次点击导出'));
 console.log('background sync: one launch request, editor remains available, only active pair locked, polling never replaces edits; save-before-export retained');
})().catch(e=>{console.error(e);process.exitCode=1;});
