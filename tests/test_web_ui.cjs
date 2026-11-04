const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.listeners = {}; this.children = []; this.value = ''; this.disabled = false; this.hidden = false; this.textContent = ''; this.dataset = {}; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  setAttribute(key, value) { this[key] = value; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  focus() {}
  scrollIntoView() {}
}
const tick = () => new Promise(resolve => setImmediate(resolve));
async function create(responses, extra = {}) {
  const elements = Object.fromEntries(['pippa-state','messages','welcome','status','message','composer','mic','send','stop-audio','voice-hint','chat-scroll','refresh','handsfree','live-transcript','toggle-text','text-entry','voice-status-details','voice-status-detail'].map(key => [key, new Element()]));
  elements['pippa-state'].textContent = JSON.stringify({csrf_token:'token',ready:true,voice:{stt:false,tts:true}});
  const calls = [];
  const viewportStyles = {};
  vm.runInNewContext(fs.readFileSync('app/static/web/app.js', 'utf8'), {
    document: {documentElement:{style:{setProperty:(key,value)=>{viewportStyles[key]=value;}}},addEventListener(){},getElementById:id=>elements[id],querySelectorAll:selector=> selector === '.reply-play'
      ? elements.messages.children.flatMap(row=>row.children).filter(node=>node.className==='play reply-play') : [],createElement:()=>new Element()},
    window:{addEventListener(){}}, navigator:{}, crypto:{randomUUID:()=> 'request-1'},
    AbortSignal: {timeout:()=>undefined}, Blob, URL, Audio:class {}, setTimeout, clearTimeout, ...extra,
    fetch:async(url,options)=>{calls.push({url,options}); const response=responses.shift(); if(response instanceof Error) throw response; return {ok:response.status===undefined||response.status<400,status:response.status||200,json:async()=>response.body};}
  });
  await tick();
  return {elements,calls,viewportStyles};
}
(async()=>{
  let app = await create([{body:{messages:[{id:'a1',role:'assistant',content:'<img src=x onerror=alert(1)>'}],revision:2,ready:true,voice:{stt:false,tts:true}}}]);
  assert.equal(app.elements.mic.disabled,true,'unconfigured STT is unavailable');
  assert.equal(app.elements.messages.children[0].children[1].textContent,'<img src=x onerror=alert(1)>','reply remains plain text');
  app = await create([{body:{messages:[],revision:1,ready:true,voice:{stt:false,tts:false}}},{status:409,body:{error:'stale'}},{body:{messages:[{id:'u1',role:'user',content:'Other tab'}],revision:3,ready:true,voice:{stt:false,tts:false}}}]);
  app.elements.message.value='Keep my draft';
  await app.elements.composer.listeners.submit({preventDefault(){}});
  assert.equal(app.elements.message.value,'Keep my draft','conflict retains draft');
  assert.equal(app.elements.send.disabled,false,'failed send unlocks composer');
  assert.equal(app.calls.length,3,'conflict refreshes without resending');
  assert.equal(app.calls[1].options.headers['X-CSRF-Token'],'token');
  assert.equal(JSON.parse(app.calls[1].options.body).revision,1);
  app = await create([{body:{messages:[],revision:1,ready:true,voice:{stt:false,tts:false}}},new Error('network'),new Error('network')]);
  app.elements.message.value='Unsure';
  await app.elements.composer.listeners.submit({preventDefault(){}});
  assert.equal(app.elements.message.value,'Unsure');
  assert.equal(app.elements.send.disabled,true,'unknown result blocks duplicate sends until reconciliation');
  assert.match(app.elements.status.textContent,/refresh|reload/i);
  let pendingCheck;
  app = await create([
    {body:{messages:[],revision:1,ready:true,pending:true,voice:{stt:false,tts:false}}},
    {body:{messages:[{id:'a2',role:'assistant',content:'Done'}],revision:1,ready:true,pending:false,voice:{stt:false,tts:false}}}
  ], {setTimeout:callback=>{pendingCheck=callback;return 1;},clearTimeout(){}});
  assert.equal(app.elements.send.disabled,true,'pending server work blocks duplicate turns');
  await pendingCheck();
  assert.equal(app.elements.send.disabled,false,'poll completion unlocks composer');
  assert.equal(app.elements.messages.children[0].children[1].textContent,'Done');
  app = await create([
    {body:{messages:[],revision:1,ready:true,voice:{stt:false,tts:false}}},new Error('network'),
    {body:{messages:[{id:'u3',role:'user',content:'Saved',request_id:'request-1'}],revision:2,ready:true,voice:{stt:false,tts:false}}}
  ]);
  app.elements.message.value='Saved';
  await app.elements.composer.listeners.submit({preventDefault(){}});
  assert.equal(app.elements.message.value,'','exact request receipt clears a successfully saved draft');
  assert.equal(app.calls.length,3,'saved message is reconciled without resend');
  let stopped = 0;
  class Recorder extends Element {
    static isTypeSupported(type) { return type.startsWith('audio/webm'); }
    constructor() { super(); this.state = 'inactive'; }
    start() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; this.listeners.dataavailable({data:new Blob(['voice'])}); this.listeners.stop(); }
  }
  app = await create([
    {body:{messages:[{id:'a-voice',role:'assistant',content:'Previous reply'}],revision:1,ready:true,voice:{stt:true,tts:true}}},
    {body:{text:'Transcribed text'}},
    {body:{messages:[{id:'u2',role:'user',content:'Edited transcript'}],revision:2,ready:true,voice:{stt:true,tts:false}}}
  ], {MediaRecorder:Recorder,navigator:{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[{stop:()=>stopped++}]})}}});
  await app.elements.mic.listeners.click();
  assert.equal(app.elements.mic['aria-pressed'],'true');
  assert.equal(app.elements.send.disabled,true,'cannot send while recording');
  const replyPlay = app.elements.messages.children[0].children[2];
  assert.equal(replyPlay.disabled,true,'cannot play assistant audio into a recording');
  await replyPlay.listeners.click();
  assert.equal(app.calls.length,1,'playback guard prevents a speech request during recording');
  await app.elements.mic.listeners.click();
  await tick();
  assert.equal(stopped,1,'microphone track released after recording');
  assert.equal(app.elements.message.value,'Transcribed text','transcript is reviewed before sending');
  assert.equal(app.calls.length,2,'transcription does not send a chat message');
  app.elements.message.value='Edited transcript';
  await app.elements.composer.listeners.submit({preventDefault(){}});
  assert.equal(JSON.parse(app.calls[2].options.body).voice,true,'edited transcript preserves voice origin');
  assert.equal(app.elements.message.value,'');
  let handsfree;
  class Conversation {
    constructor(options) { this.options=options; this.active=false; handsfree=this; }
    async start() { this.active=true; this.options.status('listening','Listening…'); }
    stop() { this.active=false; this.options.status('stopped','Conversation stopped.'); }
    stopSpeaking() {}
  }
  app = await create([
    {body:{messages:[],revision:0,ready:true,voice:{stt:true,tts:true}}},
    {body:{messages:[{id:'voice-u',role:'user',content:'hello'},{id:'voice-a',role:'assistant',content:'Hi'}],revision:1,ready:true,voice:{stt:true,tts:true}}}
  ], {window:{addEventListener(){},PippaHandsFree:Conversation},AudioContext:class {},AudioWorkletNode:class {},WebSocket:class {},navigator:{mediaDevices:{getUserMedia(){}}}});
  assert.equal(app.elements.handsfree.disabled,false,'hands-free becomes available with both voice services');
  await app.elements.handsfree.listeners.click();
  assert.equal(app.elements.handsfree.textContent,'Stop conversation');
  assert.equal(app.elements.mic.disabled,true,'manual recording cannot overlap hands-free');
  const replyId=await handsfree.options.turn('hello');
  assert.equal(replyId,'voice-a');
  assert.equal(JSON.parse(app.calls[1].options.body).voice,true,'hands-free transcript is voice-origin');
  assert.equal(JSON.parse(app.calls[1].options.body).revision,0,'automatic turns preserve revision protection');
  await app.elements.handsfree.listeners.click();
  assert.equal(handsfree.active,false,'Stop conversation stops the controller');
  app = await create([{body:{messages:[],revision:0,ready:true,voice:{stt:true,tts:true}}}],
    {window:{addEventListener(){},matchMedia:()=>({matches:true}),PippaHandsFree:Conversation},AudioContext:class {},AudioWorkletNode:class {},WebSocket:class {},navigator:{mediaDevices:{getUserMedia(){}}}});
  assert.equal(app.elements['text-entry'].hidden,true,'mobile keeps typing optional');
  await app.elements['toggle-text'].listeners.click();
  assert.equal(app.elements['text-entry'].hidden,false,'keyboard toggle reveals the composer');
  handsfree.options.transcript('Partial words');
  handsfree.options.status('thinking','Pippa is thinking…');
  assert.equal(app.elements['live-transcript'].textContent,'','committed transcript is not duplicated below a reply');
  handsfree.options.status('error','Voice connection closed. Hands-free stopped; your conversation is still available.');
  assert.ok(app.elements.status.textContent.length<50,'mobile error is concise');
  assert.equal(app.elements['voice-status-details'].hidden,false,'connection reason remains available');
  let resolveReply;
  app = await create([
    {body:{messages:[],revision:0,ready:true,voice:{stt:false,tts:false}}},
    {body:new Promise(resolve=>{resolveReply=resolve;})}
  ]);
  app.elements.message.value='Show my next meeting';
  app.elements['live-transcript'].textContent='stale words';
  const waiting=app.elements.composer.listeners.submit({preventDefault(){}});
  await tick();
  assert.equal(app.elements.messages.children[0].children[1].textContent,'Show my next meeting','user turn is visible while Pippa thinks');
  assert.equal(app.elements.messages.children[0].children[0].textContent,'You · Sending');
  assert.equal(app.elements['live-transcript'].textContent,'');
  resolveReply({messages:[{id:'saved-u',role:'user',content:'Show my next meeting'},{id:'saved-a',role:'assistant',content:'At 2pm'}],revision:1,ready:true,voice:{stt:false,tts:false}});
  await waiting;
  assert.equal(app.elements.messages.children.length,2,'saved response replaces optimistic turn without duplication');
  let resizeViewport;
  const viewport={height:500,scale:1,addEventListener:(_,callback)=>{resizeViewport=callback;}};
  app=await create([{body:{messages:[],revision:0,ready:true,voice:{stt:false,tts:false}}}],
    {window:{addEventListener(){},visualViewport:viewport}});
  assert.equal(app.viewportStyles['--app-height'],'500px');
  viewport.height=300;resizeViewport();
  assert.equal(app.viewportStyles['--app-height'],'300px','keyboard viewport keeps composer on screen');
  viewport.height=150;viewport.scale=2;resizeViewport();
  assert.equal(app.viewportStyles['--app-height'],'300px','pinch zoom does not shrink the app layout');
  console.log('Pippa UI: escaping, CSRF, conflict reconciliation, and unknown send, pending reply, and voice lifecycle checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
