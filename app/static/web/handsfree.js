(() => {
  'use strict';
  class PippaHandsFree {
    constructor(callbacks) {
      this.callbacks = callbacks;
      this._active = false;
      this._phase = 'idle';
      this._version = 0;
      this._replyVersion = 0;
      this._authPending = false;
    }
    get active() { return this._active; }
    get phase() { return this._phase; }
    _set(phase, message) {
      if (phase === 'listening') this.worklet?.port.postMessage?.({reset: true});
      this._phase = phase;
      this.callbacks.status(phase, message);
    }
    _current(version) { return this._active && this._version === version; }
    _fail(message) {
      this.stop();
      this._set('error', message);
    }
    async start() {
      if (this._active) return;
      this._active = true;
      const version = ++this._version;
      this._set('starting', 'Connecting hands-free voice…');
      try {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext ||
          (typeof AudioContext === 'undefined' ? null : AudioContext);
        if (!AudioContextClass || !navigator.mediaDevices?.getUserMedia || typeof AudioWorkletNode === 'undefined') {
          throw new Error('unsupported');
        }
        const context = new AudioContextClass({sampleRate: 16000});
        this.context = context;
        // A late permission response must also stop tracks after an earlier failure or explicit stop.
        const permission = navigator.mediaDevices.getUserMedia({audio: {
          echoCancellation: true, noiseSuppression: true, autoGainControl: true
        }}).then(stream => {
          if (this._current(version)) this.stream = stream;
          else stream.getTracks().forEach(track => track.stop());
          return stream;
        });
        const [, stream] = await Promise.all([context.resume(), permission]);
        if (!this._current(version)) return;
        await context.audioWorklet.addModule('/pippa-web-assets/pcm-worklet.js');
        if (!this._current(version)) return;
        this.source = context.createMediaStreamSource(stream);
        this.worklet = new AudioWorkletNode(context, 'pippa-pcm');
        this.gain = context.createGain(); this.gain.gain.value = 0;
        this.source.connect(this.worklet); this.worklet.connect(this.gain); this.gain.connect(context.destination);
        this.worklet.port.onmessage = event => {
          if (!this._current(version) || this._phase === 'starting') return;
          const socket = this.socket;
          if (!socket || socket.readyState !== WebSocket.OPEN) return;
          if (socket.bufferedAmount > 128 * 1024) { this._fail('Voice connection is too slow. Hands-free stopped; you can type instead.'); return; }
          const captured = new Uint8Array(event.data);
          const bytes = this._phase === 'listening' ? captured : new Uint8Array(captured.byteLength);
          if (bytes.byteLength !== 3200) { this._fail('Voice capture failed. Hands-free stopped; you can type instead.'); return; }
          let binary = '';
          for (const byte of bytes) binary += String.fromCharCode(byte);
          try {
            socket.send(JSON.stringify({message_type: 'input_audio_chunk', audio_base_64: btoa(binary), sample_rate: 16000}));
          } catch (_) { this._fail('Voice connection failed. Hands-free stopped; you can type instead.'); }
        };
        this.worklet.onprocessorerror = () => {
          if (this._current(version)) this._fail('Voice capture failed. Hands-free stopped; you can type instead.');
        };
        const token = await this.callbacks.token();
        if (!this._current(version)) return;
        if (!token || typeof token !== 'string') throw new Error('token');
        const params = new URLSearchParams({model_id: 'scribe_v2_realtime', audio_format: 'pcm_16000',
          commit_strategy: 'vad', vad_silence_threshold_secs: '1.0', min_speech_duration_ms: '200', keepalive_interval_ms: '1000', token});
        const socket = new WebSocket(`wss://api.elevenlabs.io/v1/speech-to-text/realtime?${params}`);
        this.socket = socket;
        socket.onmessage = event => {
          if (!this._current(version)) return;
          let message;
          try { message = JSON.parse(event.data); }
          catch (_) { this._fail('Voice connection sent an invalid response. Hands-free stopped.'); return; }
          if (message.message_type === 'session_started' && this._phase === 'starting') {
            clearTimeout(this.connectionTimer);
            this._set('listening', 'Listening. Speak naturally; Pippa replies when you pause.');
          } else if (message.message_type === 'partial_transcript' && this._phase === 'listening') {
            if (typeof message.text === 'string') this.callbacks.transcript(message.text.slice(0, 4000));
          } else if (message.message_type === 'committed_transcript' && this._phase === 'listening') {
            if (typeof message.text !== 'string' || !message.text.trim()) return;
            if (message.text.length > 4000) { this._fail('That voice message was too long. Hands-free stopped; try a shorter message.'); return; }
            const text = message.text.trim();
            this.callbacks.transcript(text);
            this._set('thinking', 'Pippa is thinking…');
            this._turn(text, version, ++this._replyVersion);
          } else if (['chunk_size_exceeded', 'insufficient_audio_activity', 'commit_throttled', 'unaccepted_terms', 'missing_parameters', 'system_busy'].includes(message.message_type) || /error|expired|rate.limit|auth|quota|resource_exhausted|session_ended/i.test(message.message_type || '')) {
            this._fail('Voice service is unavailable or this session expired. Hands-free stopped; you can type instead.');
          }
        };
        socket.onerror = socket.onclose = () => {
          if (this._current(version)) this._fail('Voice connection closed. Hands-free stopped; your conversation is still available.');
        };
        this.connectionTimer = setTimeout(() => {
          if (this._current(version) && this._phase === 'starting') this._fail('Voice connection timed out. Hands-free stopped; you can type instead.');
        }, 20000);
        this.expiryTimer = setTimeout(() => {
          if (this._current(version)) this._fail('This 30-minute hands-free session ended. Start again to continue.');
        }, 30 * 60 * 1000);
        this.authTimer = setInterval(async () => {
          if (!this._current(version) || this._authPending) return;
          this._authPending = true;
          try { await this.callbacks.auth(); }
          catch (_) { if (this._current(version)) this._fail('Your sign-in could not be verified. Hands-free stopped; refresh to continue.'); }
          finally { if (this._current(version)) this._authPending = false; }
        }, 30000);
      } catch (error) {
        if (!this._current(version)) return;
        this._fail(['NotAllowedError', 'SecurityError'].includes(error.name) ?
          'Microphone permission was denied. Allow it in browser settings, or type instead.' :
          'Could not start hands-free voice. Check microphone access and voice configuration, or type instead.');
      }
    }
    async _turn(text, version, replyVersion) {
      const current = () => this._current(version) && this._replyVersion === replyVersion;
      try {
        const replyId = await this.callbacks.turn(text);
        if (!current()) return;
        this._set('preparing', 'Preparing Pippa’s voice…');
        const blob = await this.callbacks.speech(replyId);
        if (!current()) return;
        const bytes = await blob.arrayBuffer();
        if (!current()) return;
        const audio = await this.context.decodeAudioData(bytes);
        if (!current()) return;
        const playback = this.context.createBufferSource();
        playback.buffer = audio; playback.connect(this.context.destination);
        this.playback = playback;
        playback.onended = () => {
          playback.disconnect();
          if (!current()) return;
          this.playback = null;
          this._set('listening', 'Listening. Speak naturally; Pippa replies when you pause.');
        };
        this._set('speaking', 'Pippa is speaking. Use Stop speaking to interrupt.');
        playback.start();
      } catch (_) {
        if (current()) this._fail('Could not complete the voice reply. Hands-free stopped; check your conversation before sending again.');
      }
    }
    stopSpeaking() {
      if (!this._active || !['speaking', 'preparing'].includes(this._phase)) return;
      this._replyVersion += 1;
      if (this.playback) {
        this.playback.onended = null;
        try { this.playback.stop(); } catch (_) {}
        this.playback.disconnect(); this.playback = null;
      }
      this._set('listening', 'Listening. Speak naturally; Pippa replies when you pause.');
    }
    stop() {
      this._active = false; this._version += 1; this._replyVersion += 1; this._authPending = false;
      clearTimeout(this.connectionTimer); clearTimeout(this.expiryTimer); clearInterval(this.authTimer);
      if (this.socket) {
        this.socket.onmessage = this.socket.onclose = this.socket.onerror = null;
        try { this.socket.close(); } catch (_) {}
        this.socket = null;
      }
      if (this.playback) {
        this.playback.onended = null;
        try { this.playback.stop(); } catch (_) {}
        this.playback.disconnect(); this.playback = null;
      }
      if (this.worklet) { this.worklet.port.onmessage = null; this.worklet.onprocessorerror = null; }
      for (const node of [this.source, this.worklet, this.gain]) { if (node) node.disconnect(); }
      this.source = this.worklet = this.gain = null;
      if (this.stream) { this.stream.getTracks().forEach(track => track.stop()); this.stream = null; }
      if (this.context) { this.context.close().catch(() => {}); this.context = null; }
      this._set('idle', 'Hands-free is off.');
    }
  }
  window.PippaHandsFree = PippaHandsFree;
})();
