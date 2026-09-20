// Run with: node tests/test_system_prompt_ui.js
// The system-prompt sheet (AG-15): view/edit the prompt a running agent was
// launched with, insert a saved prompt, duplicate the agent with an edited
// prompt, and the /system composer builtin. As with the other _ui tests, the
// page's own functions are pulled out of app.html and run in a sandbox against
// stub DOM elements and a fake server — no browser, no backend.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('agentgrid/static/app.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);   // the whole script must at least parse

// --- pull the pieces under test out of the page -----------------------------
function extract(name) {
  const start = script.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `missing ${name}`);
  const next = /\n(?:async )?function /g; next.lastIndex = start + 1;
  const end = next.exec(script)?.index ?? script.length;
  return (script.slice(start - 6, start) === 'async ' ? 'async ' : '') + script.slice(start, end);
}
function extractLine(re, label) {
  const m = re.exec(script);
  assert.ok(m, `missing ${label}`);
  return m[0];
}

// --- stub DOM ---------------------------------------------------------------
const elements = {};
function element(id) {
  return {
    id, hidden: true, innerHTML: '', value: '', textContent: '', title: '',
    dataset: {}, style: {}, focused: false, selected: false,
    classList: { contains() { return false; }, toggle() {}, add() {}, remove() {} },
    focus() { this.focused = true; }, select() { this.selected = true; },
    setRangeText() {}, setSelectionRange() {},
    getBoundingClientRect() { return { left: 0, top: 0 }; },
  };
}
const $ = id => elements[id] ||= element(id);

// --- fake server & spies ----------------------------------------------------
const calls = [];
let transcriptBlocks = [];        // what /api/transcript returns
let transcriptShouldThrow = false;
async function api(path) {
  calls.push(['GET', path]);
  if (path.startsWith('/api/transcript')) {
    if (transcriptShouldThrow) throw new Error('boom');
    return { blocks: transcriptBlocks };
  }
  return {};
}
async function post(path, body) { calls.push(['POST', path, body]); return {}; }
const toasts = [];
function toast(m) { toasts.push(m); }
const esc = t => String(t ?? '');

// spies for functions duplicate/openSysPrompt/insertSlash call out to
const spies = { openNewAgent: 0, syncEngineModels: 0, fillModels: [], fillProjectSelect: [],
                openModelPicker: 0, hideSlash: 0, openSysPromptArgs: [] };
async function openNewAgent() { spies.openNewAgent++; }
function syncEngineModels() { spies.syncEngineModels++; }
function fillModels(id, engine, sel) { spies.fillModels.push([id, engine, sel]); }
function fillProjectSelect(sel) { spies.fillProjectSelect.push(sel); }
function openModelPicker() { spies.openModelPicker++; }
function hideSlash() { spies.hideSlash++; }
function openPrompts() {}
function clearPromptForm() {}

// --- assemble the sandbox ---------------------------------------------------
const ctx = {
  $, api, post, toast, esc, openNewAgent, syncEngineModels, fillModels,
  fillProjectSelect, openModelPicker, hideSlash, openPrompts, clearPromptForm,
  // page module state the extracted code shares
  sessions: [], promptLibrary: [], projectsCache: [], openId: null,
  sysFirstTurn: {}, sysSessionId: null, slashHits: [], slashFrom: 0,
  chatRefs: [], chatRefSeq: 0,
  navigator: { clipboard: { writeText: async () => { throw new Error('blocked'); } } },
};
ctx.console = console;
vm.createContext(ctx);
// constants first, then the functions (each defines itself on the context)
vm.runInContext(extractLine(/const SYS_INSTR_RE = [^\n]*/, 'SYS_INSTR_RE'), ctx);
[ 'extractSystemPrompt', 'stripSystemPrompt', 'rememberFirstTurn', 'sessionFirstTurn',
  'fillSysPickSelect', 'openSysPrompt', 'closeSysPrompt', 'sysInsertSaved',
  'sysCopyPrompt', 'duplicateSession', 'fillNaSysLib', 'insertSlash',
].forEach(fn => vm.runInContext(extract(fn), ctx));

function reset() {
  calls.length = 0; toasts.length = 0;
  transcriptBlocks = []; transcriptShouldThrow = false;
  spies.openNewAgent = 0; spies.syncEngineModels = 0; spies.fillModels = [];
  spies.fillProjectSelect = []; spies.openModelPicker = 0; spies.hideSlash = 0;
  spies.openSysPromptArgs = [];
  ctx.sessions = []; ctx.promptLibrary = []; ctx.projectsCache = [];
  ctx.openId = null; ctx.sysFirstTurn = {}; ctx.sysSessionId = null;
  ctx.slashHits = []; ctx.slashFrom = 0;
  Object.keys(elements).forEach(k => delete elements[k]);
}

const WRAP = sys => `<system instructions>\n${sys}\n</system instructions>\n\ndo the thing`;
let passed = 0;
function test(name, fn) {
  reset();
  return Promise.resolve(fn()).then(
    () => { passed++; console.log('ok  -', name); },
    e => { console.error('FAIL-', name, '\n   ', e.message); process.exitCode = 1; });
}

(async () => {
  // 1. extraction: pull the launched prompt back out of the first turn
  await test('extractSystemPrompt reads the <system instructions> block', () => {
    assert.strictEqual(ctx.extractSystemPrompt(WRAP('You are a linter.')), 'You are a linter.');
    assert.strictEqual(ctx.extractSystemPrompt('multi\nline\nsys' &&
      WRAP('multi\nline\nsys')), 'multi\nline\nsys');
  });
  await test('extractSystemPrompt is empty when there was no system prompt', () => {
    assert.strictEqual(ctx.extractSystemPrompt('just a plain task'), '');
    assert.strictEqual(ctx.extractSystemPrompt(''), '');
    assert.strictEqual(ctx.extractSystemPrompt(null), '');
  });
  await test('a mid-message mention of the tag is not mistaken for the prompt', () => {
    // must be anchored at the very start, or a quoted tag would forge one
    assert.strictEqual(ctx.extractSystemPrompt('hi\n<system instructions>\nx\n</system instructions>'), '');
  });
  await test('stripSystemPrompt leaves the task a duplicate should start from', () => {
    assert.strictEqual(ctx.stripSystemPrompt(WRAP('You are X.')), 'do the thing');
    assert.strictEqual(ctx.stripSystemPrompt('plain task'), 'plain task');
  });

  // 2. remembering / fetching the opening turn
  await test('rememberFirstTurn caches system + task from the first user block', () => {
    ctx.rememberFirstTurn('s1', [
      { kind: 'assistant', text: 'hi' },
      { kind: 'user', text: WRAP('be terse') },
    ]);
    assert.strictEqual(ctx.sysFirstTurn.s1.system, 'be terse');
    assert.strictEqual(ctx.sysFirstTurn.s1.task, 'do the thing');
  });
  await test('sessionFirstTurn fetches once when not cached', async () => {
    transcriptBlocks = [{ kind: 'user', text: WRAP('fetched prompt') }];
    const info = await ctx.sessionFirstTurn('s2');
    assert.strictEqual(info.system, 'fetched prompt');
    assert.strictEqual(calls.filter(c => c[0] === 'GET').length, 1);
    // second call is served from cache — no new request
    await ctx.sessionFirstTurn('s2');
    assert.strictEqual(calls.filter(c => c[0] === 'GET').length, 1);
  });
  await test('sessionFirstTurn degrades gracefully when the transcript throws', async () => {
    transcriptShouldThrow = true;
    const info = await ctx.sessionFirstTurn('s3');
    assert.strictEqual(info.system, '');
    assert.strictEqual(info.task, '');
  });

  // 3. opening the sheet against a running agent
  await test('openSysPrompt shows the launched prompt and a running-state note', async () => {
    ctx.sessions = [{ sessionId: 's1', customName: 'Linter', status: 'working' }];
    ctx.sysFirstTurn = { s1: { system: 'be terse', task: 'do the thing' } };
    await ctx.openSysPrompt('s1');
    assert.strictEqual($('sysModal').hidden, false, 'modal opens');
    assert.strictEqual($('sysLive').textContent, 'be terse', 'shows the launched prompt read-only');
    assert.strictEqual($('sysText').value, 'be terse', 'seeds the editable field');
    assert.match($('sysScope').textContent, /running/, 'warns it is running');
    assert.strictEqual(ctx.sysSessionId, 's1');
  });
  await test('openSysPrompt says so when no system prompt was set', async () => {
    ctx.sessions = [{ sessionId: 's4', title: 'Bare', status: 'idle' }];
    ctx.sysFirstTurn = { s4: { system: '', task: 'x' } };
    await ctx.openSysPrompt('s4');
    assert.match($('sysLive').textContent, /No system prompt/);
    assert.strictEqual($('sysText').value, '');
  });
  await test('openSysPrompt with no open session nudges instead of opening', async () => {
    ctx.openId = null;
    await ctx.openSysPrompt(null);
    assert.strictEqual($('sysModal').hidden, true);
    assert.ok(toasts.some(t => /Open an agent/.test(t)));
  });

  // 4. inserting a saved prompt into the editor
  await test('sysInsertSaved drops the chosen library body into the editor', () => {
    ctx.promptLibrary = [{ name: 'terse', body: 'Be very terse.', description: '' }];
    $('sysPick').value = 'terse';
    ctx.sysInsertSaved();
    assert.strictEqual($('sysText').value, 'Be very terse.');
    assert.strictEqual($('sysErr').textContent, '');
  });
  await test('sysInsertSaved with nothing picked errors instead of blanking', () => {
    $('sysPick').value = '';
    $('sysText').value = 'keep me';
    ctx.sysInsertSaved();
    assert.strictEqual($('sysText').value, 'keep me');
    assert.match($('sysErr').textContent, /Pick a saved prompt/);
  });

  // 5. copy falls back to select when the clipboard API is blocked
  await test('sysCopyPrompt falls back to selecting the text', async () => {
    $('sysText').value = 'copy this';
    await ctx.sysCopyPrompt();
    assert.strictEqual($('sysText').selected, true);
    assert.ok(toasts.some(t => /copy/i.test(t)));
  });

  // 6. duplicate: pre-fill a fresh New agent sheet, never touch the original
  await test('duplicateSession pre-fills the New agent sheet from the session', async () => {
    ctx.sessions = [{ sessionId: 's1', customName: 'Linter', engine: 'codex',
                      model: 'gpt-x', cwd: '/repo', status: 'working' }];
    ctx.projectsCache = [{ path: '/repo' }];
    ctx.sysFirstTurn = { s1: { system: 'orig sys', task: 'orig task' } };
    await ctx.duplicateSession('s1');
    assert.strictEqual(spies.openNewAgent, 1, 'opens a fresh sheet');
    assert.strictEqual($('naEngine').value, 'codex');
    assert.deepStrictEqual(spies.fillModels.at(-1), ['naModel', 'codex', 'gpt-x']);
    assert.deepStrictEqual(spies.fillProjectSelect.at(-1), '/repo');
    assert.strictEqual($('naSys').value, 'orig sys');
    assert.strictEqual($('naTask').value, 'orig task');
    assert.strictEqual($('naName').value, 'Linter copy');
    // the original is never signalled: no POST to spawn/cancel/anything
    assert.strictEqual(calls.filter(c => c[0] === 'POST').length, 0);
  });
  await test('duplicateSession honours an edited system prompt override', async () => {
    ctx.sessions = [{ sessionId: 's1', title: 'A', engine: 'claude', model: '', cwd: '/x', status: 'idle' }];
    ctx.sysFirstTurn = { s1: { system: 'orig', task: 't' } };
    await ctx.duplicateSession('s1', 'edited system prompt');
    assert.strictEqual($('naSys').value, 'edited system prompt');
  });
  await test('duplicateSession on a missing session just toasts', async () => {
    ctx.sessions = [];
    await ctx.duplicateSession('nope');
    assert.strictEqual(spies.openNewAgent, 0);
    assert.ok(toasts.some(t => /No session/.test(t)));
  });

  // 7. the /system composer builtin routes to the sheet, not the model picker
  await test('insertSlash dispatches /system to the prompt sheet', () => {
    // openSysPrompt is defined on the context; shadow it with a spy for this test
    const realOpen = ctx.openSysPrompt;
    ctx.openSysPrompt = id => { spies.openSysPromptArgs.push(id); };
    ctx.openId = 's9';
    ctx.slashHits = [{ name: 'system', builtin: true }];
    ctx.slashFrom = 0;
    $('cInput').selectionStart = 7;
    ctx.insertSlash(0);
    assert.deepStrictEqual(spies.openSysPromptArgs, ['s9'], 'opens the sheet for the open agent');
    assert.strictEqual(spies.openModelPicker, 0, 'not the model picker');
    ctx.openSysPrompt = realOpen;
  });
  await test('insertSlash still sends /model to the model picker', () => {
    ctx.slashHits = [{ name: 'model', builtin: true }];
    ctx.slashFrom = 0;
    $('cInput').selectionStart = 6;
    ctx.insertSlash(0);
    assert.strictEqual(spies.openModelPicker, 1);
  });

  if (!process.exitCode) console.log(`\nAll ${passed} system-prompt UI checks passed.`);
})();
