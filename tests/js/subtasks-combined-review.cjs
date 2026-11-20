const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/convest/subtasks/static/app.js','utf8');
const reviewed={saved_at:1,revision:2,inherited:false,segments:[{label:'harvest',reviewed:true}]};
function harness(){
  const calls=[],messages=[];
  const feedback={};const c={$:()=>feedback,state:{workflow:'without_return',catalog:{episodes:[]}},dirty:()=>false,updateButtons(){},message:(...v)=>messages.push(v),taskGroup:e=>e.workflow,refreshCatalog:async()=>{},load:async()=>{},requireEpisode(){},renderLocations(){},calls,messages};
  c.api=async(path,body)=>{calls.push({path,body});if(path.includes('episode?id=n'))return {stages:{coarse:reviewed,fine:{...reviewed,saved_at:null}},pair:{state:'matched',episodes:[{id:'s'}],mapping:{}}};if(path.includes('episode?id=s'))return {stages:{coarse:{saved_at:null}}};if(path.endsWith('/map'))return {result:{stages:{coarse:reviewed}}};return {result:{path:'/annotations/episode_000000.json'}};};
  vm.createContext(c);vm.runInContext(source.split('// Begin combined review actions.')[1].split('// End combined review actions.')[0],c);return c;
}
(async()=>{
  const c=harness();assert(c.reviewedLayer(reviewed));assert(!c.reviewedLayer({...reviewed,inherited:true}));assert(!c.reviewedLayer({...reviewed,segments:[{label:'harvest',reviewed:false}]}));
  c.state.catalog.episodes=[{id:'n',variant:'normal',workflow:'without_return'}, {id:'other',variant:'normal',workflow:'with_return'}, {id:'excluded',variant:'normal',workflow:'without_return',excluded:true}];
  await c.syncReviewedSpeedup();assert.deepEqual(c.calls.filter(x=>x.body).map(x=>[x.path,x.body.id||x.body.normal_id]),[['/api/workspace/export-direct','n'],['/api/workspace/map','n'],['/api/workspace/export-direct','s']]);assert.equal(c.calls.find(x=>x.path.endsWith('/map')).body.replace,false);
  const mapped=harness();mapped.state.catalog=c.state.catalog;mapped.api=async(path,body)=>{mapped.calls.push({path,body});return {stages:{coarse:reviewed,fine:{saved_at:null}},pair:{mapping:{coarse:{stale:true}}}};};await mapped.syncReviewedSpeedup();assert.equal(mapped.calls.length,1);
  const failed=harness();failed.state.catalog=c.state.catalog;const api=failed.api;failed.api=async(path,body)=>{if(path.endsWith('export-direct'))throw Error('server unavailable');return api(path,body);};await failed.syncReviewedSpeedup();assert(!failed.calls.some(x=>x.path.endsWith('/map')));assert(failed.messages.at(-1)[0].includes('失败'));
  const single=harness();single.state.episode={id:'n'};single.state.locations={host:'28',records:{}};single.state.stages={coarse:reviewed};single.saveStage=async(stage)=>single.calls.push({save:stage});await single.approveAndExport('coarse');assert.equal(single.calls[0].save,'coarse');assert.equal(single.calls[1].path,'/api/workspace/export-direct');assert(single.state.locations.records.coarse);assert(single.messages.at(-1)[0].includes('无需再次点击导出'));assert(source.includes("$('saveCoarse').onclick=()=>run(()=>approveAndExport('coarse'))"));assert(source.includes("$('saveFine').onclick=()=>run(()=>approveAndExport('fine'))"));
  console.log('combined review: save before export; reviewed-only workflow sync; mapped/stale skip; normal export failure blocks mapping; no target overwrite passed');
})().catch(e=>{console.error(e);process.exitCode=1});
