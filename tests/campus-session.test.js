'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {CampusCookies,allowedUrls}=require('../src/thu_assistant/cookies');
const {SessionStore,identity}=require('../src/thu_assistant/session-store');
const {browserSync,authenticationState,shouldRetryModel}=require('../src/thu_assistant/session-policy');
const origin='https://learn.tsinghua.edu.cn';
const cookie=(jar,text)=>jar.absorb(origin+'/course/list',text);
function storeFixture(t){
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'thu-session-test-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const held=new Set(),locks={acquirePidLock:async f=>{assert(!held.has(f));held.add(f);return {ok:true};},releasePidLock:f=>held.delete(f)};
 const opts={dir,account:{username:'synthetic'},locks,dpapi:(mode,buf)=>buf,atomicWrite:(file,data)=>{const tmp=file+'.tmp';fs.writeFileSync(tmp,data);fs.renameSync(tmp,file);}};
 return {dir,opts,store:new SessionStore(opts),held};
}
function bootstrap(jar){return {version:2,revision:0,account:identity({username:'synthetic'}),serviceOrigin:origin,url:origin+'/',cookies:jar.browserCookies(allowedUrls(origin)),cookieRecords:jar.export(),appOrigins:[]};}
test('same-name paths, host-only and parent-domain scopes stay separate',()=>{
 const j=new CampusCookies();cookie(j,'sid=root; Path=/; Secure; HttpOnly');cookie(j,'sid=course; Path=/course');cookie(j,'shared=x; Domain=tsinghua.edu.cn; Path=/');
 assert.equal(j.valueFor(origin+'/course/list','sid'),'course');assert.equal(j.valueFor(origin+'/other','sid'),'root');
 assert.equal(j.headerFor('https://library.tsinghua.edu.cn/'),'shared=x');
 assert(!j.headerFor('https://child.learn.tsinghua.edu.cn/').includes('sid='));
 assert.throws(()=>j.headerFor('http://learn.tsinghua.edu.cn/'));
 assert.equal(j.export().find(c=>c.key==='sid'&&c.path==='/').httpOnly,true);
});
test('Max-Age overrides Expires and cannot slide with reads; deletion removes only matching path',async()=>{
 const j=new CampusCookies();cookie(j,'sid=root; Path=/; Max-Age=3600; Expires=Thu, 01 Jan 1970 00:00:00 GMT');cookie(j,'sid=course; Path=/course');
 const before=j.export().find(c=>c.path==='/');assert.equal(before.sourceMaxAge,3600);assert(Date.parse(before.expires)>Date.now());
 j.headerFor(origin+'/');assert.equal(j.export().find(c=>c.path==='/').expires,before.expires);
 cookie(j,'sid=deleted; Path=/; Max-Age=0; Expires=Thu, 01 Jan 2099 00:00:00 GMT');
 assert.equal(j.valueFor(origin+'/','sid'),'');assert.equal(j.valueFor(origin+'/course','sid'),'course');
 cookie(j,'past=x; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT');assert(!j.headerFor(origin+'/').includes('past='));
});
test('browser roundtrip preserves scope, security, SameSite and expiration',()=>{
 const j=new CampusCookies();cookie(j,'sid=root; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=600');
 cookie(j,'sid=course; Path=/course; Domain=tsinghua.edu.cn; SameSite=None; Secure');
 const b=j.browserCookies(allowedUrls(origin)),fresh=new CampusCookies();for(const c of b)fresh.setBrowser(c,allowedUrls(origin));
 assert.deepEqual(fresh.browserCookies(allowedUrls(origin)),b);
 const boot=bootstrap(j),apps={};browserSync(j,{cookies:b,origins:[]},boot,boot.account,apps);
 assert.equal(j.export().find(c=>c.path==='/').sourceMaxAge,600);
});
test('browser receives only target/auth cookies and rejects other app injection',()=>{
 const j=new CampusCookies();cookie(j,'target=yes; Path=/');j.absorb('https://library.tsinghua.edu.cn/','other=no; Path=/');j.absorb('https://id.tsinghua.edu.cn/','sso=yes; Path=/');
 const b=bootstrap(j);assert.deepEqual(b.cookies.map(c=>c.name).sort(),['sso','target']);
 assert.throws(()=>j.setBrowser({name:'x',value:'y',domain:'library.tsinghua.edu.cn',path:'/'},allowedUrls(origin)),{code:'BROWSER_COOKIE_SCOPE'});
 const apps={};browserSync(j,{cookies:b.cookies,origins:[{origin,localStorage:[{name:'token',value:'synthetic'}]},{origin:'https://library.tsinghua.edu.cn',localStorage:[{name:'token',value:'discard'}]}]},b,b.account,apps);
 assert.deepEqual(Object.keys(apps),[origin]);assert.equal(apps[origin].origins.length,1);
});
test('browser deletion, concurrent changes and already expired cookies',()=>{
 const j=new CampusCookies();cookie(j,'sid=old; Path=/');const b=bootstrap(j);
 browserSync(j,{cookies:[],origins:[]},b,b.account,{});assert.equal(j.export().length,0);
 browserSync(j,{cookies:[],origins:[]},b,b.account,{}); // duplicate deletion is harmless
 cookie(j,'sid=newer; Path=/');assert.throws(()=>browserSync(j,{cookies:[],origins:[]},b,b.account,{}),{code:'STATE_CONFLICT'});
 assert.throws(()=>browserSync(j,{cookies:[],origins:[]},b,'another',{}),{code:'STATE_ACCOUNT_MISMATCH'});
});
test('unchanged browser localStorage cannot overwrite newer same-app state',()=>{
 const j=new CampusCookies(),b=bootstrap(j),remote=[{origin,localStorage:[{name:'new',value:'value'}]}],apps={[origin]:{origins:remote}};
 browserSync(j,{cookies:[],origins:[]},b,b.account,apps);assert.deepEqual(apps[origin].origins,remote);
 assert.throws(()=>browserSync(j,{cookies:[],origins:[{origin,localStorage:[]}]},b,b.account,apps),{code:'STATE_CONFLICT'});
});
test('repeated browser saves neither delete unseen HTTP cookies nor acknowledge unseen changes',()=>{
 const j=new CampusCookies();cookie(j,'sid=before; Path=/');const boot=bootstrap(j),data={cookies:boot.cookies,origins:[]};
 cookie(j,'sid=remote; Path=/');cookie(j,'new=remote; Path=/');
 const next=browserSync(j,data,boot,boot.account,{});
 browserSync(j,data,next,boot.account,{});
 assert.equal(j.valueFor(origin+'/','new'),'remote');assert.equal(j.valueFor(origin+'/','sid'),'remote');
 const changed={...data,cookies:data.cookies.map(c=>({...c,value:'local'}))};
 assert.throws(()=>browserSync(j,changed,next,boot.account,{}),{code:'STATE_CONFLICT'});
});
test('legacy remains untouched and missing attributes rebuild into version 2',async t=>{
 const {dir,store}=storeFixture(t);const file=path.join(dir,'campus.dpapi');fs.writeFileSync(file,'legacy-ciphertext');
 const base=await store.load();assert.equal(base.migration.policy,'REBUILD_ON_DEMAND');assert.deepEqual(base.cookies,[]);
 const j=new CampusCookies();cookie(j,'fresh=yes; Path=/; Secure');const saved=await store.commit(base,j,{});
 assert.equal(saved.version,2);assert.equal(saved.revision,1);assert.equal(fs.readFileSync(file,'utf8'),'legacy-ciphertext');assert.equal(fs.readFileSync(file+'.before-v2','utf8'),'legacy-ciphertext');
});
test('failed encryption leaves legacy and last good state intact',async t=>{
 const {dir,store}=storeFixture(t);fs.writeFileSync(path.join(dir,'campus.dpapi'),'old-cipher');let b=await store.load();
 store.dpapi=()=>{throw Error('synthetic');};await assert.rejects(store.commit(b,new CampusCookies(),{}),{code:'STATE_ENCRYPT_FAILED'});assert(!fs.existsSync(store.file));
 store.dpapi=(m,b)=>b;b=await store.commit(b,new CampusCookies(),{});const prior=fs.readFileSync(store.file);
 store.dpapi=(m,b)=>{if(m==='protect')throw Error('synthetic');return b;};await assert.rejects(store.commit(b,new CampusCookies(),{}),{code:'STATE_ENCRYPT_FAILED'});assert.deepEqual(fs.readFileSync(store.file),prior);
});
test('corruption, decrypt failure, version and account errors remain distinct',async t=>{
 const {store}=storeFixture(t);fs.writeFileSync(store.file,'invalid!');await assert.rejects(store.load(),{code:'STATE_CORRUPT'});
 fs.writeFileSync(store.file,Buffer.from('not-json').toString('base64'));await assert.rejects(store.load(),{code:'STATE_CORRUPT'});
 store.dpapi=()=>{throw Error();};await assert.rejects(store.load(),{code:'STATE_DECRYPT_FAILED'});store.dpapi=(m,b)=>b;
 fs.writeFileSync(store.file,Buffer.from(JSON.stringify({version:99})).toString('base64'));await assert.rejects(store.load(),{code:'STATE_VERSION_UNSUPPORTED'});
 fs.writeFileSync(store.file,Buffer.from(JSON.stringify({version:2,account:'different'})).toString('base64'));await assert.rejects(store.load(),{code:'STATE_ACCOUNT_MISMATCH'});
});
test('account switch during an operation cannot commit old account state',async t=>{
 const {store}=storeFixture(t);const b=await store.load();store.currentAccount=()=>({username:'switched'});
 await assert.rejects(store.commit(b,new CampusCookies(),{}),{code:'STATE_ACCOUNT_MISMATCH'});assert(!fs.existsSync(store.file));
});
test('independent stale writes merge; conflicting values fail without overwriting',async t=>{
 const {store,held}=storeFixture(t);const a=await store.load(),b=await store.load();assert.equal(held.size,0);
 const x=new CampusCookies(),y=new CampusCookies();cookie(x,'x=1; Path=/');cookie(y,'y=2; Path=/');
 await store.commit(a,x,{});const merged=await store.commit(b,y,{});assert.equal(merged.cookies.length,2);
 const stale=await store.load(),j1=new CampusCookies(stale.cookies),j2=new CampusCookies(stale.cookies);cookie(j1,'x=3; Path=/');cookie(j2,'x=4; Path=/');
 await store.commit(stale,j1,{});await assert.rejects(store.commit(stale,j2,{}),{code:'STATE_CONFLICT'});assert.equal(new CampusCookies((await store.load()).cookies).valueFor(origin+'/','x'),'3');assert.equal(held.size,0);
});
test('authentication evidence is site specific and model retry only precedes output',()=>{
 const r=(statusCode,body='',finalUrl=origin)=>authenticationState({statusCode,body,finalUrl},origin);
 assert.equal(r(200,'public page').authentication_state,'UNKNOWN');assert.equal(r(401).authentication_state,'EXPIRED');assert.equal(r(403).authentication_state,'FORBIDDEN');assert.equal(r(200,'<input type="password">').authentication_state,'LOGIN_REQUIRED');
 assert.equal(r(200,'退出登录').authentication_state,'AUTHENTICATED');assert.equal(r(200,'退出登录','https://other.tsinghua.edu.cn').authentication_state,'UNKNOWN');
 assert(shouldRetryModel({type:'upstream-error',status:401},false));for(const status of [403,429,500])assert(!shouldRetryModel({type:'upstream-error',status},false));assert(!shouldRetryModel({type:'upstream-error',status:401},true));assert(!shouldRetryModel({type:'network-error'},false));
});
