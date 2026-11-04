const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup({permission, token, turn, speech, auth, globals, resume} = {}) {
  const sockets = [], contexts = [], tracks = [], statuses = [], texts = [], turns = [], nodes = [];
  class Socket {
    static OPEN = 1;
    constructor(url) { this.url=url; this.readyState=1; this.bufferedAmount=0; this.sent=[]; sockets.push(this); }
    send(value) { this.sent.push(JSON.parse(value)); }
    close() { this.closed=true; }
    event(value) { this.onmessage?.({data:JSON.stringify(value)}); }
  }
  class Context {
    constructor() { this.sampleRate=48000; this.audioWorklet={addModule:async()=>{}}; this.destination={}; contexts.push(this); }
    async resume() { if (resume) await resume(); }
    createMediaStreamSource() { return {connect(){},disconnect(){}}; }
    createGain() { return {gain:{value:1},connect(){},disconnect(){}}; }
    async decodeAudioData() { return {}; }
    createBufferSource() { this.source={connect(){},disconnect(){},start(){this.started=true;},stop(){this.stopped=true;}}; return this.source; }
    async close() { this.closed=true; }
  }
  class Worklet { constructor(){this.port={};nodes.push(this);} connect(){} disconnect(){} }
  const track={stop(){this.stopped=true;}}; tracks.push(track);
  const scope={window:{},AudioContext:Context,AudioWorkletNode:Worklet,WebSocket:Socket,navigator:{mediaDevices:{getUserMedia:permission|| (async()=>({getTracks:()=>[track]}))}},
    URLSearchParams,Uint8Array,ArrayBuffer,Blob,btoa:value=>Buffer.from(value,'binary').toString('base64'),setInterval:()=>1,clearInterval(){},setTimeout:()=>2,clearTimeout(){},...globals};
  vm.runInNewContext(fs.readFileSync('app/static/web/handsfree.js','utf8'),scope);
  const controller=new scope.window.PippaHandsFree({token:token|| (async()=> 'temporary-token'),turn:turn|| (async text=>{turns.push(text);return 'reply-1';}),speech:speech|| (async()=>new Blob(['audio'])),
    auth:auth || (async()=>{}),status:(phase,message)=>statuses.push({phase,message}),transcript:text=>texts.push(text)});
  return {controller,sockets,contexts,tracks,statuses,texts,turns,nodes};
}
(async()=>{
  let resolveTurn;
  let env=setup({turn:text=>{env.turns.push(text);return new Promise(resolve=>{resolveTurn=resolve;});}});
  await env.controller.start();
  assert.equal(env.controller.phase,'starting');
  env.sockets[0].event({message_type:'session_started'});
  assert.equal(env.controller.phase,'listening');
  env.nodes[0].port.onmessage({data:new ArrayBuffer(3200)});
  assert.equal(env.sockets[0].sent.length,1);
  assert.equal(env.sockets[0].sent[0].sample_rate,16000);
  env.sockets[0].event({message_type:'partial_transcript',text:'Hello'});
  assert.equal(env.turns.length,0);
  env.sockets[0].event({message_type:'committed_transcript',text:'Hello Pippa'});
  env.sockets[0].event({message_type:'committed_transcript',text:'Hello Pippa'});
  assert.equal(env.turns.length,1,'duplicate commits cannot create another turn');
  assert.equal(env.controller.phase,'thinking');
  env.nodes[0].port.onmessage({data:new Uint8Array(3200).fill(99).buffer});
  assert.equal(env.sockets[0].sent.length,2,'thinking sends silence to keep the connection alive');
  assert.ok(Buffer.from(env.sockets[0].sent.at(-1).audio_base_64,'base64').every(byte=>byte===0),'thinking excludes captured microphone samples');
  resolveTurn('reply-1'); await tick(); await tick();
  assert.equal(env.controller.phase,'speaking');
  assert.equal(env.contexts[0].source.started,true,'reply plays automatically');
  env.nodes[0].port.onmessage({data:new Uint8Array(3200).fill(99).buffer});
  assert.equal(env.sockets[0].sent.length,3,'playback sends silence to keep the connection alive');
  assert.ok(Buffer.from(env.sockets[0].sent.at(-1).audio_base_64,'base64').every(byte=>byte===0),'playback excludes microphone samples');
  env.contexts[0].source.onended();
  assert.equal(env.controller.phase,'listening','playback completion returns to listening');
  env.controller.stop();
  assert.equal(env.tracks[0].stopped,true); assert.equal(env.sockets[0].closed,true); assert.equal(env.contexts[0].closed,true);
  let rejectResume;
  env=setup({resume:()=>new Promise((_,reject)=>{rejectResume=reject;})});
  const failingResume=env.controller.start();await tick();
  rejectResume(new Error('Audio context rejected'));await failingResume;
  assert.equal(env.tracks[0].stopped,true,'granted microphone is released when delayed resume fails');
  assert.equal(env.contexts[0].closed,true,'failed resume closes the audio context');
  let resolvePermission;
  env=setup({permission:()=>new Promise(resolve=>resolvePermission=resolve)});
  const starting=env.controller.start(); env.controller.stop();
  resolvePermission({getTracks:()=>env.tracks}); await starting;
  assert.equal(env.tracks[0].stopped,true,'late microphone permission is cleaned up');
  assert.equal(env.sockets.length,0);
  let resolveToken;
  env=setup({token:()=>new Promise(resolve=>resolveToken=resolve)});
  const waiting=env.controller.start(); await tick(); env.controller.stop(); resolveToken('late'); await waiting;
  assert.equal(env.sockets.length,0,'late token cannot open a socket');
  env=setup(); await env.controller.start(); env.sockets[0].event({message_type:'session_started'});
  env.sockets[0].onclose();
  assert.equal(env.controller.active,false); assert.equal(env.turns.length,0); assert.equal(env.sockets.length,1,'socket errors are not retried');
  env=setup(); await env.controller.start(); env.sockets[0].event({message_type:'session_started'});
  env.sockets[0].bufferedAmount=131073;env.nodes[0].port.onmessage({data:new ArrayBuffer(3200)});
  assert.equal(env.controller.active,false,'socket backpressure stops capture');
  // A three-second constant signal at 48kHz must produce thirty 100ms PCM16k chunks.
  let authCheck;
  env=setup({auth:async()=>{throw new Error('sign-in revoked');},globals:{setInterval:callback=>{authCheck=callback;return 1;}}});
  await env.controller.start();await env.controller.start();
  assert.equal(env.sockets.length,1,'start is idempotent while active');
  await authCheck();assert.equal(env.controller.active,false,'revoked auth stops hands-free');assert.equal(env.tracks[0].stopped,true);
  let expire;
  env=setup({globals:{setTimeout:(callback,delay)=>{if(delay===1800000)expire=callback;return 2;}}});
  await env.controller.start();expire();
  assert.equal(env.controller.active,false,'session is bounded to thirty minutes');
  assert.match(env.statuses.at(-1).message,/30-minute/);
  for(const type of ['chunk_size_exceeded','insufficient_audio_activity','commit_throttled','unaccepted_terms','missing_parameters','system_busy']) {
    env=setup();await env.controller.start();env.sockets[0].event({message_type:type});
    assert.equal(env.controller.active,false,`${type} must stop safely`);
  }
  let delayedTurn;
  env=setup({turn:()=>new Promise(resolve=>{delayedTurn=resolve;})});
  await env.controller.start(); env.sockets[0].event({message_type:'session_started'});
  env.sockets[0].event({message_type:'committed_transcript',text:'Later'});
  env.controller.stop(); delayedTurn('reply-late'); await tick();
  assert.equal(env.contexts[0].source,undefined,'stopping during a turn cannot play its late reply');
  let delayedSpeech;
  env=setup({speech:()=>new Promise(resolve=>{delayedSpeech=resolve;})});
  await env.controller.start();env.sockets[0].event({message_type:'session_started'});
  env.sockets[0].event({message_type:'committed_transcript',text:'Interrupt'});await tick();
  env.controller.stopSpeaking(); assert.equal(env.controller.phase,'listening');
  delayedSpeech(new Blob(['late']));await tick();
  assert.equal(env.contexts[0].source,undefined,'interrupting preparing prevents late playback');
  env.controller.stop();
  env=setup();await env.controller.start();env.sockets[0].event({message_type:'session_started'});
  env.sockets[0].event({message_type:'committed_transcript',text:'Interrupt playback'});await tick();await tick();
  const playing=env.contexts[0].source;env.controller.stopSpeaking();
  assert.equal(playing.stopped,true);assert.equal(env.controller.phase,'listening');assert.equal(playing.onended,null);
  env.controller.stop();
  env=setup();await env.controller.start();env.sockets[0].event({message_type:'rate_limited',error:'SECRET PROVIDER DETAILS'});
  assert.equal(env.controller.active,false);assert.equal(env.statuses.some(value=>value.message.includes('SECRET')),false);
  let Processor; const chunks=[];
  vm.runInNewContext(fs.readFileSync('app/static/web/pcm-worklet.js','utf8'),{
    AudioWorkletProcessor:class {constructor(){this.port={postMessage:chunk=>chunks.push(chunk)};}},
    registerProcessor:(name,value)=>{assert.equal(name,'pippa-pcm');Processor=value;},sampleRate:48000,Float32Array,Int16Array,Uint8Array,ArrayBuffer,DataView
  });
  const processor=new Processor();
  for(let i=0;i<1125;i++)processor.process([[new Float32Array(128).fill(.5)]]);
  assert.equal(chunks.length,30);
  assert.equal(chunks[0].byteLength,3200);
  assert.equal(new DataView(chunks[0]).getInt16(0,true),16383);
  for (const rate of [16000,22050,44100,96000]) {
    let RateProcessor;const rateChunks=[];
    vm.runInNewContext(fs.readFileSync('app/static/web/pcm-worklet.js','utf8'),{
      AudioWorkletProcessor:class {constructor(){this.port={postMessage:chunk=>rateChunks.push(chunk)};}},
      registerProcessor:(_,value)=>{RateProcessor=value;},sampleRate:rate,Float32Array,Int16Array,Uint8Array,ArrayBuffer,DataView
    });
    const resampler=new RateProcessor();
    let remaining=rate;
    while(remaining){const size=Math.min(128,remaining);resampler.process([[new Float32Array(size).fill(2),new Float32Array(size).fill(-1)]]);remaining-=size;}
    assert.equal(rateChunks.length,10,`one second at ${rate}Hz produces 10 chunks`);
    assert.equal(new DataView(rateChunks[0]).getInt16(0,true),16383,'channels are averaged to mono');
  }
  processor.process([[new Float32Array(128).fill(1)]]);
  processor.port.onmessage({data:{reset:true}});
  assert.equal(processor.offset,0,'reset discards buffered echo samples');
  console.log('Hands-free lifecycle and 48kHz PCM resampling checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
