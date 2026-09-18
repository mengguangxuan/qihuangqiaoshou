// Unit tests for asynchronous UI races; no browser or external API is used.
const assert = require('node:assert/strict');
const {test} = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../dist/assets/voice.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; };

async function setup({configured = true, responder} = {}) {
  const elements = new Map(), listeners = {}, calls = [], players = [];
  function element(id) {
    if (!elements.has(id)) elements.set(id, {textContent: '', value: '', checked: false, disabled: false, dataset: {}, style: {setProperty() {}}, classList: {add() {}, remove() {}, toggle() {}}, replaceChildren() {}, append() {}});
    return elements.get(id);
  }
  element('voiceStyle').value = 'brief';
  element('voiceSilence').value = '0';
  const current = {point: 'GV14', mode: 'guide', view: 'back', audience: 'mannequin', label: '大椎'};
  let proposed = null;
  const media = {tracksStopped: 0, async getUserMedia() { return {getTracks: () => [{stop: () => media.tracksStopped++}]}; }};
  class Recorder {
    static isTypeSupported() { return true; }
    constructor() { this.state = 'inactive'; }
    start() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; queueMicrotask(() => { this.ondataavailable?.({data: new Blob(['x'.repeat(300)])}); this.onstop?.(); }); }
  }
  class AudioContext {
    async resume() {}
    async close() {}
    createAnalyser() { return {fftSize: 2048, getFloatTimeDomainData(values) { values.fill(.1); }}; }
    createMediaStreamSource() { return {connect() {}}; }
  }
  class Audio {
    constructor(src) { this.src = src; this.paused = true; players.push(this); }
    async play() { this.paused = false; this.onplaying?.(); }
    pause() { this.paused = true; }
  }
  const scope = {
    document: {getElementById: element, querySelectorAll: () => [], createElement: () => ({append() {}}), createTextNode: text => text},
    performance, AbortController, AbortSignal, Blob, URL, Audio, AudioContext, MediaRecorder: Recorder,
    navigator: {mediaDevices: media}, setInterval, clearInterval, requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    console, TypeError, Error,
    async fetch(url, options = {}) {
      calls.push({url, options});
      if (url.endsWith('/status')) return {ok: true, json: async () => ({configured, models: {chat: 'mock', transcribe: 'mock', speech: 'mock'}})};
      if (responder) { const reply = responder(url, options); if (reply) return await reply; }
      if (url.endsWith('/chat')) return {ok: true, json: async () => ({reply: '测试回答', selected_point: null, sources: [], elapsed_ms: 5})};
      if (url.endsWith('/transcribe')) return {ok: true, json: async () => ({text: '大椎在哪里？'})};
      return {ok: true, blob: async () => new Blob(['audio'])};
    },
    addEventListener(name, callback) { listeners[name] = callback; },
    QihuangPage: {
      context: () => ({...current}),
      select(id) { current.point = id; current.label = id; listeners['qihuang:context'](); },
      proposal(value) { proposed = value; }
    }
  };
  scope.window = scope;
  vm.runInNewContext(source, scope);
  await settle();
  return {scope, element, calls, players, media, current, listeners, get proposal() { return proposed; }, cleanup: () => scope.QihuangVoice.cancel('')};
}

test('missing key is explicit and makes no paid calls', async () => {
  const h = await setup({configured: false});
  await h.scope.QihuangVoice.ask('大椎在哪里？');
  assert.equal(h.element('micButton').disabled, true);
  assert.equal(h.element('micStatusBadge').textContent, '待配置');
  assert.equal(h.calls.length, 1);
  h.cleanup();
});

test('three turns retain context and history; mute avoids TTS', async () => {
  const h = await setup(); h.element('voiceMute').checked = true;
  for (const question of ['大椎在哪里', '为什么叫这个名字', '再简单一点']) await h.scope.QihuangVoice.ask(question);
  const requests = h.calls.filter(call => call.url.endsWith('/chat')).map(call => JSON.parse(call.options.body));
  assert.deepEqual(requests.map(request => request.history.length), [0, 2, 4]);
  assert.equal(requests[2].context.point, 'GV14');
  assert.equal(h.calls.filter(call => call.url.endsWith('/speech')).length, 0);
  assert.equal(h.element('voiceHistoryCount').textContent, '3 轮');
  assert.equal(h.element('voiceConnection').textContent, '服务已连接 · 最近请求成功');
  h.cleanup();
});

test('late answer cannot overwrite a cancelled turn or start audio', async () => {
  const pending = deferred();
  const h = await setup({responder: url => url.endsWith('/chat') ? pending.promise : null});
  const asking = h.scope.QihuangVoice.ask('大椎'); await settle();
  h.scope.QihuangVoice.cancel();
  pending.resolve({ok: true, json: async () => ({reply: '迟到的回答', sources: []})});
  await asking;
  assert.notEqual(h.element('voiceReply').textContent, '迟到的回答');
  assert.equal(h.players.length, 0);
  assert.equal(h.element('voiceHistoryCount').textContent, '');
  h.cleanup();
});

test('switching point invalidates pending speech and updates context', async () => {
  const pending = deferred();
  const h = await setup({responder: url => url.endsWith('/speech') ? pending.promise : null});
  const asking = h.scope.QihuangVoice.ask('大椎'); await settle();
  h.current.point = 'SI11-L'; h.current.label = '左天宗'; h.listeners['qihuang:context']();
  pending.resolve({ok: true, blob: async () => new Blob(['audio'])});
  await asking;
  assert.equal(h.players.length, 0);
  assert.equal(h.element('voiceContext').textContent, '当前：左天宗');
  assert.equal(h.element('voiceReplay').disabled, true);
  h.cleanup();
});

test('model page selection does not cancel its own answer', async () => {
  const h = await setup({responder: url => url.endsWith('/chat') ? Promise.resolve({ok: true, json: async () => ({reply: '左天宗介绍', selected_point: 'SI11-L', sources: [], elapsed_ms: 10})}) : null});
  await h.scope.QihuangVoice.ask('看看左天宗');
  assert.equal(h.current.point, 'SI11-L');
  assert.equal(h.element('voiceReply').textContent, '左天宗介绍');
  assert.equal(h.players.length, 1);
  assert.equal(h.players[0].paused, false);
  h.cleanup(); assert.equal(h.players[0].paused, true);
});

test('permission cancellation releases a late stream without recording', async () => {
  const pending = deferred(); const h = await setup();
  h.media.getUserMedia = () => pending.promise;
  const opening = h.scope.QihuangVoice.toggleMic(); await settle();
  assert.equal(h.element('micButton').textContent, '取消申请');
  h.scope.QihuangVoice.cancel();
  pending.resolve({getTracks: () => [{stop: () => h.media.tracksStopped++}]});
  await opening;
  assert.equal(h.media.tracksStopped, 1);
  assert.equal(h.calls.filter(call => call.url.endsWith('/transcribe')).length, 0);
  h.cleanup();
});

test('recording transcribes then asks, and always releases microphone', async () => {
  const h = await setup(); h.element('voiceMute').checked = true;
  await h.scope.QihuangVoice.toggleMic();
  assert.equal(h.element('micStatusBadge').textContent, '正在聆听');
  await h.scope.QihuangVoice.toggleMic(); await settle(); await settle();
  assert.equal(h.element('voiceTextInput').value, '大椎在哪里？');
  assert.equal(h.element('voiceReply').textContent, '测试回答');
  assert.equal(h.media.tracksStopped, 1);
  assert.equal(h.calls.filter(call => call.url.endsWith('/transcribe')).length, 1);
  h.cleanup();
});

test('new conversation clears history before the next request', async () => {
  const h = await setup(); h.element('voiceMute').checked = true;
  await h.scope.QihuangVoice.ask('大椎');
  h.element('voiceReset').onclick();
  await h.scope.QihuangVoice.ask('天宗');
  const last = h.calls.filter(call => call.url.endsWith('/chat')).at(-1);
  assert.equal(JSON.parse(last.options.body).history.length, 0);
  h.cleanup();
});
