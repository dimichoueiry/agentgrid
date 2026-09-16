// Run with: node tests/test_areas_ui.js
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('agentgrid/static/app.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const elements = {};
const ctx = vm.createContext({
  $: id => elements[id] ||= {textContent:'', hidden:false, innerHTML:''},
  localStorage:{getItem:()=>null}, esc:s=>String(s).replace(/</g,'&lt;'),
  sessions:[{sessionId:'a',status:'working'},{sessionId:'b',status:'blocked'},{sessionId:'c',status:'idle'}],
});
const start = script.indexOf('let areaData =');
vm.runInContext(script.slice(start, script.indexOf('// columns are data,', start)), ctx);
vm.runInContext(`areaData = {areas:[{id:'marketing',name:'Marketing',prompt:'Draft a campaign'}],members:{a:'marketing',b:'marketing'}}; areasLoaded=true; activeArea='marketing';`,ctx);
assert.equal(vm.runInContext('areaSessions("marketing").length',ctx),2);
assert.equal(vm.runInContext('areaSessions("unassigned")[0].sessionId',ctx),'c');
assert.equal(vm.runInContext('areaSessions("").length',ctx),3);
vm.runInContext('renderAreas()',ctx);
assert.equal(elements.areaCount.textContent,'2 agents');
assert.ok(elements.areaGrid.innerHTML.includes('1 working'));
assert.ok(elements.areaGrid.innerHTML.includes('1 need you'));
// Polling unchanged data must preserve the actual DOM and keyboard focus.
elements.areaGrid.innerHTML = 'browser-normalized-markup';
vm.runInContext('renderAreas()',ctx);
assert.equal(elements.areaGrid.innerHTML,'browser-normalized-markup');
vm.runInContext(`areaData.members.c='marketing'; renderAreas()`,ctx);
assert.equal(elements.areaCount.textContent,'3 agents');
assert.ok(elements.areaGrid.innerHTML.includes('3 agents'));
console.log('Work area filtering, counts, and stable polling checks passed');
