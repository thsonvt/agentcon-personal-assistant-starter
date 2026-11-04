const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const tick = async () => { await new Promise(resolve => setImmediate(resolve)); await new Promise(resolve => setImmediate(resolve)); };
class Events {
  constructor() { this.listeners = {}; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(item => item !== fn); }
  emit(type, event = {}) { for (const fn of [...(this.listeners[type] || [])]) fn(event); }
}
function setup(options = {}) {
  const peers = [], audios = [], stopped = [], created = [], turns = [], statuses = [], captions = [], timers = new Map();
  let timerId = 0;
  const track = {stopped: false, stop() { this.stopped = true; }};
  const stream = {getTracks: () => [track], getAudioTracks: () => [track]};
  class Channel extends Events {
    constructor() { super(); this.readyState = 'open'; this.sent = []; this.bufferedAmount = 0; }
    send(raw) { this.sent.push(JSON.parse(raw)); }
    close() { this.readyState = 'closed'; this.emit('close'); }
    event(value) { this.emit('message', {data: JSON.stringify(value)}); }
  }
  class Peer extends Events {
    constructor() { super(); this.iceGatheringState = options.ice || 'complete'; this.connectionState = 'new'; peers.push(this); }
    addTrack(value) { this.track = value; }
    createDataChannel(label) { this.label = label; return this.channel = new Channel(); }
    async createOffer() { assert.ok(this.channel, 'event channel must precede SDP'); return {type:'offer', sdp:'v=0\noffer'}; }
    async setLocalDescription(value) { this.localDescription = value; }
    async setRemoteDescription(value) { this.remoteDescription = value; if (options.answerError) throw new Error('answer rejected'); }
    close() { this.closed = true; this.connectionState = 'closed'; this.emit('connectionstatechange'); }
  }
  class Audio extends Events {
    constructor() { super(); audios.push(this); }
    async play() { this.played = true; if (options.playError) throw Object.assign(new Error('blocked'), {name:'NotAllowedError'}); }
    pause() { this.paused = true; }
  }
  const scope = {window:{RTCPeerConnection:Peer,sessionStorage:options.storage}, RTCPeerConnection:Peer, Audio,
    navigator:{mediaDevices:{getUserMedia:options.permission || (async()=>stream)}}, MediaStream:class {constructor(tracks){this.tracks=tracks;}},
    TextEncoder, Promise, JSON, Math, Number, Set, Error,
    setTimeout(fn, delay) { const id=++timerId; timers.set(id,{fn,delay}); return id; }, clearTimeout:id=>timers.delete(id),
    setInterval(fn, delay) { const id=++timerId; timers.set(id,{fn,delay,interval:true}); return id; }, clearInterval:id=>timers.delete(id)};
  if (options.unsupported) { scope.window = {}; delete scope.RTCPeerConnection; }
  vm.runInNewContext(fs.readFileSync('app/static/web/openai-live.js','utf8'), scope);
  const callbacks = {start:options.start || (async sdp=>{created.push(sdp);return {session:{id:'live-1'},transport:{sdp:'v=0\nanswer'}};}),
    stop:options.stop || (async id=>{stopped.push(id);}), turn:options.turn || (async text=>{turns.push(text);return 'No events today.';}),
    auth:options.auth || (async()=>{}), status:(phase,message)=>statuses.push({phase,message}), transcript:text=>captions.push(text)};
  const controller = new scope.window.PippaOpenAILive(callbacks);
  const fire = async delay => { for(const [id,timer] of [...timers]) if(timer.delay===delay) { if(!timer.interval)timers.delete(id);timer.fn(); } await tick(); };
  return {controller,callbacks,peers,audios,track,stream,stopped,created,turns,statuses,captions,timers,fire};
}
const input=(id,text,start,end)=>({type:'session.input_transcript.delta',event_id:id,delta:text,start_ms:start,end_ms:end});
const delegation=(id,offset)=>({type:'session.delegation.created',event_id:'event-'+id,offset_ms:offset,delegation:{id,target:'client'}});
async function ready(env) { await env.controller.start(); const channel=env.peers[0].channel; channel.event({type:'session.started',session:{id:'live-1'}}); return channel; }
(async()=>{
  let env=setup(); let channel=await ready(env);
  assert.equal(env.controller.active,true); assert.equal(env.controller.phase,'listening');
  assert.equal(env.peers[0].label,'oai-events'); assert.equal(env.peers[0].remoteDescription.sdp,'v=0\nanswer');
  env.peers[0].emit('track',{track:{kind:'audio'},streams:[{}]}); await tick(); assert.equal(env.audios[0].played,true);
  channel.event(input('u1','What is',0,100)); channel.event(input('u1','What is',0,100));
  channel.event({type:'session.output_transcript.delta',event_id:'a1',delta:'Let me check.',start_ms:0,end_ms:100});
  channel.event(input('u2',' on my calendar?',100,200)); channel.event(delegation('d1',200)); await env.fire(100);
  assert.deepEqual(env.turns,['What is on my calendar?']);
  assert.ok(env.captions.some(text=>text.includes('You: What is on my calendar?')&&text.includes('Pippa: Let me check.')));
  channel.event(delegation('d1',200)); channel.event(delegation('d2',200)); await env.fire(100);
  assert.equal(env.turns.length,1,'duplicate delegation or consumed input never re-executes backend');
  assert.ok(channel.sent.some(item=>item.type==='session.commentary.append'&&item.delegation_id==='d1'));
  assert.ok(!channel.sent.some(item=>item.type==='session.start'));
  env.controller.stopSpeaking(); assert.equal(env.controller.active,true); assert.equal(env.track.stopped,false); assert.equal(env.audios[0].muted,true);
  channel.event(input('u3','Tomorrow instead',201,300)); assert.equal(env.audios[0].muted,false);
  const ending=env.controller.stop(); assert.equal(env.track.stopped,true,'stop releases mic immediately');
  assert.equal(env.controller.active,false); assert.ok(channel.sent.some(item=>item.type==='session.close'));
  channel.event({type:'session.closed'}); await ending; assert.equal(env.peers[0].closed,true); assert.deepEqual(env.stopped,['live-1']);

  let resolveTurn; env=setup({turn:text=>{env.turns.push(text);return new Promise(resolve=>{resolveTurn=resolve;});}}); channel=await ready(env);
  channel.event(input('t1','Friday',0,100)); channel.event(delegation('q1',100)); await env.fire(100);
  channel.event(input('t2','Actually, Thursday',101,200)); channel.event(delegation('q2',200)); await env.fire(100);
  assert.deepEqual(env.turns,['Friday'],'backend turns are serialized');
  resolveTurn('Friday is available.'); await tick(); await env.fire(100);
  assert.deepEqual(env.turns,['Friday','Actually, Thursday'],'corrections arriving in flight are not lost');
  const waitingStop=env.controller.stop(); resolveTurn('Thursday is available.'); await tick();
  assert.ok(!channel.sent.some(event=>event.type==='session.commentary.append'&&event.delegation_id==='q2'),'late result after stop is not spoken');
  channel.event({type:'session.closed'});await waitingStop;

  let resolvePermission; env=setup({permission:()=>new Promise(resolve=>{resolvePermission=resolve;})});
  const starting=env.controller.start(); env.controller.stop(); resolvePermission(env.stream); await starting;
  assert.equal(env.track.stopped,true); assert.equal(env.peers[0].closed,true);
  let resolveCreate;env=setup({start:()=>new Promise(resolve=>{resolveCreate=resolve;})});
  const pending=env.controller.start(); await tick(); env.controller.stop(); resolveCreate({session:{id:'late-id'},transport:{sdp:'answer'}});await pending;await tick();
  assert.deepEqual(env.stopped,['late-id'],'a late provider creation is stopped, not leaked');
  assert.equal(env.peers[0].remoteDescription,undefined);

  env=setup({ice:'gathering'}); const iceStart=env.controller.start();await tick(); await env.fire(10000);await iceStart;
  assert.equal(env.track.stopped,true);assert.equal(env.controller.active,false);assert.match(env.statuses.at(-1).message,/ICE|connection/i);
  env=setup(); await env.controller.start();await env.fire(30000);
  assert.equal(env.track.stopped,true);assert.deepEqual(env.stopped,['live-1']);await env.fire(5000);
  assert.equal(env.controller.active,false);
  env=setup({unsupported:true});await env.controller.start();assert.equal(env.controller.active,false);assert.match(env.statuses.at(-1).message,/support/i);
  env=setup({permission:async()=>{throw Object.assign(new Error('blocked'),{name:'NotAllowedError'});}});await env.controller.start();assert.match(env.statuses.at(-1).message,/permission/i);
  env=setup({playError:true});channel=await ready(env);env.peers[0].emit('track',{track:{kind:'audio'}});await tick();
  assert.equal(env.track.stopped,true);assert.match(env.statuses.at(-1).message,/playback|sound/i);
  env=setup();channel=await ready(env);channel.event({type:'error',error:{message:'private-provider-detail'}});await tick();
  assert.equal(env.controller.active,false);assert.ok(!env.statuses.some(item=>item.message.includes('private-provider-detail')));
  env=setup();channel=await ready(env);channel.close();await tick();assert.equal(env.track.stopped,true);assert.match(env.statuses.at(-1).message,/unconfirmed|unexpected/i);
  env=setup({stop:async()=>{throw new Error('stop failed');}});channel=await ready(env);env.controller.stop();await tick();await env.fire(5000);
  assert.match(env.statuses.at(-1).message,/unconfirmed|could not confirm/i);
  env=setup();channel=await ready(env);await env.fire(5*60*1000);assert.equal(env.track.stopped,true);assert.deepEqual(env.stopped,['live-1']);
  env=setup({auth:async()=>{throw new Error('signed out');}});channel=await ready(env);await env.fire(30000);assert.equal(env.track.stopped,true);

  env=setup({turn:async()=> '界'.repeat(900)});channel=await ready(env);channel.event(input('long1','hello',0,10));channel.event(delegation('long2',10));await env.fire(100);
  const resultEvents=channel.sent.filter(item=>item.delegation_id==='long2');
  assert.ok(resultEvents.length>3);assert.ok(resultEvents.every(item=>Buffer.byteLength(item.content||'','utf8')<=400),'context appends stay conservatively within token limit');
  assert.equal(resultEvents.filter(item=>item.type==='session.thinking.append').map(item=>item.content).join(''),'界'.repeat(900));
  env.controller.stop();channel.event({type:'session.closed'});
  const values=new Map(), storage={getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};
  let attempts=0;
  env=setup({storage,stop:async()=>{if(++attempts===1)throw new Error('temporary');}});channel=await ready(env);
  assert.equal(values.get('pippa-openai-live-session'),'live-1');
  env.controller.stop();await tick();await env.fire(5000);
  assert.equal(env.controller.needsStop,true);await env.controller.start();assert.equal(env.created.length,1,'failed stop blocks replacement creation');
  await env.controller.stop();assert.equal(env.controller.needsStop,false);assert.equal(values.size,0,'successful stop removes recovery ID');
  assert.equal(attempts,2,'failed stop is explicitly retryable');
  values.set('pippa-openai-live-session','left-on-reload');
  env=setup({storage});assert.equal(env.controller.needsStop,true);await env.controller.start();assert.equal(env.created.length,0);
  await env.controller.stop();assert.deepEqual(env.stopped,['left-on-reload']);assert.equal(values.size,0);
  values.set('pippa-openai-live-session','already-gone');
  env=setup({storage,stop:async()=>{throw Object.assign(new Error('gone'),{status:404});}});
  await env.controller.stop();assert.equal(env.controller.needsStop,false,'404 is a confirmed expired/absent owned record');assert.equal(values.size,0);

  env=setup();channel=await ready(env);channel.event(delegation('early',100));
  assert.ok(channel.sent.some(event=>event.type==='session.thinking.append'&&event.delegation_id==='early'&&event.content===''),'acknowledgement suppressed before waiting');
  await env.fire(100);assert.equal(env.turns.length,0);
  channel.event(input('late-fragment','Check today',0,100));await env.fire(100);
  assert.deepEqual(env.turns,['Check today'],'delegation waits for late input transcript');
  channel.event(delegation('no-text',500));await env.fire(100);await env.fire(2000);await env.fire(100);
  assert.equal(env.turns.length,1,'missing transcript never reuses consumed speech');
  assert.ok(channel.sent.some(event=>event.type==='session.commentary.append'&&event.delegation_id==='no-text'&&event.content.includes('repeat')));
  env.controller.stop();channel.event({type:'session.closed'});
  env=setup();channel=await ready(env);channel.event(delegation('backpressure',100));
  channel.bufferedAmount=70000;await env.fire(100);await env.fire(2000);await env.fire(100);
  assert.equal(env.controller.active,false,'no-input delegation backpressure fails closed without an unhandled timer rejection');
  assert.equal(env.track.stopped,true);assert.match(env.statuses.at(-1).message,/slow/);
  let resolveOldCreate, resolveOldStop, creations=0;
  env=setup({storage,
    start:()=> ++creations===1 ? new Promise(resolve=>{resolveOldCreate=resolve;}) : Promise.resolve({session:{id:'new-session'},transport:{sdp:'answer'}}),
    stop:id=>id==='old-session' ? new Promise(resolve=>{resolveOldStop=resolve;}) : Promise.resolve()});
  const oldStart=env.controller.start();await tick();env.controller.stop();
  await env.controller.start();assert.equal(values.get('pippa-openai-live-session'),'new-session');
  resolveOldCreate({session:{id:'old-session'},transport:{sdp:'answer'}});await tick();
  assert.equal(values.get('pippa-openai-live-session'),'new-session','late old creation never overwrites newer recovery ID');
  resolveOldStop();await oldStart;await tick();
  assert.equal(values.get('pippa-openai-live-session'),'new-session','late old shutdown never clears newer recovery ID');
  env.controller.stop();env.peers[1].channel.event({type:'session.closed'});await tick();
  assert.equal(values.size,0,'matching current shutdown clears its own recovery ID');
  env=setup();channel=await ready(env);const confirmedStop=env.controller.stop();await tick();await env.fire(5000);await confirmedStop;
  assert.equal(env.controller.phase,'idle','confirmed server hangup is a normal Stop even when final usage event is absent');
  assert.equal(env.track.stopped,true);assert.equal(env.peers[0].closed,true);assert.equal(env.controller.needsStop,false);
  assert.notEqual(env.controller.run.finalized,true,'HTTP hangup does not pretend the final usage event was observed');
  console.log('OpenAI Live: media lifecycle, transcript delegation, replay protection, and failure handling passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
