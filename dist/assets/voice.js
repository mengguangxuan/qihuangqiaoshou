/* Voice UI: recording -> transcription -> conversation -> synthesized audio. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const page = window.QihuangPage;
  let configured = false;
  let revision = 0;
  let controller = null;
  let recording = null;
  let audio = null;
  let audioURL = null;
  let lastAnswer = '';
  let history = [];
  let contextKey = JSON.stringify(page.context());
  let selecting = false;
  let speechStartedAt = null;

  function phase(label, message) {
    $('micStatusBadge').textContent = label;
    $('voiceQuestionState').textContent = message;
    $('micStatusBadge').classList.toggle('listening', label === '正在聆听');
  }

  function stopAudio() {
    if (audio) { audio.pause(); audio.src = ''; audio = null; }
    if (audioURL) { URL.revokeObjectURL(audioURL); audioURL = null; }
    window.speechSynthesis?.cancel();
    $('voiceAgent').classList.remove('speaking');
  }

  function releaseRecording(rec) {
    if (!rec) return;
    clearInterval(rec.tick);
    if (rec.frame) cancelAnimationFrame(rec.frame);
    rec.stream?.getTracks().forEach(track => track.stop());
    rec.context?.close().catch(() => {});
    if (recording && recording !== rec) return;
    document.querySelectorAll('#voiceMeter i').forEach(bar => bar.style.setProperty('--level', 0));
    $('micButton').classList.remove('listening');
    $('micButton').textContent = '开始说话';
  }

  function cancel(message = '已停止，可继续提问') {
    revision += 1;
    controller?.abort(); controller = null;
    if (recording) {
      const rec = recording; recording = null; rec.cancelled = true;
      if (rec.recorder?.state === 'recording') rec.recorder.stop();
      releaseRecording(rec);
    }
    stopAudio();
    page.proposal(null);
    $('sendVoiceText').disabled = false;
    $('voiceState').textContent = '讲解已停止';
    if (message) phase('待命', message);
  }

  function errorMessage(error) {
    if (error.name === 'NotAllowedError') return '麦克风权限被拒绝，请在浏览器设置中允许此网页使用麦克风。';
    if (error.name === 'NotFoundError') return '没有检测到麦克风，请连接设备后再试。';
    if (error.name === 'NotReadableError') return '麦克风无法读取，可能正被其他程序占用。';
    if (error.name === 'TimeoutError') return '请求超时，请检查网络后重试。';
    if (error instanceof TypeError) return '连接失败，请确认本机语音服务正在运行。';
    return error.message || '操作失败，请重试。';
  }

  async function request(path, options, signal) {
    const response = await fetch('/api/voice/' + path, {
      ...options, signal: AbortSignal.any([signal, AbortSignal.timeout(75000)])
    });
    if (!response.ok) {
      let data;
      try { data = await response.json(); } catch (_) {}
      throw new Error(data?.error || `服务返回错误（${response.status}）`);
    }
    $('voiceConnection').textContent = '服务已连接 · 最近请求成功';
    $('voiceConnection').dataset.ready = 'true';
    return response;
  }

  async function checkConfig() {
    $('voiceConnection').textContent = '正在检查本机配置…';
    try {
      const response = await fetch('/api/voice/status', {cache: 'no-store', signal: AbortSignal.timeout(4000)});
      if (!response.ok) throw new Error('请通过 start_qihuang_voice.ps1 启动语音服务。');
      const status = await response.json();
      configured = status.configured === true;
      $('voiceConnection').textContent = configured ? 'API Key 已配置 · 可以开始提问' : '等待填写 API Key · 填好后点击检查配置';
      $('voiceConnection').dataset.ready = String(configured);
      $('voiceModels').textContent = `问答：${status.models.chat} · 识别：${status.models.transcribe} · 声音：${status.models.speech} / ${status.voice || '默认音色'}`;
      $('micButton').disabled = !configured;
      if (!configured) phase('待配置', '开发预览已就绪。请填写本机 .env 中的 OPENAI_API_KEY。');
      else phase('待命', '可以开始说话或输入问题。录音与问题将发送至 OpenAI 处理。');
    } catch (error) {
      configured = false;
      $('micButton').disabled = true;
      $('voiceConnection').textContent = errorMessage(error);
      phase('未连接', '语音后端未连接，网页内容仍可浏览。');
    }
  }

  function showHistory() {
    const list = $('voiceHistory');
    list.replaceChildren();
    history.slice(-6).forEach(item => {
      const entry = document.createElement('p');
      const label = document.createElement('strong');
      label.textContent = item.role === 'user' ? '你：' : '讲解员：';
      entry.append(label, document.createTextNode(item.content));
      list.append(entry);
    });
    $('voiceHistoryCount').textContent = `${history.length / 2} 轮`;
  }

  async function playText(text, ticket, signal, startedAt = null) {
    if ($('voiceMute').checked || ticket !== revision) return;
    phase('准备声音', '正在合成讲解，文字已经可以阅读');
    $('voiceState').textContent = '正在生成 AI 语音';
    const response = await request('speech', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text})}, signal);
    const blob = await response.blob();
    if (ticket !== revision || $('voiceMute').checked) return;
    stopAudio();
    audioURL = URL.createObjectURL(blob);
    const player = new Audio(audioURL);
    audio = player;
    player.onplaying = () => {
      if (ticket !== revision) return;
      $('voiceAgent').classList.add('speaking');
      $('voiceState').textContent = '正在讲解 · AI 合成声音';
      phase('正在讲解', '可以停止讲解，或开始一个新问题');
      if (startedAt !== null) $('voiceTiming').textContent = `本轮从提交到开始播放：${((performance.now() - startedAt) / 1000).toFixed(1)} 秒`;
    };
    player.onended = () => {
      if (ticket !== revision) return;
      stopAudio();
      $('voiceState').textContent = '讲解完毕 · 可以继续追问';
      phase('已回答', '可以继续追问，也可以切换穴位');
    };
    player.onerror = () => {
      if (ticket !== revision) return;
      stopAudio();
      phase('播放失败', '音频无法播放，文字回答已保留，可点击重播');
    };
    try { await player.play(); }
    catch (error) {
      if (ticket !== revision) return;
      phase('点击播放', '浏览器暂停了自动播放，请点击“重播回答”');
      $('voiceState').textContent = '请点击重播回答';
    }
  }

  async function ask(rawText, fromRecording = false) {
    const text = String(rawText || '').trim();
    if (!text) { phase('请输入问题', '输入问题，或点击开始说话'); return; }
    if (text.length > 1500) { phase('问题过长', '请将问题缩短到 1500 字以内'); return; }
    const startedAt = fromRecording ? speechStartedAt : performance.now();
    cancel('');
    lastAnswer = '';
    $('voiceReplay').disabled = true;
    if (!configured) {
      $('voiceReply').textContent = '还未配置 API Key，暂时不能调用 AI。填写本机 .env 后点击“检查配置”。';
      phase('待配置', '问题已保留在输入框中，配置好后可重新发送');
      return;
    }
    const ticket = revision;
    controller = new AbortController();
    const signal = controller.signal;
    const questionContext = page.context();
    $('voiceTranscript').textContent = text;
    $('voiceTextInput').value = text;
    $('voiceReply').textContent = '正在结合当前页面和前文组织回答…';
    $('voiceSources').textContent = '';
    $('voiceTiming').textContent = '';
    $('sendVoiceText').disabled = true;
    phase('正在回答', '正在理解你的问题，可点击停止取消');
    let answered = false;
    try {
      const response = await request('chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text, context: questionContext, history, style: $('voiceStyle').value})}, signal);
      const result = await response.json();
      if (ticket !== revision) return;
      if (result.selected_point) {
        selecting = true;
        try { page.select(result.selected_point); }
        finally { selecting = false; contextKey = JSON.stringify(page.context()); }
      }
      lastAnswer = result.reply;
      answered = true;
      $('voiceReply').textContent = result.reply;
      $('agentAnswer').textContent = result.reply;
      $('voiceReplay').disabled = false;
      $('voiceSources').textContent = result.sources?.length ? '依据：' + result.sources.map(source => source.name).join('、') + ' · 项目科普资料，待权威校订' : 'AI 生成回答 · 涉及具体出处请进一步核对';
      history.push({role: 'user', content: text}, {role: 'assistant', content: result.reply});
      history = history.slice(-12);
      showHistory();
      page.proposal(result.proposal);
      $('voiceTiming').textContent = `回答生成：${(result.elapsed_ms / 1000).toFixed(1)} 秒`;
      $('sendVoiceText').disabled = false;
      phase(result.proposal ? '动作待确认' : '已回答', result.proposal ? '仅生成动作建议，尚未执行' : '可继续追问或重播回答');
      await playText(result.reply, ticket, signal, startedAt);
    } catch (error) {
      if (ticket !== revision || error.name === 'AbortError') return;
      if (!answered) $('voiceReply').textContent = errorMessage(error);
      phase(answered ? '语音暂不可用' : '请求失败', answered ? '文字回答已保留。' + errorMessage(error) : errorMessage(error));
    } finally {
      if (ticket === revision) $('sendVoiceText').disabled = false;
    }
  }

  async function narrate(text) {
    cancel('');
    if (!configured) { phase('待配置', '填写 API Key 后可使用 AI 语音讲解'); return; }
    lastAnswer = text;
    $('voiceReply').textContent = text;
    $('agentAnswer').textContent = text;
    $('voiceReplay').disabled = false;
    if ($('voiceMute').checked) phase('已显示', '当前为静音模式，取消静音后可重播');
    const ticket = revision;
    controller = new AbortController();
    try { await playText(text, ticket, controller.signal, performance.now()); }
    catch (error) { if (ticket === revision && error.name !== 'AbortError') phase('语音暂不可用', errorMessage(error)); }
  }

  async function toggleMic() {
    if (recording?.recorder?.state === 'recording') { finishRecording(); return; }
    if (recording) { cancel('已取消麦克风申请'); return; }
    cancel('');
    if (!configured) { phase('待配置', '请先填写 API Key 并检查配置'); return; }
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      phase('录音不可用', '当前浏览器没有录音能力，请在 Chrome / Edge 打开本页或使用文字输入'); return;
    }
    const ticket = revision;
    const rec = {cancelled: false, chunks: [], heard: false};
    recording = rec;
    phase('请求权限', '请允许网页使用麦克风，录音将在授权完成后开始');
    $('voiceTranscript').textContent = '等待麦克风授权，尚未开始录音';
    $('micButton').textContent = '取消申请';
    try {
      rec.stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
      if (ticket !== revision || rec.cancelled) { releaseRecording(rec); return; }
      const mime = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm'].find(type => MediaRecorder.isTypeSupported(type));
      if (!mime) throw new Error('浏览器没有兼容的录音格式，请使用 Chrome / Edge');
      rec.recorder = new MediaRecorder(rec.stream, {mimeType: mime});
      rec.context = new AudioContext();
      await rec.context.resume();
      if (ticket !== revision || rec.cancelled) { releaseRecording(rec); return; }
      const analyser = rec.context.createAnalyser(); analyser.fftSize = 2048;
      rec.context.createMediaStreamSource(rec.stream).connect(analyser);
      const values = new Float32Array(analyser.fftSize);
      rec.recorder.ondataavailable = event => { if (event.data.size) rec.chunks.push(event.data); };
      rec.recorder.onerror = () => { if (ticket === revision) cancel('录音设备出现错误，请重试'); };
      rec.recorder.onstop = async () => {
        releaseRecording(rec);
        if (rec.cancelled || ticket !== revision) return;
        recording = null;
        if (!rec.heard) { $('voiceTranscript').textContent = '没有检测到明显声音'; phase('没有声音', '请检查麦克风输入，或直接输入问题'); return; }
        const blob = new Blob(rec.chunks, {type: mime});
        speechStartedAt = performance.now();
        controller = new AbortController();
        phase('正在识别', '正在转写录音，识别结果可以修改后重新发送');
        $('voiceTranscript').textContent = '录音已结束，正在识别…';
        try {
          const response = await request('transcribe', {method: 'POST', headers: {'Content-Type': mime}, body: blob}, controller.signal);
          const data = await response.json();
          if (ticket !== revision) return;
          $('voiceTranscript').textContent = data.text;
          $('voiceTextInput').value = data.text;
          await ask(data.text, true);
        } catch (error) {
          if (ticket !== revision || error.name === 'AbortError') return;
          $('voiceTranscript').textContent = '本次录音未能完成转写';
          phase('识别失败', errorMessage(error));
        }
      };
      rec.started = performance.now(); rec.lastSound = rec.started;
      rec.recorder.start(250);
      $('micButton').classList.add('listening');
      $('voiceTranscript').textContent = '麦克风已开启，正在录音…';
      const meter = () => {
        if (recording !== rec || rec.cancelled || rec.recorder.state !== 'recording') return;
        analyser.getFloatTimeDomainData(values);
        const rms = Math.sqrt(values.reduce((sum, value) => sum + value * value, 0) / values.length);
        if (rms > 0.012) { rec.lastSound = performance.now(); rec.heard = true; }
        document.querySelectorAll('#voiceMeter i').forEach((bar, index) => bar.style.setProperty('--level', Math.min(1, rms * (12 + index % 4 * 3))));
        rec.frame = requestAnimationFrame(meter);
      };
      meter();
      phase('正在聆听', '说完点击“说完了”；最长录音 45 秒');
      rec.tick = setInterval(() => {
        const elapsed = (performance.now() - rec.started) / 1000;
        $('micButton').textContent = `说完了 · ${Math.floor(elapsed)} 秒`;
        const silence = Number($('voiceSilence').value);
        if (elapsed >= 45 || (silence > 0 && rec.heard && elapsed > 2 && performance.now() - rec.lastSound > silence * 1000)) finishRecording();
      }, 200);
    } catch (error) {
      releaseRecording(rec);
      if (ticket !== revision) return;
      recording = null;
      $('voiceTranscript').textContent = '麦克风未启动';
      phase('录音未启动', errorMessage(error));
    }
  }

  function finishRecording() {
    const rec = recording;
    if (rec?.recorder?.state === 'recording') {
      clearInterval(rec.tick);
      rec.recorder.stop();
      $('micButton').textContent = '开始说话';
    }
  }

  window.QihuangVoice = {ask, toggleMic, narrate, cancel, get speaking() { return !!audio && !audio.paused; }};
  $('voiceCheck').onclick = checkConfig;
  $('voiceStop').onclick = () => cancel();
  $('voiceReplay').onclick = () => {
    if (audio?.paused && audio.src) {
      audio.play().catch(() => phase('播放失败', '浏览器仍无法播放音频，请检查声音设置'));
    } else if (lastAnswer) narrate(lastAnswer);
  };
  $('voiceMute').onchange = () => { if ($('voiceMute').checked) cancel('已开启静音，仍可阅读和发送文字问题'); };
  $('voiceReset').onclick = () => { cancel('已清空对话，可以开始新话题'); history = []; showHistory(); lastAnswer = ''; $('voiceReplay').disabled = true; $('voiceTextInput').value = ''; $('voiceTranscript').textContent = '新对话已开始'; $('voiceReply').textContent = '选择一个穴位，问问它的名字、位置或文化背景。'; $('voiceSources').textContent = ''; $('voiceTiming').textContent = ''; };
  document.querySelectorAll('[data-voice-prompt]').forEach(button => button.onclick = () => {
    $('voiceStyle').value = button.dataset.voiceStyle;
    ask(button.dataset.voicePrompt);
  });
  window.addEventListener('qihuang:context', () => {
    const next = JSON.stringify(page.context());
    if (next !== contextKey && !selecting) {
      cancel('已切换页面，下一次提问将使用新的穴位上下文');
      lastAnswer = ''; $('voiceReplay').disabled = true;
    }
    contextKey = next;
    $('voiceContext').textContent = '当前：' + page.context().label;
  });
  window.addEventListener('pagehide', () => cancel(''));
  $('voiceContext').textContent = '当前：' + page.context().label;
  checkConfig();
})();
