// Run with: node tests/test_chat_ui.js
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('agentgrid/static/app.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const elements = {};
const saved = new Map([['agentgrid.chatModel', 'legacy-claude']]);
const ctx = vm.createContext({
  $: id => elements[id] ||= {value:'', hidden:true, focus(){}, showPicker(){this.opened=true;},
    querySelector(){return {}; }},
  esc: s=>s, localStorage:{getItem:k=>saved.has(k)?saved.get(k):null, setItem:(k,v)=>saved.set(k,v)},
  chatModel:'', chatEngine:'claude', chatDisabled:false, openId:'session', chatTools:new Map(),
  chatGrow(){}, hideSlash(){}, toast(){}, chatAtt:[], chatClose(){}, chatClearAtt(){}, chatClearRefs(){},
  loadTodos(){}, chatSetStatus(){}, chatConnect(){}, chatSyncState(){},
  document:{querySelectorAll:()=>[]}, api:async()=>({}),
  post(){throw Error('Model command was sent to backend');}
});
function extract(name) {
  const start = script.indexOf(`function ${name}(`);
  const next = /\n(?:async )?function /g; next.lastIndex = start + 1;
  const end = next.exec(script)?.index ?? script.length;
  return script.slice(start, end);
}
vm.runInContext(script.slice(script.indexOf('let ENGINE_MODELS ='),script.indexOf('function syncEngineModels()')),ctx);
vm.runInContext('async '+extract('chatSend'),ctx);
vm.runInContext(extract('chatOpen'),ctx);
(async()=>{
  vm.runInContext('chatOpen("session", "codex")',ctx);
  assert.equal(ctx.chatDisabled,false);
  assert.equal(ctx.chatModel,''); // Never carry a Claude alias into Codex.
  elements.cInput.value='/model codex-exact';
  await vm.runInContext('chatSend()',ctx);
  assert.equal(saved.get('agentgrid.chatModel.codex'),'codex-exact');
  vm.runInContext('chatOpen("session", "claude")',ctx);
  assert.equal(ctx.chatModel,'legacy-claude');
  elements.cInput.value='/model default';
  await vm.runInContext('chatSend()',ctx);
  vm.runInContext('chatOpen("session", "claude")',ctx);
  assert.equal(ctx.chatModel,''); // An explicit default must override the legacy preference.
  vm.runInContext('chatOpen("session", "codex")',ctx);
  assert.equal(ctx.chatModel,'codex-exact');
  elements.cInput.value='/model';
  await vm.runInContext('chatSend()',ctx);
  assert.equal(elements.cModel.opened,true);
  console.log('Chat UI syntax, engine selection, and /model checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
