(() => {
  'use strict';
  class PippaOpenAILive {
    constructor(callbacks) {
      this.callbacks = callbacks;
      this._active = false;
      this._phase = 'idle';
      this.sequence = 0;
      let previous;
      try { previous = window.sessionStorage?.getItem('pippa-openai-live-session'); } catch (_) {}
      if (typeof previous === 'string' && previous.length > 0 && previous.length <= 256) {
        this.run = {sessionId: previous, ending: true, cleaned: true, stopFailed: true};
      }
    }
    get active() { return this._active; }
    get phase() { return this._phase; }
    get needsStop() { return Boolean(this.run?.sessionId && this.run.ending && !this.run.stopAccepted); }
    _saveSession(run) {
      if (this.run !== run) return;
      try { window.sessionStorage?.setItem('pippa-openai-live-session', run.sessionId); } catch (_) {}
    }
    _clearSession(run) {
      try {
        if (window.sessionStorage?.getItem('pippa-openai-live-session') === run.sessionId) {
          window.sessionStorage.removeItem('pippa-openai-live-session');
        }
      } catch (_) {}
    }
    _current(run) { return this.run === run && this._active && !run.ending; }
    _set(phase, message) { this._phase = phase; this.callbacks.status(phase, message); }
    _send(run, type, values = {}) {
      if (!this._current(run) || run.channel?.readyState !== 'open') return false;
      if (run.channel.bufferedAmount > 65536) throw new Error('Voice connection is too slow. Stop and try again.');
      run.channel.send(JSON.stringify({type, event_id: `pippa_live_${++this.sequence}`, ...values}));
      return true;
    }
    async start() {
      if (this._active) return;
      if (this.needsStop) { this._set('error', 'The previous voice session still needs cleanup. Select Retry stopping voice before starting again.'); return; }
      if (this.run && !this.run.cleaned) { this._set('stopping', 'Finishing the previous voice session…'); return; }
      const run = {seen: new Set(), delegations: new Set(), inputs: [], queue: [], consumedUntil: -1,
        inputCaption: '', outputCaption: '', busy: false, ready: false};
      this.run = run;
      this._active = true;
      this._set('starting', 'Connecting OpenAI voice…');
      run.startTimer = setTimeout(() => this._fail(run, 'Voice connection timed out. Stop and try again.'), 30000);
      try {
        const Peer = window.RTCPeerConnection || (typeof RTCPeerConnection === 'undefined' ? null : RTCPeerConnection);
        if (!Peer || !navigator.mediaDevices?.getUserMedia) throw new Error('This browser does not support live voice. Use a current browser over HTTPS or localhost.');
        run.peer = new Peer();
        run.audio = new Audio(); run.audio.autoplay = true;
        run.peer.addEventListener('track', event => {
          if (!this._current(run)) return;
          run.audio.srcObject = event.streams?.[0] || new MediaStream([event.track]);
          run.audio.play().catch(() => this._fail(run, 'Your browser blocked voice playback. Allow sound for this page, then start again.'));
        });
        run.peer.addEventListener('connectionstatechange', () => {
          if (this._current(run) && ['failed', 'disconnected', 'closed'].includes(run.peer.connectionState)) {
            this._fail(run, 'Voice connection ended unexpectedly. Check the conversation before trying again.');
          }
        });
        const stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
        if (!this._current(run)) { stream.getTracks().forEach(track => track.stop()); return; }
        run.stream = stream;
        for (const track of stream.getAudioTracks()) run.peer.addTrack(track, stream);
        run.channel = run.peer.createDataChannel('oai-events');
        run.channel.addEventListener('message', event => this._message(run, event.data));
        run.channel.addEventListener('close', () => {
          if (this._current(run)) this._fail(run, 'Voice connection closed unexpectedly. Final usage is unconfirmed; check the conversation before restarting.');
        });
        run.channel.addEventListener('error', () => this._fail(run, 'Voice data connection failed. Check your conversation before restarting.'));
        await run.peer.setLocalDescription(await run.peer.createOffer());
        await this._ice(run);
        if (!this._current(run)) return;
        const offer = run.peer.localDescription?.sdp;
        if (typeof offer !== 'string' || !offer.trim()) throw new Error('The browser could not create a voice connection.');
        const result = await this.callbacks.start(offer);
        const id = result?.session?.id;
        if (typeof id === 'string' && id && id.length <= 256) { run.sessionId = id; this._saveSession(run); }
        if (!this._current(run)) {
          if (run.sessionId) await this._stopProvider(run);
          return;
        }
        if (!run.sessionId || typeof result?.transport?.sdp !== 'string' || !result.transport.sdp.trim()) {
          throw new Error('The voice service returned an invalid connection.');
        }
        await run.peer.setRemoteDescription({type: 'answer', sdp: result.transport.sdp});
        if (!this._current(run)) return;
        run.expiryTimer = setTimeout(() => {
          if (this._current(run)) this._end(run, 'The five-minute voice limit was reached. Start conversation to continue.', 'idle');
        }, 5 * 60 * 1000);
        run.authTimer = setInterval(async () => {
          if (!this._current(run) || run.authPending) return;
          run.authPending = true;
          try { await this.callbacks.auth(); }
          catch (_) { this._fail(run, 'Your sign-in could not be verified. Refresh the page before using voice.'); }
          finally { run.authPending = false; }
        }, 30000);
      } catch (error) {
        if (this._current(run)) this._fail(run, ['NotAllowedError', 'SecurityError'].includes(error.name) ?
          'Microphone permission was denied. Allow microphone access, then start again.' : error.message || 'Voice could not start. Try again or type instead.');
      }
    }
    _ice(run) {
      if (run.peer.iceGatheringState === 'complete') return Promise.resolve();
      return new Promise((resolve, reject) => {
        const finish = error => {
          clearTimeout(run.iceTimer);
          run.peer.removeEventListener('icegatheringstatechange', changed);
          run.cancelIce = null;
          if (error) reject(error); else resolve();
        };
        const changed = () => { if (run.peer.iceGatheringState === 'complete') finish(); };
        run.cancelIce = () => finish(new Error('Voice connection cancelled.'));
        run.iceTimer = setTimeout(() => finish(new Error('ICE connection setup timed out. Try another network or type instead.')), 10000);
        run.peer.addEventListener('icegatheringstatechange', changed);
        changed();
      });
    }
    _message(run, raw) {
      let event;
      try {
        if (typeof raw !== 'string' || raw.length > 65536) throw new Error('invalid event');
        event = JSON.parse(raw);
        if (!event || typeof event.type !== 'string') throw new Error('invalid event');
      } catch (_) { this._fail(run, 'Voice sent an invalid response. Your saved conversation is still available.'); return; }
      if (event.type === 'session.closed') {
        run.finalized = true;
        const reason = event.reason === 'expired' ? 'The voice session reached its time limit. Start conversation to continue.' :
          event.reason === 'connection_lost' ? 'The voice connection was lost. Check your conversation before restarting.' :
            'The voice service ended this session. Start conversation to continue.';
        if (this._current(run)) this._end(run, reason, event.reason === 'connection_lost' ? 'error' : 'idle');
        this._cleanup(run);
        return;
      }
      if (!this._current(run)) return;
      try {
        if (event.type === 'session.started') {
          run.ready = true;
          clearTimeout(run.startTimer);
          this._set('listening', 'Listening. Speak naturally.');
          if (run.queue.length) this._schedule(run);
        } else if (event.type === 'error') {
          this._fail(run, 'OpenAI voice could not complete that request. Check your conversation and voice access before restarting.');
        } else if (event.type === 'session.input_transcript.delta' || event.type === 'session.output_transcript.delta') {
          if (typeof event.event_id !== 'string' || event.event_id.length > 256 || typeof event.delta !== 'string' ||
              !Number.isFinite(event.start_ms) || !Number.isFinite(event.end_ms) || event.start_ms < 0 || event.end_ms < event.start_ms) {
            throw new Error('Voice sent an invalid transcript. Stop and try again.');
          }
          if (run.seen.has(event.event_id)) return;
          if (run.seen.size >= 4096) throw new Error('This voice session has too much transcript history. Start a new session.');
          run.seen.add(event.event_id);
          if (event.type === 'session.input_transcript.delta') {
            if (event.start_ms < run.consumedUntil) return;
            run.inputs.push({text: event.delta, start: event.start_ms, end: event.end_ms, order: run.seen.size});
            if (run.inputs.reduce((size, part) => size + part.text.length, 0) > 4000) throw new Error('That voice request is too long. Stop and type it, or try a shorter request.');
            run.inputCaption = (run.inputCaption + event.delta).slice(-1000);
            run.audio.muted = false;
            if (!run.busy) this._set('listening', 'Listening.');
            if (run.queue.length) this._schedule(run);
          } else {
            run.outputCaption = (run.outputCaption + event.delta).slice(-1000);
            this._set('speaking', 'Pippa is speaking.');
          }
          this.callbacks.transcript(`You: ${run.inputCaption}\nPippa: ${run.outputCaption}`);
        } else if (event.type === 'session.delegation.created') {
          const id = event.delegation?.id;
          if (event.delegation?.target !== 'client') return;
          if (typeof id !== 'string' || !id || id.length > 256 || !Number.isFinite(event.offset_ms) || event.offset_ms < 0) throw new Error('Voice sent an invalid task. Stop and try again.');
          if (run.delegations.has(id)) return;
          if (run.delegations.size >= 100) throw new Error('This voice session has reached its task limit. Start again to continue.');
          run.delegations.add(id);
          this._send(run, 'session.thinking.append', {delegation_id: id, content: ''});
          const delegation = {id, offset: event.offset_ms};
          delegation.expiryTimer = setTimeout(() => { delegation.expired = true; this._schedule(run); }, 2000);
          run.queue.push(delegation);
          this._schedule(run);
        }
      } catch (error) { this._fail(run, error.message || 'Voice could not continue.'); }
    }
    _schedule(run) {
      if (!this._current(run) || run.busy || run.delegationTimer) return;
      // Transcript fragments have no completed-turn event; calendar writes still require typed confirmation.
      run.delegationTimer = setTimeout(() => {
        run.delegationTimer = null;
        this._drain(run).catch(error => this._fail(run, error.message || 'Voice could not continue.'));
      }, 100);
    }
    async _drain(run) {
      if (!this._current(run) || run.busy || !run.ready) return;
      const delegation = run.queue[0];
      if (!delegation) return;
      const selected = run.inputs.filter(part => part.start <= delegation.offset).sort((a, b) => a.start - b.start || a.order - b.order);
      const text = selected.map(part => part.text).join('').trim();
      if (!text && !delegation.expired && delegation.offset > run.consumedUntil) return;
      run.queue.shift();
      clearTimeout(delegation.expiryTimer);
      if (!text) {
        try {
          this._send(run, delegation.expired ? 'session.commentary.append' : 'session.thinking.append', {delegation_id: delegation.id,
            content: delegation.expired ? 'I could not read a new request clearly. Please repeat it. The backend did not run this request.' : 'No new user transcript is available. Do not repeat previous actions; wait for a new request.'});
        } catch (error) { this._fail(run, error.message); }
        if (run.queue.length) this._schedule(run);
        return;
      }
      run.inputs = run.inputs.filter(part => !selected.includes(part));
      run.consumedUntil = Math.max(run.consumedUntil, ...selected.map(part => part.end));
      run.busy = true;
      this._set('thinking', 'Checking with your calendar assistant…');
      try {
        const reply = await this.callbacks.turn(text);
        if (!this._current(run)) return;
        if (typeof reply !== 'string' || !reply.trim()) throw new Error('The backend result is uncertain. Review the conversation before trying again.');
        let chunk = '';
        const encoder = new TextEncoder();
        for (const character of reply) {
          if (encoder.encode(chunk + character).length > 400) {
            this._send(run, 'session.thinking.append', {delegation_id: delegation.id, content: chunk});
            chunk = '';
          }
          chunk += character;
        }
        if (chunk) this._send(run, 'session.thinking.append', {delegation_id: delegation.id, content: chunk});
        this._send(run, 'session.commentary.append', {delegation_id: delegation.id,
          content: 'Explain the verified backend result just supplied. Preserve uncertainty and any request for typed confirmation. Never claim a calendar change unless the backend explicitly reported it. The complete result is also visible in chat.'});
        this._set('listening', 'Listening. Your backend result is visible in chat.');
      } catch (error) {
        if (this._current(run)) this._fail(run, error.message || 'Could not finish the voice request. Check the conversation before trying again.');
      } finally {
        run.busy = false;
        if (this._current(run) && run.queue.length) this._schedule(run);
      }
    }
    stopSpeaking() {
      const run = this.run;
      if (!run || !this._current(run)) return;
      run.audio.muted = true;
      try {
        this._send(run, 'session.instructions.append', {delegation_id: null,
          content: 'Stop speaking now and listen for the next user request. This does not cancel or repeat backend work.'});
        this._set('listening', 'Reply audio stopped. I am still listening.');
      } catch (_) { this._fail(run, 'Could not stop the spoken reply. Voice has been stopped.'); }
    }
    _fail(run, message) {
      if (this._current(run)) this._end(run, message, 'error');
    }
    async _stopProvider(run) {
      if (!run.sessionId || run.stopAccepted) return true;
      if (run.stopPromise) return run.stopPromise;
      run.stopRequested = true;
      run.stopFailed = false;
      run.stopPromise = (async () => {
        try {
          await this.callbacks.stop(run.sessionId);
          run.stopAccepted = true;
          this._clearSession(run);
          if (this.run === run && run.ending) this._set(run.endPhase || 'idle', run.endMessage || 'The voice session is stopped.');
          return true;
        } catch (error) {
          if ([404, 410].includes(error.status)) {
            run.stopAccepted = true;
            this._clearSession(run);
            if (this.run === run && run.ending) this._set(run.endPhase || 'idle', run.endMessage || 'The voice session is stopped.');
            return true;
          }
          run.stopFailed = true;
          run.stopRequested = false;
          if (this.run === run && !this._active) this._set('error', 'Microphone is off, but server voice shutdown is unconfirmed. Select Retry stopping voice.');
          return false;
        } finally { run.stopPromise = null; }
      })();
      return run.stopPromise;
    }
    stop(reason = 'Conversation stopped.') {
      const run = this.run;
      if (!run) return Promise.resolve();
      if (run.ending && !run.stopAccepted) {
        return this._stopProvider(run).then(stopped => {
          if (stopped && this.run === run) this._set('idle', 'The voice session is stopped.');
        });
      }
      return this._end(run, reason, 'idle');
    }
    _end(run, message, phase) {
      if (run.ending) return run.done;
      run.ending = true;
      run.endPhase = phase; run.endMessage = message;
      run.done = new Promise(resolve => { run.resolveDone = resolve; });
      if (this.run === run) this._active = false;
      for (const timer of [run.startTimer, run.expiryTimer, run.delegationTimer]) clearTimeout(timer);
      clearInterval(run.authTimer);
      run.cancelIce?.();
      run.stream?.getTracks().forEach(track => track.stop());
      run.audio?.pause();
      if (run.audio) run.audio.srcObject = null;
      for (const delegation of run.queue || []) clearTimeout(delegation.expiryTimer);
      run.queue = [];
      if (this.run === run) this._set(phase, message);
      if (run.channel?.readyState === 'open' && !run.finalized) {
        try { run.channel.send(JSON.stringify({type: 'session.close'})); } catch (_) {}
      }
      this._stopProvider(run);
      if (run.finalized || !run.sessionId) this._cleanup(run);
      else run.closeTimer = setTimeout(() => {
        if (this.run === run && !run.finalized && !run.stopAccepted) this._set('error', 'Microphone is off. Voice session shutdown is unconfirmed; select Retry stopping voice.');
        this._cleanup(run);
      }, 5000);
      return run.done;
    }
    _cleanup(run) {
      if (run.cleaned) return;
      run.cleaned = true;
      clearTimeout(run.closeTimer);
      run.channel?.close(); run.peer?.close();
      run.resolveDone?.();
    }
  }
  window.PippaOpenAILive = PippaOpenAILive;
})();
