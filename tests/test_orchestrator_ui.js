// Run with: node tests/test_orchestrator_ui.js
//
// The orchestrator menu and panel, checked the way the rest of the UI is: the
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
  ageStr: sec => `${Math.round(sec / 60)}m`,
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

// --- the breadcrumb menu ---------------------------------------------------
vm.runInContext('renderOrchestrators()', ctx);
const strip = elements.orchMenu.innerHTML;
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
// The badge counts what is waiting on you, not how many exist.
assert.equal(elements.orchCount.textContent, '1', 'one of the two is waiting');
assert.ok(elements.orchCount.title.includes('waiting on you'), elements.orchCount.title);
// Repainting unchanged data must not touch the DOM: it would kill hover/focus.
elements.orchMenu.innerHTML = 'browser-normalized';
vm.runInContext('renderOrchestrators()', ctx);
assert.equal(elements.orchMenu.innerHTML, 'browser-normalized');
vm.runInContext(`orchestrators[0].status = 'waiting'; renderOrchestrators()`, ctx);
assert.ok(elements.orchMenu.innerHTML.includes('Needs you'), 'a status change repaints');
assert.equal(elements.orchCount.textContent, '2');
vm.runInContext(`orchestrators[0].status = 'running'`, ctx);
// The button belongs to the board, not to notes or tickets.
elements.orchMenu.classList = {remove() {}};
vm.runInContext(`view = 'tickets'; renderOrchestrators()`, ctx);
assert.equal(elements.orchBtn.hidden, true);
vm.runInContext(`view = 'board'; renderOrchestrators()`, ctx);
assert.equal(elements.orchBtn.hidden, false);
// With none at all, the menu still explains itself and offers one.
vm.runInContext(`const kept = orchestrators; orchestrators = []; renderOrchestrators();
                 orchestrators = kept;`, ctx);
assert.ok(elements.orchMenu.innerHTML.includes('starts the agents to do it'), elements.orchMenu.innerHTML);
assert.ok(elements.orchMenu.innerHTML.includes('data-add-orch'));
assert.equal(elements.orchCount.textContent, '', 'nothing waiting, no badge');
vm.runInContext('renderOrchestrators()', ctx);

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
assert.ok(!elements.orchMenu.innerHTML.includes('<img src=x>'), 'names are escaped');
vm.runInContext(`orchEvents = [{at: 1, type: 'message', text: '<script>bad()</script>'}];
  openOrch = '2'; orchTab = 'chat'; renderOrchPanel()`, ctx);
assert.ok(!elements.orchBody.innerHTML.includes('<script>bad()'), 'messages are escaped');

// --- the reader keeps their place ------------------------------------------
// A body that scrolls: 1000px of content in a 300px window.
const body = elements.orchBody;
Object.assign(body, {scrollHeight: 1000, clientHeight: 300});
elements.orchNewer = {...element(), hidden: true};
vm.runInContext(`openOrch = '1'; orchTab = 'chat'; orchEvents = [{at: 1, type: 'message', text: 'one'}];
  $('orchBody')._tab = ''; renderOrchPanel()`, ctx);
assert.equal(body.scrollTop, 1000, 'opening a chat lands on the newest line');
// Scrolled up to read, then something new arrives: stay put, say so.
body.scrollTop = 200;
vm.runInContext(`orchEvents.push({at: 2, type: 'message', text: 'two'}); renderOrchPanel()`, ctx);
assert.equal(body.scrollTop, 200, 'a repaint never yanks someone who scrolled up');
assert.equal(elements.orchNewer.hidden, false, 'the pill says there is more below');
// Nothing changed (the 2s poll): the DOM is not touched at all.
body.innerHTML = 'reader-is-selecting-text';
vm.runInContext('renderOrchPanel()', ctx);
assert.equal(body.innerHTML, 'reader-is-selecting-text', 'an unchanged repaint is invisible');
// At the bottom, it follows the conversation.
elements.orchNewer.hidden = true;
body.scrollTop = 700;                    // 1000 - 700 - 300 = 0: at the bottom
vm.runInContext(`orchEvents.push({at: 3, type: 'message', text: 'three'}); renderOrchPanel()`, ctx);
assert.equal(body.scrollTop, 1000);
assert.equal(elements.orchNewer.hidden, true);
// Activity runs newest-first: it opens at the top and is then left alone.
vm.runInContext(`orchTab = 'activity'; renderOrchPanel()`, ctx);
assert.equal(body.scrollTop, 0, 'activity opens on the newest entry, at the top');
body.scrollTop = 450;
vm.runInContext(`orchEvents.push({at: 4, type: 'tool', name: 'list_agents', ok: true}); renderOrchPanel()`, ctx);
assert.equal(body.scrollTop, 450);

// --- an agent changing is part of the story --------------------------------
vm.runInContext(`orchTab = 'chat'; orchEvents = [
  {at: 5, type: 'agent_update', name: 'parser-fix', from: 'working', to: 'done'},
  {at: 6, type: 'agent_update', name: 'docs', from: 'working', to: 'blocked'}]; renderOrchPanel()`, ctx);
assert.ok(body.innerHTML.includes('parser-fix') && body.innerHTML.includes('finished its turn'), body.innerHTML);
assert.ok(body.innerHTML.includes('needs input'), body.innerHTML);

// --- the approval banner keeps what you typed -------------------------------
const approval = elements.orchApproval;
vm.runInContext(`openOrch = '2'; orchestrators[1].status = 'waiting';
  orchestrators[1].pending = {id: 'p1', tool: 'start_agent', reason: 'starts a new agent',
    args: {project: '/repo', task: 'Fix the parser', name: 'parser-fix'}};
  $('orchApproval')._pending = ''; renderOrchPanel()`, ctx);
assert.ok(approval.innerHTML.includes('Fix the parser'));
approval.innerHTML = 'you-are-typing-here';
vm.runInContext('renderOrchPanel()', ctx);
assert.equal(approval.innerHTML, 'you-are-typing-here', 'a poll must not wipe an edit in progress');
vm.runInContext(`orchestrators[1].pending = {id: 'p2', tool: 'start_agent', reason: 'starts a new agent',
  args: {task: 'Write the docs'}}; renderOrchPanel()`, ctx);
assert.ok(approval.innerHTML.includes('Write the docs'), 'a new request does rebuild it');

// --- one line that answers "how is it going?" -------------------------------
const status = () => [elements.orchStatus.innerHTML, elements.orchStatus.className];
vm.runInContext(`orchestrators[1].pending = null; orchestrators[1].status = 'running';
  orchestrators[1].phase = 'watching'; orchestrators[1].busyAgents = 2;
  orchestrators[1].lastChangeAt = Date.now() / 1000 - 180; renderOrchPanel()`, ctx);
assert.ok(status()[0].includes('Watching 2 agents'), status()[0]);
assert.ok(status()[0].includes('it will report when one changes'), status()[0]);
assert.ok(status()[0].includes('last change 3m ago'), status()[0]);
assert.ok(status()[1].includes('live'));
vm.runInContext(`orchestrators[1].phase = 'thinking'; renderOrchPanel()`, ctx);
assert.ok(status()[0].includes('Thinking'), status()[0]);
vm.runInContext(`orchestrators[1].phase = ''; orchestrators[1].status = 'waiting'; renderOrchPanel()`, ctx);
assert.ok(status()[0].includes('Waiting for your reply') && status()[1].includes('needs'), status()[0]);
vm.runInContext(`orchestrators[1].pending = {id: 'p3', tool: 'start_agent', args: {}}; renderOrchPanel()`, ctx);
assert.ok(status()[0].includes('Waiting for your approval'), status()[0]);

// --- personas: who it is, shown wherever it matters -------------------------
vm.runInContext(`orchestrators = ${JSON.stringify(ORCHS)};
  orchestrators[0].name = 'Product Manager · Engineering';
  orchestrators[0].persona = {id: 'pm', name: 'Product Manager',
    agentDefaults: {engine: 'claude', model: 'claude-opus-5', mode: 'interactive'}};
  activeArea = 'engineering'; view = 'board'; renderOrchestrators()`, ctx);
const menu = elements.orchMenu.innerHTML;
assert.ok(menu.includes('data-open-bank') && menu.includes('Personas'), 'the bank is one click from the menu');
// A row named after its persona does not repeat it; one with its own name does.
vm.runInContext(`orchestrators[0].name = 'Shipper'; renderOrchestrators()`, ctx);
assert.ok(elements.orchMenu.innerHTML.includes('Product Manager'), elements.orchMenu.innerHTML);

// the approval banner shows what will really run, defaults filled in
elements.orchApproval = {...element()};
vm.runInContext(`openOrch = '2'; orchestrators[1].status = 'waiting'; orchestrators[1].phase = '';
  orchestrators[1].pending = {id: 'p9', tool: 'start_agent', reason: 'starts a new agent',
    args: {project: '/repo', task: 'Build lesson 4', engine: 'claude', model: 'claude-opus-5',
           mode: 'interactive', savedAgent: 'MLG-Lesson-Builder'}};
  $('orchApproval')._pending = ''; renderOrchPanel()`, ctx);
const shown = elements.orchApproval.innerHTML;
assert.ok(shown.includes('saved agent MLG-Lesson-Builder'), shown);
assert.ok(shown.includes('claude-opus-5') && shown.includes('interactive'), shown);

// the panel names its persona
vm.runInContext(`orchestrators[1].persona = {id: 'cos', name: 'Chief of Staff', agentDefaults: {}};
  orchestrators[1].pending = null; renderOrchPanel()`, ctx);
assert.ok(elements.orchPanelSub.textContent.startsWith('Chief of Staff'), elements.orchPanelSub.textContent);

// --- memory: kept where it belongs, visible, and reversible -----------------
vm.runInContext(`orchestrators[1].personaMemory = [{id: 'm1', text: 'Keep prompts short.'}];
  orchestrators[1].memory = [{id: 'm2', text: 'The SEO agent is retired.'}];
  orchTab = 'memory'; renderOrchPanel()`, ctx);
const mem = elements.orchBody.innerHTML;
assert.ok(mem.includes('How Chief of Staff works') && mem.includes('Keep prompts short.'), mem);
assert.ok(mem.includes('Facts about this team') && mem.includes('The SEO agent is retired.'), mem);
assert.ok(mem.includes('data-forget-scope="persona" data-forget-id="m1"'), mem);
assert.ok(mem.includes('data-forget-scope="posting" data-forget-id="m2"'), mem);
// In the chat, a saved memory says where it went and offers an undo...
vm.runInContext(`orchTab = 'chat'; orchEvents = [
  {at: 1, type: 'memory_saved', scope: 'persona', id: 'm1', text: 'Keep prompts short.', owner: 'Chief of Staff'},
  {at: 2, type: 'memory_saved', scope: 'posting', id: 'gone', text: 'Old fact.', owner: 'COS'}];
  renderOrchPanel()`, ctx);
const said = elements.orchBody.innerHTML;
assert.ok(said.includes('remembered for every Chief of Staff posting'), said);
assert.ok(said.includes('data-forget-id="m1"') && said.includes('Undo'), said);
// ...and one that has since been removed says so instead of offering a dead button.
assert.ok(said.includes('remembered for this team') && said.includes('forgotten'), said);
assert.ok(!said.includes('data-forget-id="gone"'), said);

// --- the persona bank -------------------------------------------------------
elements.personaList = {...element()};
vm.runInContext(`personaData = {personas: [
  {id: 'pm', name: 'Product Manager', description: 'Turns goals into shipped work.', model: 'openai/gpt-5.6',
   agentDefaults: {engine: 'claude', model: 'claude-opus-5', mode: 'interactive'},
   postings: [{id: 'a', name: 'Product Manager · MLG'}, {id: 'b', name: 'Product Manager · COS'}],
   memory: [{id: 'm', text: 'x'}]},
  {id: 'el', name: 'Engineering Lead', description: '', model: 'openai/gpt-5.6', agentDefaults: {},
   postings: [], memory: []}], catalog: {skills: [], agents: [], prompts: []}};
  renderPersonaBank()`, ctx);
const bank = elements.personaList.innerHTML;
assert.ok(bank.includes('posted to Product Manager · MLG, Product Manager · COS'), bank);
assert.ok(bank.includes('1 lesson') && bank.includes('not posted yet'), bank);
assert.ok(bank.includes('starts agents as claude · claude-opus-5 · always interactive'), bank);
assert.ok(bank.includes('data-ppost="el"') && bank.includes('data-pedit="pm"'), bank);

// a pick list keeps a named-but-missing item, checked and marked, so saving
// the persona never silently drops it
elements.pSkills = {...element()};
vm.runInContext(`renderPick('pSkills', [{name: 'worktree-task', description: 'Isolate work.'},
                                        {name: 'sales-playbook', description: 'Sell.'}],
                            ['worktree-task', 'gone-skill'], s => s.description)`, ctx);
const pick = elements.pSkills.innerHTML;
assert.ok(/value="worktree-task" checked/.test(pick), pick);
assert.ok(!/value="sales-playbook" checked/.test(pick), pick);
assert.ok(/value="gone-skill" checked/.test(pick) && pick.includes('not found'), pick);
// a persona summary is the line people read before posting one
assert.equal(vm.runInContext(`personaSummary({model: 'openai/gpt-5.6', description: '',
  agentDefaults: {engine: 'claude', model: '', mode: 'background'}})`, ctx),
  'thinks with openai/gpt-5.6 · starts agents as claude · CLI default · always background');
// "let it decide" says so, rather than looking like a rule
assert.ok(vm.runInContext(`personaSummary({model: 'm', agentDefaults: {mode: 'either'}})`, ctx)
  .endsWith('session its choice'));
// persona names are escaped wherever they appear
vm.runInContext(`personaData.personas[0].name = '<img src=x>'; renderPersonaBank()`, ctx);
assert.ok(!elements.personaList.innerHTML.includes('<img src=x>'));

console.log('Orchestrator UI: scope, menu, badge, panel tabs, approvals, scroll, status line, personas, memory and escaping checks passed');
