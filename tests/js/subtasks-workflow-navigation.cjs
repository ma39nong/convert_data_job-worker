const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/convest/subtasks/static/app.js','utf8');
const load=source.slice(source.indexOf('async function load('),source.indexOf('function setMode('));
const requests=[];
const c={state:{loadSequence:0,episode:{id:'existing'}},canLeave:()=>true,pause:()=>{},api:()=>new Promise(resolve=>requests.push(resolve))};
vm.createContext(c);vm.runInContext(load,c);
(async()=>{
  const older=c.load('with-return',true);c.load('without-return',true);
  requests[0]({episode:{id:'outdated'}});
  await older;
  assert.equal(c.state.episode.id,'existing');
  assert.equal(c.state.loadSequence,2);
  console.log('outdated episode responses cannot change the newly selected workflow');
})().catch(e=>{console.error(e);process.exitCode=1;});
