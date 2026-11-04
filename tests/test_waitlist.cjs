const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync('app/static/landing/waitlist.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
class Element {
  constructor(value='') { this.value=value; this.dataset={}; this.handlers={}; this.disabled=false; this.hidden=false; this.textContent=''; this.checked=false; }
  addEventListener(name, fn) { this.handlers[name]=fn; }
  async fire(name, extra={}) { return this.handlers[name]?.({preventDefault(){},target:this,...extra}); }
  setAttribute(name,value) { this[name]=value; }
  removeAttribute(name) { delete this[name]; }
  focus() { this.focused=true; }
  reportValidity() { return this.valid !== false; }
  showModal() { this.open=true; }
  close() { this.open=false; this.fire('close'); }
  reset() { this.resetCalled=true; }
}
async function setup(submitFetch=ok, {valid=true,config={enabled:true,siteKey:'test-key',unsupportedDomains:['yahoo.com','aol.com']},configFails=false,email=' me@gmail.com ',preloaded=true}={}) {
  const ids=['waitlist','waitlist-feedback','waitlist-email','waitlist-entry','waitlist-survey','survey-form','survey-progress','survey-title','survey-back','survey-next','survey-skip','survey-close','survey-feedback','survey-first-name','survey-tools-other','survey-other-wrap','survey-challenge','survey-wish','survey-success','survey-success-title','survey-fields','survey-done','waitlist-challenge'];
  const els=Object.fromEntries(ids.map(id=>[id,new Element()]));
  const steps=Array.from({length:4},()=>new Element());
  const tools=['google_calendar','outlook','apple_calendar','calendly','cal_com','whatsapp','manual','other'].map(v=>new Element(v));
  els['waitlist-email'].value=email; els.waitlist.valid=valid; els.waitlist.action='/api/waitlist';
  els.waitlist.querySelector=()=>els['waitlist-entry'];
  let callbacks, script, resets=0, renders=0;
  const calls=[];
  const turnstile={render:(_,options)=>{renders++;callbacks=options;return 'widget-id';},reset:()=>{resets++;}};
  const win=preloaded?{turnstile}:{};
  vm.runInNewContext(code,{
    window:win, document:{querySelector:s=>els[s.slice(1)],querySelectorAll:s=>s==='[data-survey-step]'?steps:tools,head:{appendChild:s=>{script=s;}},createElement:()=>({}),body:{style:{}}},
    FormData:class { get(name){return {email:els['waitlist-email'].value,website:'',pippaHold:'false',mailingLists:'attacker'}[name];} },
    URLSearchParams,AbortSignal,
    fetch:async(url,options)=>{if(url==='/api/waitlist/config'){if(configFails)throw Error('offline');return {ok:true,json:async()=>config};} calls.push({url,...options});return submitFetch(url,options);},
  });
  await tick();
  const p={els,steps,tools,win,calls,get callbacks(){return callbacks;},get script(){return script;},get resets(){return resets;},get renders(){return renders;},verify:()=>callbacks.callback('verified-token'),open:()=>els.waitlist.fire('submit'),next:()=>els['survey-form'].fire('submit'),close:()=>els['survey-close'].fire('click')};
  p.finish=async()=>{await p.open();els['survey-first-name'].value='Son';await p.next();await p.next();await p.next();};
  return p;
}
const ok=async()=>({ok:true,status:200,json:async()=>({success:true})});

test('email CTA opens survey without sending or loading security check; first name required',async()=>{
 const p=await setup();await p.open();assert.equal(p.els['waitlist-survey'].open,true);assert.equal(p.calls.length,0);assert.equal(p.renders,0);
 await p.next();assert.equal(p.steps[0].hidden,false);assert.match(p.els['survey-feedback'].textContent,/first name/i);
 p.els['survey-first-name'].value='Son';await p.next();assert.equal(p.steps[1].hidden,false);
});
test('back and close preserve answers in memory and do not send',async()=>{
 const p=await setup();await p.open();p.els['survey-first-name'].value='Son';await p.next();p.tools[0].checked=true;await p.next();p.els['survey-challenge'].value='Too much back and forth';
 await p.els['survey-back'].fire('click');assert.equal(p.tools[0].checked,true);await p.close();await p.open();assert.equal(p.els['survey-challenge'].value,'Too much back and forth');assert.equal(p.calls.length,0);
});
test('all research questions can be skipped; only final verified submit posts allowed fields once',async()=>{
 const p=await setup();await p.finish();assert.equal(p.renders,1);await p.next();assert.equal(p.calls.length,0);p.verify();await p.next();
 const data=new URLSearchParams(p.calls[0].body);assert.deepEqual([...data.keys()],['email','website','cf-turnstile-response','surveyVersion','firstName','schedulingTools','schedulingToolsOther','schedulingChallenge','pippaWish']);
 assert.equal(data.get('email'),'me@gmail.com');assert.equal(data.get('firstName'),'Son');assert.equal(data.get('surveyVersion'),'1');assert.equal(data.get('schedulingTools'),'[]');
 assert.equal(p.els['survey-success'].hidden,false);assert.match(p.els['survey-success-title'].textContent,/Son/);assert.equal(p.resets,0);await p.next();assert.equal(p.calls.length,1);
});
test('selected tools and text answers reach one final request',async()=>{
 const p=await setup();await p.finish();p.tools[0].checked=true;p.tools[7].checked=true;p.els['survey-tools-other'].value='Notebook';p.els['survey-challenge'].value='Time zones';p.els['survey-wish'].value='Find a free hour';p.verify();await p.next();const data=new URLSearchParams(p.calls[0].body);
 assert.equal(data.get('schedulingTools'),'["google_calendar","other"]');assert.equal(data.get('schedulingToolsOther'),'Notebook');assert.equal(data.get('schedulingChallenge'),'Time zones');assert.equal(data.get('pippaWish'),'Find a free hour');
});
for(const [name,reply] of [
 ['HTTP rejection',async()=>({ok:false,status:400,json:async()=>({success:false})})],['human check',async()=>({ok:false,status:403,json:async()=>({success:false})})],['false success',async()=>({ok:true,status:200,json:async()=>({success:false})})],['rate limit',async()=>({ok:false,status:429})],['network failure',async()=>{throw Error('offline');}],['invalid JSON',async()=>({ok:true,status:200,json:async()=>{throw Error('JSON');}})],['timeout',async()=>{throw Error('timeout');}]
])test(name+' retains answers and requires a new token',async()=>{
 const p=await setup(reply);await p.finish();p.verify();await p.next();assert.equal(p.els['survey-feedback'].dataset.state,'error');assert.equal(p.els['survey-first-name'].value,'Son');assert.equal(p.els['survey-next'].disabled,true);assert.equal(p.resets,1);p.verify();assert.equal(p.els['survey-next'].disabled,false);
});
test('config failure still allows answering but final submission fails closed',async()=>{const p=await setup(ok,{configFails:true});await p.finish();assert.equal(p.renders,0);await p.next();assert.equal(p.calls.length,0);assert.equal(p.els['survey-next'].disabled,true);});
test('disabled config fails closed',async()=>{const p=await setup(ok,{config:{enabled:false}});await p.finish();assert.equal(p.renders,0);assert.equal(p.els['survey-next'].disabled,true);});
test('invalid email never opens survey',async()=>{const p=await setup(ok,{valid:false});await p.open();assert.equal(p.els['waitlist-survey'].open,undefined);assert.equal(p.calls.length,0);});
for(const email of ['person@yahoo.com','person@AOL.COM','person@mail.yahoo.com'])test('rejects unsupported '+email,async()=>{const p=await setup(ok,{email});await p.open();assert.equal(p.els['waitlist-survey'].open,undefined);assert.equal(p.calls.length,0);assert.equal(p.els['waitlist-feedback'].dataset.state,'eligibility');});
test('expiry and widget errors block final submit',async()=>{const p=await setup();await p.finish();p.verify();p.callbacks['expired-callback']();await p.next();assert.equal(p.calls.length,0);p.verify();p.callbacks['error-callback']();await p.next();assert.equal(p.calls.length,0);});
test('loads CDN only on final step when not preloaded, script failure fails closed',async()=>{const p=await setup(ok,{preloaded:false});await p.open();assert.equal(p.script,undefined);p.els['survey-first-name'].value='Son';await p.next();await p.next();await p.next();assert.match(p.script.src,/challenges.cloudflare.com/);p.script.onerror();assert.equal(p.els['survey-next'].disabled,true);});
test('concurrent final clicks send only one request',async()=>{let resolve;const p=await setup(()=>new Promise(r=>resolve=r));await p.finish();p.verify();const pending=p.next();await p.next();assert.equal(p.calls.length,1);resolve(await ok());await pending;});
for(const code of ['workspace_unverified','email_check_unavailable','invalid_email','invalid_survey'])test('actionable backend '+code+' preserves survey',async()=>{const p=await setup(async()=>({ok:false,status:422,json:async()=>({success:false,code})}));await p.finish();p.verify();await p.next();assert.ok(p.els['survey-feedback'].textContent);assert.equal(p.els['survey-first-name'].value,'Son');assert.equal(p.resets,1);});
test('overlong first name cannot advance',async()=>{const p=await setup();await p.open();p.els['survey-first-name'].value='A'.repeat(81);await p.next();assert.equal(p.steps[0].hidden,false);});
test('skip controls advance optional questions and retain name',async()=>{const p=await setup();await p.open();p.els['survey-first-name'].value='Son';await p.next();await p.els['survey-skip'].fire('click');assert.equal(p.steps[2].hidden,false);await p.els['survey-skip'].fire('click');assert.equal(p.steps[3].hidden,false);assert.equal(p.els['survey-first-name'].value,'Son');assert.equal(p.calls.length,0);});
test('close restores focus to entry and submission prevents Escape dismissal',async()=>{let resolve;const p=await setup(()=>new Promise(r=>resolve=r));await p.finish();await p.close();assert.equal(p.els['waitlist-entry'].focused,true);await p.open();p.verify();const pending=p.next();let prevented=false;await p.els['waitlist-survey'].fire('cancel',{preventDefault(){prevented=true;}});assert.equal(prevented,true);resolve(await ok());await pending;});
test('late challenge callback while sending cannot enable another request',async()=>{let resolve;const p=await setup(()=>new Promise(r=>resolve=r));await p.finish();p.verify();const pending=p.next();p.verify();assert.equal(p.els['survey-next'].disabled,true);resolve(await ok());await pending;assert.equal(p.els['survey-next'].disabled,true);await p.next();assert.equal(p.calls.length,1);});
test('cleared Other selection omits stale free text from submission',async()=>{const p=await setup();await p.finish();p.els['survey-tools-other'].value='Old tool';p.verify();await p.next();assert.equal(new URLSearchParams(p.calls[0].body).get('schedulingToolsOther'),'');});
test('success name is assigned as text, not inserted as HTML',async()=>{const p=await setup();await p.finish();p.els['survey-first-name'].value='<img src=x onerror=alert(1)>';p.verify();await p.next();assert.equal(p.els['survey-success-title'].textContent,"Thanks, <img src=x onerror=alert(1)>. You're on the list.");assert.equal(p.els['survey-success-title'].innerHTML,undefined);});
test('a completed signup can reopen and obtain a fresh challenge for another signup',async()=>{const p=await setup();await p.finish();p.verify();await p.next();await p.close();await p.open();p.els['survey-first-name'].value='Second';await p.next();await p.next();const before=p.resets;await p.next();assert.equal(p.resets,before+1,'entering the final step resets the existing consumed widget');assert.equal(p.els['survey-next'].disabled,true);p.verify();await p.next();assert.equal(p.calls.length,2);assert.equal(new URLSearchParams(p.calls[1].body).get('firstName'),'Second');});
test('revisiting the final step does not overlap a pending widget reset',async()=>{const p=await setup();await p.finish();p.verify();p.callbacks['expired-callback']();await p.els['survey-back'].fire('click');await p.next();assert.equal(p.resets,1);await p.els['survey-back'].fire('click');await p.next();assert.equal(p.resets,1);p.verify();assert.equal(p.els['survey-next'].disabled,false);});
