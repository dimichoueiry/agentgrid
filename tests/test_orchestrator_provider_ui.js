// Run with: node tests/test_orchestrator_provider_ui.js
//
// A persona's brain -- OpenRouter or the local Claude Code CLI -- as the page
// shows and saves it: the editor's model field follows the provider, the save
// carries the choice, the OpenRouter warning appears only for an OpenRouter
// persona, and a Claude Code brain is labelled as such wherever the model is.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('agentgrid/static/app.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);

const elements = {};
const element = () => ({textContent: '', hidden: false, innerHTML: '', value: '', placeholder: '',
                        dataset: {}, open: false, setAttribute() {}, removeAttribute() {},
                        getAttribute: () => null, querySelectorAll: () => [], focus() {},
                        showModal() { this.open = true; }, close() { this.open = false; }});
const posted = [];
let openRouterCalls = 0;
const ctx = vm.createContext({
  $: id => elements[id] ||= element(),
  esc: s => String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
  document: {querySelectorAll: () => [], querySelector: () => null},
  localStorage: {getItem: () => null, setItem() {}},
  sessions: [], view: 'board', activeArea: '', project: '', areaData: {areas: [], members: {}},
  COLUMNS: [], toast() {}, console,
  api: async () => ({}),
  post: async (url, body) => { posted.push({url, body}); return {ok: true, persona: {...body, name: body.name}}; },
  ENGINE_MODELS: {claude: [['', 'Default'], ['opus', 'Opus (alias)'], ['claude-opus-5-5', 'Claude Opus 5.5']],
                  codex: [['', 'Default']], openrouter: [['openai/gpt-5', 'GPT-5']]},
  loadOpenRouterModels: async () => { openRouterCalls++; },
  workflowModelOptions: engine => (ctx.ENGINE_MODELS[engine] || []).filter(([v]) => v)
    .map(([v]) => `<option value="${v}">`).join(''),
  EventSource: function () { this.close = () => {}; },
});
const start = script.indexOf('let orchestrators = [], orchCaps');
const end = script.indexOf('function bindOrchestrators()', start);
assert.ok(start > 0 && end > start, 'the orchestrator section was not found');
vm.runInContext(script.slice(start, end), ctx);
// stand-ins for loaders defined elsewhere on the page
vm.runInContext('loadPersonas = async () => {}; loadOrchestrators = async () => {};', ctx);

const PERSONAS = [
  {id: 'a', name: 'Router', provider: 'openrouter', model: 'openai/gpt-5', agentDefaults: {}, allowedModels: [], postings: [], memory: []},
  {id: 'b', name: 'Local', provider: 'claude', model: 'claude-opus-5-5', agentDefaults: {}, allowedModels: [], postings: [], memory: []},
];
vm.runInContext(`personaData = ${JSON.stringify({personas: PERSONAS, catalog: {}})}`, ctx);

(async () => {
  // --- labels ------------------------------------------------------------
  assert.equal(ctx.brainLabel('claude', 'claude-opus-5-5'), 'Claude Code · claude-opus-5-5');
  assert.equal(ctx.brainLabel('openrouter', 'openai/gpt-5'), 'openai/gpt-5');
  assert.equal(ctx.brainLabel(undefined, 'x/y'), 'x/y', 'an older record without a provider reads as before');
  assert.match(ctx.personaSummary(PERSONAS[1]), /thinks with Claude Code · claude-opus-5-5/);

  // --- the OpenRouter warning follows the persona ------------------------
  vm.runInContext('orchKeyConnected = false', ctx);
  ctx.fillPersonaSelect('a');
  assert.equal(elements.orchConnHint.hidden, false, 'an OpenRouter persona without a key is warned');
  ctx.fillPersonaSelect('b');
  assert.equal(elements.orchConnHint.hidden, true, 'a Claude Code persona needs no key');
  vm.runInContext('orchKeyConnected = true', ctx);
  ctx.fillPersonaSelect('a');
  assert.equal(elements.orchConnHint.hidden, true, 'a connected key needs no warning');

  // --- the editor --------------------------------------------------------
  await ctx.openPersonaEditor('b');
  assert.equal(elements.pProvider.value, 'claude');
  assert.equal(elements.pModel.value, 'claude-opus-5-5');
  assert.match(elements.pModel.placeholder, /claude-opus-5-5/);
  assert.match(elements.pModels.innerHTML, /claude-opus-5-5/);
  assert.equal(openRouterCalls, 0, 'a Claude Code persona does not fetch the OpenRouter catalog');

  await ctx.openPersonaEditor('a');
  assert.equal(elements.pProvider.value, 'openrouter');
  assert.match(elements.pModels.innerHTML, /openai\/gpt-5/);
  assert.equal(openRouterCalls, 1);

  // A new persona starts on the provider the user already uses first.
  await ctx.openPersonaEditor('');
  assert.equal(elements.pProvider.value, 'openrouter');

  // --- saving carries the choice -----------------------------------------
  await ctx.openPersonaEditor('b');
  elements.pName.value = 'Local';
  elements.pAllowed.value = '';
  await ctx.savePersona();
  const saved = posted.find(p => p.url === '/api/personas/save').body;
  assert.equal(saved.provider, 'claude');
  assert.equal(saved.model, 'claude-opus-5-5');

  console.log('orchestrator provider UI: all checks passed');
})().catch(error => { console.error(error); process.exit(1); });
