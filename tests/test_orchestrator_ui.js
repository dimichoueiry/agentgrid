// Run with: node tests/test_orchestrator_ui.js
//
// The orchestrator strip and panel, checked the way the rest of the UI is: the
// page's own script is parsed, then the orchestrator section is run in a
// sandbox with stubbed elements. What matters here is that state reaches the
// screen -- a waiting orchestrator reads as needing you, an approval offers
// the editable task, and a repaint with unchanged data leaves the DOM alone.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('agentgrid/static/app.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);

const elements = {};
const element = () => ({textContent: '', hidden: false, innerHTML: '', value: '', placeholder: '',
                        dataset: {}, setAttribute() {}, querySelectorAll: () => [],
                        scrollTop: 0, scrollHeight: 0});
const ctx = vm.createContext({
  $: id => elements[id] ||= element(),
  esc: s => String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
  document: {querySelectorAll: () => [], querySelector: () => null},
  localStorage: {getItem: () => null, setItem() {}},
  sessions: [], view: 'board', activeArea: 'engineering', project: '',
  areaData: {areas: [{id: 'engineering', name: 'Engineering'}, {id: 'design', name: 'Design'}], members: {}},
  // the real column table, so a child's state is named exactly as the board names it
  COLUMNS: [{key: 'blocked', name: 'Needs you', match: s => s.status === 'blocked' || s.status === 'failed'},
            {key: 'working', name: 'Working', match: s => s.status === 'working'},
            {key: 'done', name: 'Replied', match: s => s.status === 'done' || s.status === 'stopped'},
            {key: 'complete', name: 'Done', match: s => s.status === 'complete'},
            {key: 'idle', name: 'Idle', match: s => s.status === 'idle'}],
  toast() {}, api: async () => ({}), post: async () => ({}),
  EventSource: function () { this.close = () => {}; },
  console,
});
const start = script.indexOf('let orchestrators = [], orchCaps');
const end = script.indexOf('function bindOrchestrators()', start);
assert.ok(start > 0 && end > start, 'the orchestrator section was not found');
vm.runInContext(script.slice(start, end), ctx);

// --- scope: an area sees its own orchestrators plus the global one ----------
const ORCHS = [
  {id: '1', name: 'Shipper', scope: 'engineering', model: 'openai/gpt-5', mode: 'ask',
   status: 'running', spawns: 2, steps: 7, costUsd: 0.1234, children: [], plan: [], pending: null},
  {id: '2', name: 'Chief', scope: '', model: 'anthropic/claude', mode: 'auto',
   status: 'waiting', spawns: 0, steps: 1, costUsd: 0, children: [], plan: [],
   pending: {id: 'p1', tool: 'start_agent', reason: 'starts a new agent',
             args: {project: '/repo', task: 'Fix the parser', name: 'parser-fix', mode: 'background'}}},
  {id: '3', name: 'Designer', scope: 'design', model: 'x/y', mode: 'ask', status: 'idle',
   spawns: 0, steps: 0, costUsd: 0, children: [], plan: [], pending: null},
];
vm.runInContext(`orchestrators = ${JSON.stringify(ORCHS)}; orchConnected = true;`, ctx);
assert.deepEqual(vm.runInContext('visibleOrchestrators().map(o => o.name)', ctx), ['Shipper', 'Chief']);
vm.runInContext(`activeArea = ''`, ctx);
assert.equal(vm.runInContext('visibleOrchestrators().length', ctx), 3, 'All agents shows every one');
vm.runInContext(`activeArea = 'unassigned'`, ctx);
assert.deepEqual(vm.runInContext('visibleOrchestrators().map(o => o.name)', ctx), ['Chief']);
vm.runInContext(`activeArea = 'engineering'`, ctx);

// --- the strip -------------------------------------------------------------
vm.runInContext('renderOrchestrators()', ctx);
const strip = elements.orchStrip.innerHTML;
assert.ok(strip.includes('data-orch="1"') && strip.includes('data-orch="2"'), strip);
assert.ok(!strip.includes('data-orch="3"'), 'another area’s orchestrator is not shown');
assert.ok(strip.includes('data-add-orch'), 'creating one is always one click away');
// A global orchestrator says so while you are inside one area.
assert.ok(strip.includes('all areas'), strip);
// Working shows its counters; a pending approval outranks the status label.
assert.ok(strip.includes('2 agents') && strip.includes('$0.12'), strip);
const waiting = strip.match(/<button class="ocard needs"[^]*?<\/button>/)[0];
assert.ok(waiting.includes('Approve?'), waiting);
assert.ok(waiting.includes('Chief'), waiting);
// Repainting unchanged data must not touch the DOM: it would kill hover/focus.
elements.orchStrip.innerHTML = 'browser-normalized';
vm.runInContext('renderOrchestrators()', ctx);
assert.equal(elements.orchStrip.innerHTML, 'browser-normalized');
vm.runInContext(`orchestrators[0].status = 'waiting'; renderOrchestrators()`, ctx);
assert.ok(elements.orchStrip.innerHTML.includes('Needs you'), 'a status change repaints');
vm.runInContext(`orchestrators[0].status = 'running'`, ctx);
// The strip belongs to the board, not to notes or tickets.
vm.runInContext(`view = 'tickets'; renderOrchestrators()`, ctx);
assert.equal(elements.orchStrip.hidden, true);
vm.runInContext(`view = 'board'; renderOrchestrators()`, ctx);
assert.equal(elements.orchStrip.hidden, false);

// --- the panel -------------------------------------------------------------
elements.orchPanel = {...element(), open: true, showModal() { this.open = true; },
                      close() { this.open = false; }, addEventListener() {}};
elements.orchTabs = {...element(), querySelectorAll: () => []};
vm.runInContext(`openOrch = '1'; orchTab = 'chat'; orchEvents = [
  {at: 1, type: 'run_started', goal: 'Ship the parser'},
  {at: 2, type: 'note', text: 'thinking about it'},
  {at: 3, type: 'agent_started', name: 'parser-fix', cwd: '/repo', task: 'Fix it', interactive: false},
  {at: 4, type: 'tool', name: 'read_agent_output', ok: true, summary: 'parser-fix'},
  {at: 5, type: 'message', text: 'The parser is fixed.'}
]; renderOrchPanel()`, ctx);
assert.equal(elements.orchPanelName.textContent, 'Shipper');
const sub = elements.orchPanelSub.textContent;
assert.ok(sub.includes('openai/gpt-5') && sub.includes('Engineering') && sub.includes('Working'), sub);
assert.ok(sub.includes('2 agents started') && sub.includes('7 steps') && sub.includes('$0.12'), sub);
assert.equal(elements.orchStop.hidden, false, 'a running orchestrator can be stopped');
assert.equal(elements.orchSend.textContent, 'Send');
// Chat carries what was said and what changed, not the tool traffic.
const chat = elements.orchBody.innerHTML;
assert.ok(chat.includes('Ship the parser') && chat.includes('The parser is fixed.'), chat);
assert.ok(chat.includes('parser-fix'), 'starting an agent is part of the conversation');
assert.ok(!chat.includes('read_agent_output'), 'tool traffic stays in Activity');
// Activity carries everything, newest first.
vm.runInContext(`orchTab = 'activity'; renderOrchPanel()`, ctx);
const activity = elements.orchBody.innerHTML;
assert.ok(activity.includes('read_agent_output'), activity);
assert.ok(activity.indexOf('message') < activity.indexOf('run_started'), 'newest first');
// Agents tab reads live status off the board, and says so when a child ended.
vm.runInContext(`sessions = [{sessionId: 's1', jobId: 'job1', title: 'parser-fix', status: 'working'}];
  orchestrators[0].children = [
    {name: 'parser-fix', jobId: 'job1', engine: 'claude', cwd: '/repo', interactive: false},
    {name: 'gone', jobId: 'job9', engine: 'codex', cwd: '/repo', interactive: true}];
  orchTab = 'agents'; renderOrchPanel()`, ctx);
const agents = elements.orchBody.innerHTML;
assert.ok(agents.includes('parser-fix') && agents.includes('Working'), agents);
assert.ok(agents.includes('gone') && agents.includes('ended'), agents);
assert.ok(agents.includes('interactive'), agents);
// Plan tab: empty until it writes one.
vm.runInContext(`orchTab = 'plan'; renderOrchPanel()`, ctx);
assert.ok(elements.orchBody.innerHTML.includes('checklist appears here'));
vm.runInContext(`orchestrators[0].plan = [{text: 'read the tests', done: true},
                                          {text: 'fix the parser', done: false}]; renderOrchPanel()`, ctx);
const plan = elements.orchBody.innerHTML;
assert.ok(plan.includes('class="done"') && plan.includes('read the tests'), plan);

// --- the approval banner ---------------------------------------------------
vm.runInContext(`openOrch = '2'; orchTab = 'chat'; orchEvents = []; renderOrchPanel()`, ctx);
assert.equal(elements.orchApproval.hidden, false);
const banner = elements.orchApproval.innerHTML;
assert.ok(banner.includes('starts a new agent'), banner);
assert.ok(banner.includes('Fix the parser') && banner.includes('id="oaTask"'),
          'the task is editable before approving');
assert.ok(banner.includes('id="oaName"') && banner.includes('parser-fix'), banner);
assert.ok(banner.includes('/repo') && banner.includes('background'), 'the fixed facts are shown');
assert.ok(banner.includes('data-oapp') && banner.includes('data-oapp-auto') &&
          banner.includes('data-odecline'), banner);
assert.ok(banner.includes('id="oaReason"'), 'declining can carry a reason');
// A waiting orchestrator with no approval is waiting on a reply, not a decision.
vm.runInContext(`orchestrators[1].pending = null; renderOrchPanel()`, ctx);
assert.equal(elements.orchApproval.hidden, true);
assert.equal(elements.orchSend.textContent, 'Send');
// Not started yet: the composer asks for a goal.
vm.runInContext(`orchestrators[1].status = 'idle'; orchestrators[1].goal = ''; renderOrchPanel()`, ctx);
assert.equal(elements.orchSend.textContent, 'Start');
assert.ok(elements.orchInput.placeholder.includes('work on'), elements.orchInput.placeholder);
assert.equal(elements.orchStop.hidden, true);

// --- escaping --------------------------------------------------------------
vm.runInContext(`orchestrators[1].name = '<img src=x>'; renderOrchestrators()`, ctx);
assert.ok(!elements.orchStrip.innerHTML.includes('<img src=x>'), 'names are escaped');
vm.runInContext(`orchEvents = [{at: 1, type: 'message', text: '<script>bad()</script>'}];
  openOrch = '2'; orchTab = 'chat'; renderOrchPanel()`, ctx);
assert.ok(!elements.orchBody.innerHTML.includes('<script>bad()'), 'messages are escaped');

console.log('Orchestrator UI: scope, strip, panel tabs, approvals and escaping checks passed');
