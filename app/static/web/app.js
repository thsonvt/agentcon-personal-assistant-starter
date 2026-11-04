(() => {
  'use strict';
  const bootstrap = JSON.parse(document.getElementById('pippa-state').textContent);
  const el = id => document.getElementById(id);
  const composer = el('composer'), input = el('message'), mic = el('mic'), send = el('send');
  const status = el('status'), messages = el('messages'), stopAudio = el('stop-audio');
  const conversationButton = el('handsfree');
  const textEntry = el('text-entry'), textToggle = el('toggle-text');
  function typing(show) {
    if (!textEntry || !textToggle) return;
    textEntry.hidden = !show;
    textToggle.setAttribute('aria-expanded', String(show));
    textToggle.textContent = show ? 'Hide keyboard' : 'Type a message';
    if (show && !input.disabled) input.focus();
  }
  typing(!window.matchMedia?.('(max-width: 760px)').matches);
  textToggle?.addEventListener('click', () => typing(textEntry.hidden));
  if (window.visualViewport && document.documentElement) {
    const viewport = window.visualViewport;
    const resize = () => {
      if (viewport.scale === 1) document.documentElement.style.setProperty('--app-height', `${viewport.height}px`);
    };
    viewport.addEventListener('resize', resize);
    resize();
  }
  let handsFree = null;
  let state = {messages: [], revision: 0, ready: false, voice: bootstrap.voice || {}};
  let busy = false, uncertain = false, voiceDraft = false, recorder = null, stream = null;
  let recordingTimer = null, recordingCancelled = false, player = null, audioUrl = null;
  let playbackVersion = 0, recordingVersion = 0, pollTimer = null, pollAttempts = 0;
  const codec = () => typeof MediaRecorder === 'undefined' ? '' :
    ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'].find(type => MediaRecorder.isTypeSupported(type)) || '';
  const uuid = () => crypto.randomUUID ? crypto.randomUUID() :
    'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const random = crypto.getRandomValues(new Uint8Array(1))[0] & 15;
      return (c === 'x' ? random : (random & 3) | 8).toString(16);
    });
  function controls() {
    const conversation = Boolean(handsFree?.active);
    input.disabled = conversation || busy || Boolean(recorder) || !state.ready || Boolean(state.pending);
    send.disabled = conversation || busy || Boolean(recorder) || !state.ready || uncertain || Boolean(state.pending);
    mic.disabled = conversation || busy || !state.ready || Boolean(state.pending) || !state.voice.stt || !navigator.mediaDevices?.getUserMedia || !codec();
    conversationButton.disabled = !conversation && (!handsFree || busy || recorder || uncertain || state.pending || !state.ready || !state.voice.stt || !state.voice.tts || !navigator.mediaDevices?.getUserMedia || !(window.AudioContext || typeof AudioContext !== 'undefined' || window.webkitAudioContext) || typeof AudioWorkletNode === 'undefined' || typeof WebSocket === 'undefined');
    conversationButton.textContent = conversation ? 'Stop conversation' : 'Start conversation';
    stopAudio.textContent = conversation ? 'Stop speaking' : 'Stop audio';
    conversationButton.setAttribute('aria-pressed', String(conversation));
    if (textToggle) textToggle.disabled = conversation;
    composer.setAttribute('aria-busy', String(busy));
    mic.setAttribute('aria-pressed', String(Boolean(recorder)));
    mic.textContent = recorder ? 'Stop recording' : 'Record voice';
    document.querySelectorAll('[data-prompt]').forEach(button => { button.disabled = input.disabled; });
    document.querySelectorAll('.reply-play').forEach(button => {
      button.disabled = conversation || busy || Boolean(recorder) || !state.ready || Boolean(state.pending);
    });
  }
  async function request(path, options = {}, audio = false) {
    const response = await fetch(path, {credentials: 'same-origin', signal: AbortSignal.timeout(90000), ...options});
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const error = new Error(body.error || `Request failed (${response.status}).`);
      error.status = response.status;
      throw error;
    }
    return audio ? response.blob() : response.json();
  }
  const jsonPost = value => ({method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': bootstrap.csrf_token}, body: JSON.stringify(value)});
  function stopPlayback() {
    playbackVersion += 1;
    if (player) { player.pause(); player = null; }
    if (audioUrl) { URL.revokeObjectURL(audioUrl); audioUrl = null; }
    stopAudio.hidden = true;
  }
  async function speak(messageId, button) {
    if (handsFree?.active || busy || recorder || !state.ready || state.pending) return;
    stopPlayback();
    const version = playbackVersion;
    button.disabled = true;
    status.textContent = 'Preparing audio…';
    stopAudio.hidden = false;
    try {
      const audio = await request('/api/web/speech', jsonPost({message_id: messageId}), true);
      if (version !== playbackVersion) return;
      audioUrl = URL.createObjectURL(audio);
      player = new Audio(audioUrl);
      player.addEventListener('ended', stopPlayback, {once: true});
      player.addEventListener('error', () => { stopPlayback(); status.textContent = 'Audio could not play. You can still read the reply.'; }, {once: true});
      await player.play();
      if (version === playbackVersion) status.textContent = 'Playing Pippa’s reply.';
    } catch (error) {
      if (version === playbackVersion) { stopPlayback(); status.textContent = error.message || 'Audio unavailable. You can still read the reply.'; }
    } finally { button.disabled = false; }
  }
  function render(next) {
    state = next;
    messages.replaceChildren();
    for (const message of state.messages || []) {
      if (!['user', 'assistant'].includes(message.role)) continue;
      const row = document.createElement('li');
      row.className = `message ${message.role}`;
      const speaker = document.createElement('span');
      speaker.className = 'speaker';
      speaker.textContent = message.role === 'user' ? (message.pending ? 'You · Sending' : 'You') : 'Pippa';
      const content = document.createElement('p');
      content.textContent = message.content;
      row.append(speaker, content);
      if (message.role === 'assistant' && state.voice.tts) {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'play reply-play'; button.textContent = 'Play reply';
        button.addEventListener('click', () => speak(message.id, button));
        row.append(button);
      }
      messages.append(row);
    }
    el('welcome').hidden = Boolean(messages.children.length);
    el('voice-hint').textContent = !state.voice.stt ? 'Voice input is not configured yet. You can type your message.' :
      !navigator.mediaDevices?.getUserMedia || !codec() ? 'Voice recording is unavailable in this browser. Use a supported browser over HTTPS, or type.' :
        'Start conversation for hands-free voice, or Record voice to review a transcript before sending.';
    controls();
    clearTimeout(pollTimer);
    if (state.pending && pollAttempts < 10) {
      status.textContent = 'Pippa is still working. Checking for your reply…';
      pollTimer = setTimeout(async () => {
        pollAttempts += 1;
        try { await refresh(); if (!state.pending) { status.textContent = ''; pollAttempts = 0; } }
        catch (_) { status.textContent = 'Could not check your reply. Refresh the conversation to check again.'; el('refresh').hidden = false; }
      }, 3000);
    } else if (state.pending) {
      status.textContent = 'Pippa is still working. Refresh the conversation to check for your reply.';
      el('refresh').hidden = false;
    } else { pollAttempts = 0; }
    el('chat-scroll').scrollTop = state.messages?.length ? el('chat-scroll').scrollHeight : 0;
  }
  async function refresh() { render(await request('/api/web/state')); }
  async function sendMessage(text, fromVoice) {
    if (busy || recorder || uncertain || state.pending || !state.ready || !text) return;
    const requestId = uuid();
    busy = true; controls(); stopPlayback(); status.textContent = 'Pippa is thinking…';
    el('live-transcript').textContent = '';
    render({...state, messages: [...state.messages, {id: requestId, role: 'user', content: text, pending: true}]});
    try {
      render(await request('/api/web/message', jsonPost({message: text, voice: fromVoice, revision: state.revision, request_id: requestId})));
      input.value = ''; voiceDraft = false; if (!state.pending) status.textContent = '';
      return [...state.messages].reverse().find(message => message.role === 'assistant')?.id;
    } catch (error) {
      if (error.status && error.status < 500 && error.status !== 409) {
        render({...state, messages: state.messages.filter(message => message.id !== requestId)});
        status.textContent = error.message;
      } else {
        try {
          await refresh();
          const saved = (state.messages || []).some(message => message.request_id === requestId);
          if (saved) { input.value = ''; voiceDraft = false; status.textContent = 'Conversation refreshed. Your message was saved; check the reply before sending again.'; }
          else status.textContent = error.status === 409 ? 'The conversation changed. Your draft is still here; review the latest messages before sending.' :
            'Could not confirm the send. Your draft is still here; review the latest messages before sending again.';
        } catch (_) {
          uncertain = true; el('refresh').hidden = false;
          status.textContent = 'Could not check whether your message was saved. Reload to refresh the conversation before sending again. Copy your draft first.';
        }
      }
    } finally { busy = false; controls(); if (!input.disabled) input.focus(); }
  }
  composer.addEventListener('submit', async event => {
    event.preventDefault();
    if (handsFree?.active) return;
    await sendMessage(input.value.trim(), voiceDraft);
  });

  if (window.PippaHandsFree) {
    handsFree = new window.PippaHandsFree({
      token: async () => (await request('/api/web/realtime-token', jsonPost({}))).token,
      turn: async text => {
        const replyId = await sendMessage(text, true);
        if (!replyId || state.pending) throw new Error('Review the conversation before starting again.');
        return replyId;
      },
      speech: replyId => request('/api/web/speech', jsonPost({message_id: replyId}), true),
      auth: async () => {
        const current = await request('/api/web/state');
        if (!current.ready) throw new Error('Sign in to continue.');
      },
      status: (phase, message) => {
        const labels = {starting: 'Connecting…', listening: 'Listening', thinking: 'Thinking…', preparing: 'Preparing reply…', speaking: 'Pippa is speaking', idle: ''};
        status.textContent = phase === 'error' ? (message.includes('permission') ? 'Microphone blocked. Check permissions.' : message.includes('sign-in') ? 'Sign in again to use voice.' : 'Voice disconnected. Tap Start to retry.') : labels[phase] ?? message;
        const details = el('voice-status-details'), detail = el('voice-status-detail');
        if (details && detail) { details.hidden = phase !== 'error'; detail.textContent = phase === 'error' ? message : ''; }
        if (document.body) document.body.dataset.voicePhase = phase;
        if (phase !== 'listening') el('live-transcript').textContent = '';
        stopAudio.hidden = !['preparing', 'speaking'].includes(phase);
        controls();
      },
      transcript: text => { el('live-transcript').textContent = text; },
    });
  }
  conversationButton.addEventListener('click', async () => {
    if (!handsFree) return;
    if (handsFree.active) { handsFree.stop(); return; }
    if (busy || recorder || uncertain || state.pending || !state.ready) return;
    if (input.value.trim()) { status.textContent = 'Send or clear your draft before starting a conversation.'; input.focus(); return; }
    stopPlayback();
    await handsFree.start();
    controls();
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); composer.requestSubmit(); }
  });
  document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => {
    typing(true); input.value = button.dataset.prompt; voiceDraft = false; input.focus();
  }));
  function stopTracks() {
    clearTimeout(recordingTimer);
    if (stream) { stream.getTracks().forEach(track => track.stop()); stream = null; }
  }
  mic.addEventListener('click', async () => {
    if (handsFree?.active) return;
    if (recorder) { recorder.stop(); return; }
    if (busy || !state.ready || Boolean(state.pending) || !state.voice.stt) return;
    if (input.value.trim()) { status.textContent = 'Send or clear your draft before recording a new message.'; input.focus(); return; }
    const mimeType = codec();
    if (!mimeType || !navigator.mediaDevices?.getUserMedia) { status.textContent = 'This browser cannot record voice. You can type instead.'; return; }
    stopPlayback(); busy = true; controls(); status.textContent = 'Allow microphone access to record.';
    const version = ++recordingVersion;
    try {
      stream = await navigator.mediaDevices.getUserMedia({audio: true});
      if (version !== recordingVersion) { stopTracks(); return; }
      recordingCancelled = false;
      const chunks = []; let size = 0;
      recorder = new MediaRecorder(stream, {mimeType});
      recorder.addEventListener('dataavailable', event => {
        if (!event.data.size) return;
        size += event.data.size;
        if (size > 5 * 1024 * 1024) {
          recordingCancelled = true; status.textContent = 'Recording exceeded 5 MB. Please try a shorter message.';
          if (recorder && recorder.state !== 'inactive') recorder.stop();
        } else chunks.push(event.data);
      });
      recorder.addEventListener('error', () => {
        recordingCancelled = true; status.textContent = 'Recording failed. You can type your message instead.';
        const active = recorder; recorder = null; stopTracks(); busy = false; controls();
        if (active && active.state !== 'inactive') active.stop();
      });
      recorder.addEventListener('stop', async () => {
        recorder = null; stopTracks(); busy = true; controls();
        try {
          if (recordingCancelled) return;
          const audio = new Blob(chunks, {type: mimeType});
          if (!audio.size) { status.textContent = 'No audio was recorded. Try again or type a message.'; return; }
          status.textContent = 'Transcribing your recording…';
          const result = await request('/api/web/transcribe', {method: 'POST', headers: {'Content-Type': mimeType, 'X-CSRF-Token': bootstrap.csrf_token}, body: audio});
          if (!result.text?.trim()) { status.textContent = 'No speech detected. Try again or type a message.'; return; }
          input.value = result.text.trim(); voiceDraft = true;
          typing(true);
          status.textContent = 'Review your transcript, then press Send. You can edit it first.';
        } catch (error) { status.textContent = error.message || 'Transcription failed. You can type your message instead.'; }
        finally { busy = false; controls(); if (!input.disabled) input.focus(); }
      });
      recorder.start(1000);
      busy = false; controls(); status.textContent = 'Recording… press Stop recording when you’re done (60 seconds maximum).';
      recordingTimer = setTimeout(() => { if (recorder && recorder.state !== 'inactive') recorder.stop(); }, 60000);
    } catch (error) {
      recorder = null; stopTracks(); busy = false; controls();
      status.textContent = ['NotAllowedError', 'SecurityError'].includes(error.name) ? 'Microphone permission was denied. Allow it in browser settings, or type your message.' : 'Microphone unavailable. Check your device or type your message.';
    }
  });
  el('refresh').addEventListener('click', async () => {
    el('refresh').disabled = true; pollAttempts = 0;
    try { await refresh(); uncertain = false; el('refresh').hidden = true; controls(); if (!state.pending) status.textContent = 'Conversation refreshed. Review the messages before sending again.'; }
    catch (_) { status.textContent = 'Could not refresh the conversation. Check your connection and try again.'; }
    finally { el('refresh').disabled = false; }
  });
  stopAudio.addEventListener('click', () => {
    if (handsFree?.active) { handsFree.stopSpeaking(); return; }
    stopPlayback(); status.textContent = '';
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && handsFree?.active) handsFree.stop();
  });
  window.addEventListener('pagehide', () => {
    if (handsFree?.active) handsFree.stop();
    clearTimeout(pollTimer); recordingVersion += 1; recordingCancelled = true;
    if (recorder && recorder.state !== 'inactive') recorder.stop();
    stopTracks(); stopPlayback();
  });
  controls();
  refresh().then(() => { if (!state.ready) status.textContent = 'Pippa’s assistant is not configured yet. Please try again later.'; })
    .catch(error => { status.textContent = error.message || 'Could not load your conversation. Refresh to try again.'; el('refresh').hidden = false; controls(); });
})();
