const fs=require('fs'),vm=require('vm'),assert=require('assert');
const bridge=JSON.parse(fs.readFileSync(__dirname+'/deployment.json','utf8')).files.find(f=>f.name==='bridge.js').replacements.map(p=>p.new).join('\n');
const start=bridge.indexOf('  let mediaPollId =');const end=bridge.indexOf('  let stayDraftDirty =',start);
let n=0;const calls=[];const ctx={console,Date,Set,Map,JSON,Number,String,Boolean,Object,
 state:{currentStay:{id:'A',version:1},role:'admin',tvWelcomeVideos:{}},connected:true,
 document:{querySelector:()=>false},window:{setInterval:()=>{}},render:()=>{},pending:new Map(),
 post:(type,data)=>{calls.push({type,data});return 'r'+(++n)},welcomeBuildRequestId:'',
 stayDraftDirty:false,staySaveRequestId:''};vm.createContext(ctx);
vm.runInContext(bridge.slice(start,end)+';this.api={applyMediaBundle,pollStayMedia,requestWelcomeBuild,clearMediaView}',ctx);
ctx.api.pollStayMedia(true);assert.equal(calls[0].data.stayId,'A');
ctx.api.applyMediaBundle({stay_id:'B',version:1,state:'ready',videos:{full:{ready:true,mediaPath:'FOREIGN'}}});
assert.notEqual(ctx.state.tvWelcomeVideos.full.mediaPath,'FOREIGN');
ctx.api.applyMediaBundle({stay_id:'A',version:1,state:'scheduled',due_at:Date.now()/1000+7200,videos:{}});
assert(ctx.state.livingRoomTvWelcomeStatus.includes('запланирована'));
ctx.api.requestWelcomeBuild();ctx.api.requestWelcomeBuild();assert.equal(calls.filter(c=>c.type==='relax47/prepare-tv-welcome').length,1);
const request=calls.find(c=>c.type==='relax47/prepare-tv-welcome');assert(!request.data.videos);assert.equal(JSON.parse(request.data.sourceSignature).stayVersion,1);
ctx.state.currentStay={id:'B',version:3};ctx.api.pollStayMedia(true);assert(!ctx.state.tvWelcomeVideos.full.ready);
ctx.api.applyMediaBundle({stay_id:'A',version:1,state:'ready',videos:{full:{ready:true,mediaPath:'FOREIGN'}}});assert.notEqual(ctx.state.tvWelcomeVideos.full.mediaPath,'FOREIGN');
ctx.state.currentStay={id:'A',version:2};
ctx.api.applyMediaBundle({stay_id:'A',version:1,state:'ready',videos:{full:{ready:true,mediaPath:'OLD'}}});assert(!ctx.state.tvWelcomeVideos.full.ready);
const videos=Object.fromEntries(['full','presentation','smart','rules'].map(k=>[k,{ready:true,mediaPath:k}]));
ctx.api.applyMediaBundle({stay_id:'A',version:2,state:'ready',videos});assert(ctx.state.tvWelcomeAllReady);
ctx.stayDraftDirty=true;ctx.api.applyMediaBundle({stay_id:'A',version:2,state:'ready',videos});assert(!ctx.state.tvWelcomeAllReady);assert(!ctx.state.tvWelcomeVideos.full.ready);
const voice=require('./changes/voice.js');
const c={activeStay:true,guestName:'Гость',features:{lights:true,climate:true,gate:true,spa:true,music:true},floors:[1,2],spa:{enabled:true,sessions:[]}};
const full=voice.buildTour(c,'full'),parts=['presentation','smart','rules'].flatMap(k=>voice.buildTour(c,k));
assert.deepEqual(full,parts);assert.equal(new Set(full.map(x=>x.id)).size,full.length);
console.log('Client: cross-stay/stale response rejection, one request, dirty editor protection, four modes and full-course order passed');
