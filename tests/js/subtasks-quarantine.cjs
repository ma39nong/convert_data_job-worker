const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/convest/subtasks/static/app.js','utf8');
const e=(id,excluded,saved=true)=>({id,excluded,directory_id:'d',remote_directory:'batch',episode_index:id,coarse_saved:saved,fine_saved:saved});
const c={state:{directory:'d',filter:'all',catalog:{episodes:[e(0,false),e(1,true),e(2,false,false)]}},$:()=>({value:''}),shortDirectory:p=>p};
vm.createContext(c);vm.runInContext(source.split('\n').find(l=>l.startsWith('function taskGroup(')),c);vm.runInContext(source.split('\n').find(l=>l.startsWith('function compareDirectories(')),c);vm.runInContext(source.split('\n').find(l=>l.startsWith('function inVariant(')),c);vm.runInContext(source.split('\n').find(l=>l.startsWith('function visibleEpisodes(')),c);
assert.deepEqual(Array.from(c.visibleEpisodes(),e=>e.id),[0,2]);
c.state.filter='coarse';assert.deepEqual(Array.from(c.visibleEpisodes(),e=>e.id),[0]);
c.state.filter='excluded';assert.deepEqual(Array.from(c.visibleEpisodes(),e=>e.id),[1]);
c.state.filter='fine';assert.deepEqual(Array.from(c.visibleEpisodes(),e=>e.id),[0]);
console.log('subtasks quarantine UI: excluded records are separate from active and saved filters passed');

c.state.catalog.episodes.push({...e(3,false),variant:'speedup'});c.state.filter='all';assert.deepEqual(Array.from(c.visibleEpisodes(),e=>e.id),[0,2]);c.state.variant='speedup';assert.deepEqual(Array.from(c.visibleEpisodes(),e=>e.id),[3]);
