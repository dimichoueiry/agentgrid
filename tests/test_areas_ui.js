// Run with: node tests/test_areas_ui.js
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('agentgrid/static/app.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const elements = {};
const ctx = vm.createContext({
  $: id => elements[id] ||= {textContent:'', hidden:false, innerHTML:''},
  localStorage:{getItem:()=>null}, esc:s=>String(s).replace(/</g,'&lt;'),
  sessions:[{sessionId:'a',status:'working'},{sessionId:'b',status:'blocked'},{sessionId:'c',status:'idle'},
    {sessionId:'d',status:'done',unread:true},{sessionId:'e',status:'done',unread:false},{sessionId:'f',status:'stopped',unread:true}],
});
const start = script.indexOf('let areaData =');
vm.runInContext(script.slice(start, script.indexOf('// columns are data,', start)), ctx);
vm.runInContext(`areaData = {areas:[{id:'marketing',name:'Marketing',prompt:'Draft a campaign'},{id:'design',name:'Design',prompt:''}],members:{a:'marketing',b:'marketing',d:'marketing',e:'marketing',f:'design'}}; areasLoaded=true; activeArea='marketing';`,ctx);
assert.equal(vm.runInContext('areaSessions("marketing").length',ctx),4);
assert.equal(vm.runInContext('areaSessions("unassigned")[0].sessionId',ctx),'c');
assert.equal(vm.runInContext('areaSessions("").length',ctx),6);
vm.runInContext('renderAreas()',ctx);
assert.equal(elements.areaCount.textContent,'4 agents');
const card = id => elements.areaGrid.innerHTML.match(new RegExp(`<button class="area-card" data-area="${id}"[^]*?</button>`))[0];
// every kind of activity shows, most urgent first, and a read reply is not counted
const mk = card('marketing');
assert.ok(mk.includes('data-state="needs"'), 'the most urgent state colours the ring');
const order = ['1 needs you', '1 replied', '1 working'].map(x => mk.indexOf(x));
assert.ok(order.every(i => i > 0) && order[0] < order[1] && order[1] < order[2], mk);
// a stopped session with unread output is a reply too, like the board's blue stub
assert.ok(card('design').includes('data-state="replied"'));
assert.ok(card('design').includes('1 replied'));
// quiet areas stay quiet: no ring, no badges
assert.ok(!card('unassigned').includes('data-state'));
assert.ok(!card('unassigned').includes('apill'));
assert.equal(vm.runInContext('areaActivity([{status:"working"},{status:"working"}]).state',ctx),'working');
assert.ok(vm.runInContext('areaActivity([{status:"working"},{status:"working"}]).html',ctx).includes('2 working'));
assert.ok(vm.runInContext('areaActivity([{status:"done",unread:true},{status:"done",unread:true}]).html',ctx).includes('2 agents replied'));
// Polling unchanged data must preserve the actual DOM and keyboard focus.
elements.areaGrid.innerHTML = 'browser-normalized-markup';
vm.runInContext('renderAreas()',ctx);
assert.equal(elements.areaGrid.innerHTML,'browser-normalized-markup');
vm.runInContext(`areaData.members.c='marketing'; renderAreas()`,ctx);
assert.equal(elements.areaCount.textContent,'5 agents');
assert.ok(elements.areaGrid.innerHTML.includes('5 agents'));
console.log('Work area filtering, counts, activity badges, and stable polling checks passed');
