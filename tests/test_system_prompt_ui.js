// Run with: node tests/test_system_prompt_ui.js
// The system-prompt sheet (AG-15, rewired onto AG-14's endpoints in AG-16).
// The SOURCE OF TRUTH is the server's per-session store: the sheet reads it
// with GET /api/system-prompt, writes it with POST, and duplicates an agent
// with POST /api/agents/duplicate. The turn-1 preamble scraped from the
// transcript survives only as the read-only "Launched with" context line.
// As with the other _ui tests, the page's own functions are pulled out of
// app.html and run in a sandbox against stub DOM elements and a fake server
// that keeps the same trim/cap/clear rules as agentgrid/discovery.py.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('agentgrid/static/app.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);   // the whole script must at least parse

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
    disabled: false, dataset: {}, style: {}, attrs: {}, focused: false, selected: false,
    classList: { contains() { return false; }, toggle() {}, add() {}, remove() {} },
    focus() { this.focused = true; }, select() { this.selected = true; },
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
    setRangeText() {}, setSelectionRange() {},
  };
}
const $ = id => elements[id] ||= element(id);

// --- a fake server with discovery.py's rules --------------------------------
const LONG = 'You are a careful reviewer. '.repeat(4000);   // ~112k chars
const server = { prompts: {}, agents: [], transcript: [], failGet: false, failPost: false };
const calls = [];
async function api(path) {
  calls.push(['GET', path]);
  if (path.startsWith('/api/system-prompt')) {
    if (server.failGet) throw new Error('unreachable');
    const id = decodeURIComponent(new RegExp('session=([^&]*)').exec(path)[1]);
    return { sessionId: id, systemPrompt: server.prompts[id] || '' };
  }
  if (path.startsWith('/api/transcript')) return { blocks: server.transcript };
  return {};
}
async function post(path, body) {
  calls.push(['POST', path, body]);
  if (path === '/api/system-prompt') {
    if (server.failPost) return { error: 'Could not save.' };
    if (!body.sessionId) return { error: 'Which session? A session id is required.' };
    const kept = String(body.systemPrompt || '').trim();   // as discovery.save_system_prompt — no cap
    if (kept) server.prompts[body.sessionId] = kept; else delete server.prompts[body.sessionId];
    return { sessionId: body.sessionId, systemPrompt: kept };
  }
  if (path === '/api/agents/duplicate') {
    if (body.sessionId) {
      const s = ctx.sessions.find(x => x.sessionId === body.sessionId);
      if (!s) return { error: 'Unknown session.' };
      const name = (s.customName || s.title) + ' (copy)';
      server.agents.push({ name, engine: s.engine, model: s.model || '',
                           systemPrompt: server.prompts[body.sessionId] || '' });
      return { agents: server.agents.slice(), name };
    }
    return { error: 'No saved agent by that name to duplicate.' };
  }
  return {};
}
const toasts = [];
function toast(m) { toasts.push(m); }
const esc = t => String(t ?? '');

const spies = { openNewAgent: 0, fillLibrarySelect: [], fillProjectSelect: [],
                fillModels: [], syncEngineModels: 0, openModelPicker: 0,
                hideSlash: 0, openSysPromptArgs: [] };
async function openNewAgent() {           // the real one clears the sheet first
  spies.openNewAgent++;
  $('naSys').value = ''; $('naTask').value = ''; $('naName').value = '';
}
function fillLibrarySelect(n) { spies.fillLibrarySelect.push(n); $('naLib').value = n || ''; }
function fillProjectSelect(p) { spies.fillProjectSelect.push(p); $('naProject').value = p; }
function fillModels(id, e, sel) { spies.fillModels.push([id, e, sel]); }
function syncEngineModels() { spies.syncEngineModels++; }
function openModelPicker() { spies.openModelPicker++; }
function hideSlash() { spies.hideSlash++; }
function openPrompts() {} function clearPromptForm() {}

const ctx = {
  $, api, post, toast, esc, openNewAgent, fillLibrarySelect, fillProjectSelect,
  fillModels, syncEngineModels, openModelPicker, hideSlash, openPrompts, clearPromptForm,
  sessions: [], promptLibrary: [], projectsCache: [], agentLibrary: [], openId: null,
  sysFirstTurn: {}, sysSessionId: null,
  slashHits: [], slashFrom: 0, chatRefs: [], chatRefSeq: 0,
  navigator: { clipboard: { writeText: async () => { throw new Error('blocked'); } } },
  console,
};
vm.createContext(ctx);
vm.runInContext(extractLine(/const SYS_INSTR_RE = [^\n]*/, 'SYS_INSTR_RE'), ctx);
[ 'extractSystemPrompt', 'stripSystemPrompt', 'rememberFirstTurn', 'sessionFirstTurn',
  'loadStoredSystemPrompt', 'sysUpdateCount', 'fillSysPickSelect', 'openSysPrompt',
  'closeSysPrompt', 'sysInsertSaved', 'sysCopyPrompt', 'sysApplyPrompt',
  'duplicateSession', 'applyLibraryDefinition', 'fillNaSysLib', 'insertSlash',
].forEach(fn => vm.runInContext(extract(fn), ctx));

function reset() {
  calls.length = 0; toasts.length = 0;
  server.prompts = {}; server.agents = []; server.transcript = [];
  server.failGet = false; server.failPost = false;
  Object.assign(spies, { openNewAgent: 0, fillLibrarySelect: [], fillProjectSelect: [],
    fillModels: [], syncEngineModels: 0, openModelPicker: 0, hideSlash: 0, openSysPromptArgs: [] });
  ctx.sessions = []; ctx.promptLibrary = []; ctx.projectsCache = []; ctx.agentLibrary = [];
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
  // 1. the stored prompt is the source of truth
  await test('openSysPrompt seeds the editor from the STORE, not the transcript', async () => {
    server.prompts.s1 = 'stored standing prompt';
    server.transcript = [{ kind: 'user', text: WRAP('launch-time preamble') }];
    ctx.sessions = [{ sessionId: 's1', customName: 'Linter', status: 'working' }];
    await ctx.openSysPrompt('s1');
    assert.strictEqual($('sysModal').hidden, false);
    assert.strictEqual($('sysText').value, 'stored standing prompt', 'editor holds the stored prompt');
    assert.ok(calls.some(c => c[1].startsWith('/api/system-prompt?session=s1')), 'read the store');
  });
  await test('the launch-time preamble shows only as read-only context', async () => {
    server.prompts.s1 = 'stored';
    server.transcript = [{ kind: 'user', text: WRAP('launch-time preamble') }];
    ctx.sessions = [{ sessionId: 's1', title: 'A', status: 'idle' }];
    await ctx.openSysPrompt('s1');
    assert.strictEqual($('sysLaunch').textContent, 'launch-time preamble');
    assert.strictEqual($('sysLaunchRow').hidden, false);
    assert.strictEqual($('sysText').value, 'stored', 'context never overwrites the editor');
  });
  await test('no launch preamble hides the context row entirely', async () => {
    server.transcript = [{ kind: 'user', text: 'just a plain task' }];
    ctx.sessions = [{ sessionId: 's1', title: 'A', status: 'idle' }];
    await ctx.openSysPrompt('s1');
    assert.strictEqual($('sysLaunchRow').hidden, true);
  });
  await test('AG-19: a launch preamble equal to the standing prompt is not shown twice', async () => {
    // A freshly created agent now persists the prompt it launched with as its
    // standing prompt, so the editor and the "Launched with" scrape hold the
    // same text — the redundant context row is suppressed.
    server.prompts.s1 = 'You are a security auditor.';
    server.transcript = [{ kind: 'user', text: WRAP('You are a security auditor.') }];
    ctx.sessions = [{ sessionId: 's1', title: 'A', status: 'idle' }];
    await ctx.openSysPrompt('s1');
    assert.strictEqual($('sysText').value, 'You are a security auditor.', 'editor shows the prompt once');
    assert.strictEqual($('sysLaunchRow').hidden, true, 'no duplicate context row');
  });
  await test('an empty store opens an empty editor, not an error', async () => {
    ctx.sessions = [{ sessionId: 's1', title: 'A', status: 'idle' }];
    await ctx.openSysPrompt('s1');
    assert.strictEqual($('sysText').value, '');
    assert.strictEqual($('sysErr').textContent, '');
  });
  await test('a running session is told the edit applies to future turns', async () => {
    ctx.sessions = [{ sessionId: 's1', customName: 'Linter', status: 'working' }];
    await ctx.openSysPrompt('s1');
    assert.match($('sysScope').textContent, /running/);
    assert.match($('sysScope').textContent, /from now on/, 'promises future turns, not in-place');
    assert.ok(!/can't be re-instructed/.test($('sysScope').textContent), 'the old false claim is gone');
  });
  await test('the editor sets no maxlength and counts without showing a limit', async () => {
    server.prompts.s1 = 'abc';
    ctx.sessions = [{ sessionId: 's1', title: 'A', status: 'idle' }];
    await ctx.openSysPrompt('s1');
    // No fence: the browser must never silently drop typed or pasted text.
    assert.strictEqual($('sysText').getAttribute('maxlength'), undefined);
    assert.strictEqual($('sysCount').textContent, '3 characters');
  });
  await test('a very long stored prompt opens in full', async () => {
    server.prompts.s1 = LONG;
    ctx.sessions = [{ sessionId: 's1', title: 'A', status: 'idle' }];
    await ctx.openSysPrompt('s1');
    assert.strictEqual($('sysText').value, LONG, 'nothing was cut off');
    assert.ok($('sysText').value.length > 100000);
  });
  await test('saving a very long prompt keeps every character', async () => {
    ctx.sessions = [{ sessionId: 's1', title: 'A', status: 'idle' }];
    await ctx.openSysPrompt('s1');
    $('sysText').value = LONG;
    await ctx.sysApplyPrompt();
    assert.strictEqual(server.prompts.s1, LONG.trim(), 'the server kept it whole');
    assert.strictEqual($('sysText').value, LONG.trim());
  });
  await test('an unreadable store warns instead of showing a misleading blank', async () => {
    server.failGet = true;
    ctx.sessions = [{ sessionId: 's1', title: 'A', status: 'idle' }];
    await ctx.openSysPrompt('s1');
    assert.match($('sysErr').textContent, /Could not read the stored prompt/);
  });
  await test('openSysPrompt with no open session nudges instead of opening', async () => {
    await ctx.openSysPrompt(null);
    assert.strictEqual($('sysModal').hidden, true);
    assert.ok(toasts.some(t => /Open an agent/.test(t)));
  });

  // 2. saving actually applies
  await test('sysApplyPrompt POSTs the prompt and it lands in the store', async () => {
    ctx.sessions = [{ sessionId: 's1', title: 'A', status: 'working' }];
    await ctx.openSysPrompt('s1');
    $('sysText').value = 'be terse';
    await ctx.sysApplyPrompt();
    assert.strictEqual(server.prompts.s1, 'be terse', 'the server now holds it');
    const sent = calls.find(c => c[0] === 'POST' && c[1] === '/api/system-prompt');
    // field-wise: the body is built inside the vm realm, so deepStrictEqual
    // would fail on prototypes rather than on content
    assert.strictEqual(sent[2].sessionId, 's1');
    assert.strictEqual(sent[2].systemPrompt, 'be terse');
    assert.match($('sysStatus').textContent, /every turn you send/);
  });
  await test('the field is refilled from what the server kept, not what was typed', async () => {
    ctx.sessions = [{ sessionId: 's1', title: 'A', status: 'idle' }];
    await ctx.openSysPrompt('s1');
    $('sysText').value = '   padded with space   ';
    await ctx.sysApplyPrompt();
    assert.strictEqual($('sysText').value, 'padded with space', 'shows the trimmed text the server kept');
    assert.strictEqual($('sysCount').textContent, '17 characters');
  });
  await test('saving an empty prompt clears the entry and says so', async () => {
    server.prompts.s1 = 'old prompt';
    ctx.sessions = [{ sessionId: 's1', title: 'A', status: 'idle' }];
    await ctx.openSysPrompt('s1');
    $('sysText').value = '';
    await ctx.sysApplyPrompt();
    assert.ok(!('s1' in server.prompts), 'entry removed, not stored blank');
    assert.match($('sysStatus').textContent, /Cleared/);
  });
  await test('a refused save surfaces the error and re-enables the button', async () => {
    server.failPost = true;
    ctx.sessions = [{ sessionId: 's1', title: 'A', status: 'idle' }];
    await ctx.openSysPrompt('s1');
    $('sysText').value = 'x';
    await ctx.sysApplyPrompt();
    assert.strictEqual($('sysErr').textContent, 'Could not save.');
    assert.strictEqual($('sysApply').disabled, false, 'not left stuck disabled');
  });
  await test('sysApplyPrompt with no session open refuses rather than POSTing', async () => {
    ctx.sysSessionId = null;
    await ctx.sysApplyPrompt();
    assert.ok(!calls.some(c => c[0] === 'POST'), 'nothing sent');
    assert.ok(toasts.some(t => /Open an agent/.test(t)));
  });

  // 3. duplicate goes through the real endpoint
  await test('duplicateSession captures the session via /api/agents/duplicate', async () => {
    server.prompts.s1 = 'standing prompt';
    ctx.sessions = [{ sessionId: 's1', customName: 'Linter', engine: 'codex',
                      model: 'gpt-x', cwd: '/repo', status: 'working' }];
    ctx.projectsCache = [{ path: '/repo' }];
    await ctx.duplicateSession('s1');
    const sent = calls.find(c => c[1] === '/api/agents/duplicate');
    assert.strictEqual(sent[2].sessionId, 's1');
    assert.deepStrictEqual(Object.keys(sent[2]), ['sessionId'],
      'only the session id is sent — the server owns engine/model/prompt capture');
    assert.strictEqual(spies.openNewAgent, 1, 'sheet opened to launch it');
    assert.deepStrictEqual(spies.fillLibrarySelect.at(-1), 'Linter (copy)', 'new def selected');
    // the captured definition, not the transcript, fills the sheet
    assert.strictEqual($('naSys').value, 'standing prompt');
    assert.strictEqual($('naEngine').value, 'codex');
    assert.deepStrictEqual(spies.fillModels.at(-1), ['naModel', 'codex', 'gpt-x']);
    // cwd is not part of a definition, so the source's folder is offered
    assert.deepStrictEqual(spies.fillProjectSelect.at(-1), '/repo');
    assert.ok(toasts.some(t => /Duplicated as "Linter \(copy\)"/.test(t)));
  });
  await test('duplicateSession never spawns or signals the original session', async () => {
    ctx.sessions = [{ sessionId: 's1', title: 'A', engine: 'claude', cwd: '/x', status: 'working' }];
    await ctx.duplicateSession('s1');
    assert.ok(!calls.some(c => c[1] === '/api/spawn'), 'no spawn');
    assert.ok(!calls.some(c => c[1] === '/api/chat'), 'no message to the running agent');
  });
  await test('a rejected duplicate toasts the server reason', async () => {
    ctx.sessions = [{ sessionId: 's1', title: 'A', engine: 'claude', status: 'idle' }];
    ctx.sessions = [];                       // server will not know it
    await ctx.duplicateSession('gone');
    assert.strictEqual(spies.openNewAgent, 0);
    assert.ok(toasts.some(t => /No session/.test(t)));
  });
  await test('applyLibraryDefinition fills the sheet and reports a miss', () => {
    ctx.agentLibrary = [{ name: 'Linter', engine: 'codex', model: 'm', systemPrompt: 'sp' }];
    assert.strictEqual(ctx.applyLibraryDefinition('Linter'), true);
    assert.strictEqual($('naSys').value, 'sp');
    assert.strictEqual($('naName').value, 'Linter');
    assert.strictEqual(ctx.applyLibraryDefinition('nope'), false);
    assert.strictEqual($('naLibDel').hidden, true);
  });

  // 4. the library picker and /system entry point still work
  await test('sysInsertSaved drops the chosen library body into the editor', () => {
    ctx.promptLibrary = [{ name: 'terse', body: 'Be very terse.', description: '' }];
    $('sysPick').value = 'terse';
    ctx.sysInsertSaved();
    assert.strictEqual($('sysText').value, 'Be very terse.');
  });
  await test('sysInsertSaved with nothing picked errors instead of blanking', () => {
    $('sysPick').value = ''; $('sysText').value = 'keep me';
    ctx.sysInsertSaved();
    assert.strictEqual($('sysText').value, 'keep me');
    assert.match($('sysErr').textContent, /Pick a saved prompt/);
  });
  await test('sysCopyPrompt falls back to selecting the text', async () => {
    $('sysText').value = 'copy this';
    await ctx.sysCopyPrompt();
    assert.strictEqual($('sysText').selected, true);
  });
  await test('insertSlash dispatches /system to the sheet, /model to the picker', () => {
    const real = ctx.openSysPrompt;
    ctx.openSysPrompt = id => { spies.openSysPromptArgs.push(id); };
    ctx.openId = 's9';
    ctx.slashHits = [{ name: 'system', builtin: true }];
    $('cInput').selectionStart = 7;
    ctx.insertSlash(0);
    assert.deepStrictEqual(spies.openSysPromptArgs, ['s9']);
    assert.strictEqual(spies.openModelPicker, 0);
    ctx.slashHits = [{ name: 'model', builtin: true }];
    ctx.insertSlash(0);
    assert.strictEqual(spies.openModelPicker, 1);
    ctx.openSysPrompt = real;
  });

  // 5. the transcript helpers that still back the context line
  await test('extractSystemPrompt reads the <system instructions> block', () => {
    assert.strictEqual(ctx.extractSystemPrompt(WRAP('You are a linter.')), 'You are a linter.');
    assert.strictEqual(ctx.extractSystemPrompt('just a plain task'), '');
    assert.strictEqual(ctx.extractSystemPrompt(null), '');
  });
  await test('a mid-message mention of the tag is not mistaken for the prompt', () => {
    assert.strictEqual(ctx.extractSystemPrompt('hi\n<system instructions>\nx\n</system instructions>'), '');
  });
  await test('sessionFirstTurn caches so the context line costs one fetch', async () => {
    server.transcript = [{ kind: 'user', text: WRAP('p') }];
    assert.strictEqual((await ctx.sessionFirstTurn('s2')).system, 'p');
    await ctx.sessionFirstTurn('s2');
    assert.strictEqual(calls.filter(c => c[1].startsWith('/api/transcript')).length, 1);
  });
  await test('an unreadable transcript degrades to no context line', async () => {
    const boom = ctx.api;
    ctx.api = async () => { throw new Error('boom'); };
    const info = await ctx.sessionFirstTurn('s3');
    assert.strictEqual(info.system, '');
    ctx.api = boom;
  });

  if (!process.exitCode) console.log(`\nAll ${passed} system-prompt UI checks passed.`);
})();
