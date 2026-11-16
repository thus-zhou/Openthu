'use strict';
// stdin/stdout is a private subprocess boundary. Authentication material is
// never returned as plaintext; browser bootstrap is itself DPAPI sealed.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { once } = require('node:events');
const { createRequire } = require('node:module');
const { validateUrl, assertReadUrl, toTunnel, safeUrl, redact, sanitizeJson } = require('./security');
const helper = require(process.env.THU_ACCESS_HELPER || path.join(__dirname, '../../runtime/windows/access.js'));
const up = createRequire(path.join(process.env.THU_MADMODEL_DIR || path.join(__dirname, '../../vendor/madmodel-proxy'), 'package.json'));
const auth = up('./madmodel-auth');
const credentials = up('./platform/credentials');
const locks = up('./platform/process-lock');
const paths = up('./platform/paths');
const { atomicWrite } = up('./platform/file-store');
const {withPidLock,AuthGate,SerialQueue,errorRecord} = require('./scheduler');
const stateDir = process.env.THU_ASSISTANT_STATE_DIR || path.join(process.env.LOCALAPPDATA, 'TsinghuaAssistant');
const {CampusCookies,allowedUrls,inScope} = require('./cookies');
const {SessionStore} = require('./session-store');
const {authenticationState,shouldRetryModel,browserSync,loginRequired} = require('./session-policy');
const {execFileSync} = require('node:child_process');
const {collectPages,parseCount}=require('./pagination');
const authGate = new AuthGate(locks,paths.AUTH_LOCK);
let activeControl = {}, activeSession = null;
const fail = code => Object.assign(new Error(code), {code});
let secrets = [];
console.warn = () => {}; console.error = () => {}; console.log = () => {};
async function withAuthentication(action) {
  return authGate.run(async()=>{
    const s=activeSession;
    if(s){const rebased=await s.store.rebase(s.base,s.jar,s.apps,activeControl);s.base=rebased.base;s.apps=rebased.apps;}
    const result=await action();
    if(s){s.base=await s.store.commit(s.base,s.jar,s.apps,activeControl);s.jar.replace(s.base.cookies);s.apps=structuredClone(s.base.apps);}
    return result;
  },activeControl);
}
function sealBrowser(jar,account,serviceOrigin,url,apps,revision){
  const urls=allowedUrls(serviceOrigin,url),origins=apps[serviceOrigin]?.origins||[];
  return helper.dpapi('protect',Buffer.from(JSON.stringify({version:2,account:activeSession.store.account,serviceOrigin,url,
    cookies:jar.browserCookies(urls),cookieRecords:jar.export().filter(c=>inScope(c,urls)),
    origins,appOrigins:origins,revision,redactions:[account?.username,account?.fingerPrint].filter(Boolean)}))).toString('base64');
}
function importLearning(jar,token){
  const cookies=jar.browserCookies(['https://learn.tsinghua.edu.cn/']);
  try{return JSON.parse(execFileSync(process.env.THU_PYTHON||'python',[path.join(path.dirname(process.env.THU_ACCESS_HELPER||path.join(__dirname, '../../runtime/windows/access.js')),'import-session.py')],
    {input:JSON.stringify({cookies,csrf:token}),encoding:'utf8',windowsHide:true,stdio:['pipe','pipe','pipe'],timeout:45000}));}
  catch{throw fail('LEARN_PLUGIN_IMPORT_FAILED');}
}
function learningSignature(){
  try{return crypto.createHash('sha256').update(fs.readFileSync(path.join(process.env.LOCALAPPDATA,'LearnAssistant','session.dpapi'))).digest('hex');}
  catch{return null;}
}
function markLearning(){
  const origin='https://learn.tsinghua.edu.cn';
  activeSession.apps[origin]={...(activeSession.apps[origin]||{}),providerSignature:learningSignature(),authentication_state:'AUTHENTICATED',checkedAt:new Date().toISOString()};
}

function collectSecrets(jar, account) {
  const t = credentials.readToken();
  secrets = [account?.username, account?.password, account?.fingerPrint, t?.token,
    ...Object.values(jar.cookies).flat().map(c => c.value)];
}

function decodeHtml(value) {
  return String(value || '').replace(/&amp;/g,'&').replace(/&quot;/g,'"').replace(/&#39;|&apos;/g,"'")
    .replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&nbsp;?/g,' ')
    .replace(/&#(\d+);/g,(_,n)=>String.fromCodePoint(Number(n)));
}

function textOnly(html) {
  return decodeHtml(html.replace(/<!--[\s\S]*?-->/g,' ').replace(/<(script|style|template)\b[^>]*>[\s\S]*?<\/\1>/gi,' ')
    .replace(/<input\b[^>]*>/gi,' ').replace(/<[^>]*>/g,' ')).replace(/\s+/g,' ').trim();
}

function pageResult(response,options={}) {
  const body = response.body || '';
  let json;
  try { json = JSON.parse(body); } catch { /* HTML or text. */ }
  if (json !== undefined) {
    const data=sanitizeJson(json,secrets);
    if(Buffer.byteLength(JSON.stringify(data))>8*1024*1024)throw fail('RESULT_TOO_LARGE_SELECT_NARROWER_SOURCE');
    return { status:response.statusCode>=200&&response.statusCode<300?'JSON_READ_VERIFIED':'HTTP_ERROR', httpStatus:response.statusCode,
      url:safeUrl(response.finalUrl), data };
  }
  const login = loginRequired(response);
  const links = [];
  for (const m of body.matchAll(/<a\b[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi)) {
    try {
      const target = new URL(decodeHtml(m[1]), response.finalUrl);
      if (!['https:','http:'].includes(target.protocol)) continue;
      const label = textOnly(m[2]);
      if (label) links.push({label:redact(label,secrets).slice(0,100),url:safeUrl(target.href),
        schoolDomain:target.hostname === 'tsinghua.edu.cn' || target.hostname.endsWith('.tsinghua.edu.cn')});
    } catch { /* Skip non-navigation links. */ }
  }
  const title = textOnly(/<title[^>]*>([\s\S]*?)<\/title>/i.exec(body)?.[1] || '');
  const content = textOnly(body);
  const text=redact(content,secrets),offset=Math.max(0,Number(options.offset)||0),limit=options.snapshot?text.length:Math.min(100000,Math.max(1,Number(options.limit)||24000));
  if(Buffer.byteLength(text)+Buffer.byteLength(JSON.stringify(links))>8*1024*1024)throw fail('RESULT_TOO_LARGE_SELECT_NARROWER_SOURCE');
  const jsOnly = /enable javascript|doesn't work properly without javascript|请开启.*javascript/i.test(content) || content.length < 50 && /<script/i.test(body);
  return { status:login?'AUTH_REQUIRED':response.statusCode>=200&&response.statusCode<300?(jsOnly?'BROWSER_REQUIRED':'PAGE_READ_VERIFIED'):'HTTP_ERROR',
    httpStatus:response.statusCode,url:safeUrl(response.finalUrl),title:redact(title,secrets),
    authenticated:login?false:null,maintenance:response.statusCode===503&&/维护|maintenance/i.test(content),
    text:login?'学校要求登录或再次认证。':text.slice(offset,offset+limit),
    totalCharacters:text.length,offset,nextOffset:offset+limit<text.length?offset+limit:null,links:login?[]:links };
}

async function downloadFile(client,account,input,destination,maxBytes) {
  if(typeof destination!=='string'||!path.isAbsolute(destination))throw fail('ABSOLUTE_DESTINATION_FILE_REQUIRED');
  const target=path.resolve(destination);
  if(target.split(/[\\/]/).some(p=>/^(?:\.codex|\.madmodel-proxy|LearnAssistant|TsinghuaAssistant|credentials|private)$/i.test(p)))
    throw fail('DOWNLOAD_DESTINATION_IS_CREDENTIAL_STORAGE');
  if(fs.existsSync(target))throw fail('DOWNLOAD_TARGET_ALREADY_EXISTS');
  if(/[:<>"|?*]/.test(path.basename(target)))throw fail('INVALID_DESTINATION_FILENAME');
  const budget=Math.min(1024*1024*1024,Math.max(1024,Number(maxBytes)||256*1024*1024));
  let url=assertReadUrl(input).href,res;
  for(let attempt=0;attempt<2;attempt++) {
    for(let hop=0;hop<16;hop++) {
      assertReadUrl(url);
      res=await fetch(url,{headers:{Cookie:client.jar.headerFor(url)},redirect:'manual',signal:AbortSignal.timeout(120000)});
      client.jar.absorb(url,res.headers.getSetCookie());
      if(res.status>=300&&res.status<400&&res.headers.get('location')) {
        const next=new URL(res.headers.get('location'),url).href;
        await res.body?.cancel();assertReadUrl(next);url=next;res=null;continue;
      }
      break;
    }
    if(!res)throw fail('DOWNLOAD_REDIRECT_LIMIT');
    const type=res.headers.get('content-type')||'';
    if(/text\/html/i.test(type)&&!['.html','.htm'].includes(path.extname(target).toLowerCase())) {
      await res.body?.cancel();
      if(attempt===0){await read(client,input,account,true);url=input;continue;}
      throw fail('DOWNLOAD_RETURNED_HTML_NOT_FILE');
    }
    if(!res.ok){await res.body?.cancel();throw fail('DOWNLOAD_HTTP_ERROR');}
    break;
  }
  fs.mkdirSync(path.dirname(target),{recursive:true});
  const temporary=target+'.'+crypto.randomUUID()+'.part';
  const declared=Number(res.headers.get('content-length'));
  if(declared>budget){await res.body?.cancel();throw fail('DOWNLOAD_BYTE_LIMIT');}
  let bytes=0,htmlHead='';const hash=crypto.createHash('sha256'),stream=fs.createWriteStream(temporary,{flags:'wx'});
  let writeFailure=null;stream.on('error',e=>{writeFailure=e;});
  try {
    const reader=res.body.getReader();
    try {
      for(;;) {
        const item=await reader.read();if(item.done)break;
        bytes+=item.value.length;if(bytes>budget)throw fail('DOWNLOAD_BYTE_LIMIT');
        if(htmlHead.length<32768)htmlHead+=Buffer.from(item.value).toString('utf8').slice(0,32768-htmlHead.length);
        hash.update(item.value);
        if(writeFailure)throw writeFailure;
        if(!stream.write(item.value))await once(stream,'drain');
      }
    }finally{try{await reader.cancel();}catch{}}
    const finished=once(stream,'finish');stream.end();await finished;
    if(!bytes)throw fail('DOWNLOAD_EMPTY');
    if(/sm2publicKey|<input[^>]*type=["']password|<title[^>]*>[^<]*(?:登录|认证)/i.test(htmlHead))throw fail('DOWNLOAD_IS_LOGIN_PAGE');
    const extension=path.extname(target).toLowerCase();
    if(extension==='.pdf'&&!htmlHead.slice(0,1024).includes('%PDF-'))throw fail('DOWNLOAD_FILE_SIGNATURE_MISMATCH');
    if(['.zip','.docx','.xlsx','.pptx'].includes(extension)&&!/^PK[\u0003\u0005\u0007]/.test(htmlHead))throw fail('DOWNLOAD_FILE_SIGNATURE_MISMATCH');
    const digest=hash.digest('hex');
    // EXCL prevents an existing file from being overwritten, even if another
    // process created it while the network transfer was in progress.
    fs.copyFileSync(temporary,target,fs.constants.COPYFILE_EXCL);
    const record={status:'DOWNLOADED_VERIFIED',path:target,bytes,sha256:digest,mimeType:res.headers.get('content-type')||'',
      signatureChecked:['.pdf','.zip','.docx','.xlsx','.pptx'].includes(extension),
      url:safeUrl(url),checkedAt:new Date().toISOString()};
    try{fs.appendFileSync(path.join(path.dirname(target),'.thu-assistant-downloads.jsonl'),JSON.stringify(record)+'\n');}
    catch{record.status='DOWNLOADED_VERIFIED_WITH_AUDIT_WARNING';record.auditStatus='MANIFEST_WRITE_FAILED';}
    return record;
  }finally{
    const closed=stream.closed?null:new Promise(resolve=>stream.once('close',resolve));
    stream.destroy();if(closed)await closed;
    try{fs.unlinkSync(temporary);}catch{}
  }
}

async function read(client, url, account, authenticate = true) {
  assertReadUrl(url);
  let response = await auth.requestWithRedirects({url,timeout:20000},client.jar);
  if (authenticate && new URL(response.finalUrl).hostname === 'id.tsinghua.edu.cn' && /sm2publicKey/.test(response.body)) {
    if (!account) throw fail('ACCOUNT_CREDENTIALS_UNAVAILABLE');
    if(activeControl.identityAttempted)throw fail('AUTH_RECOVERY_EXHAUSTED');
    activeControl.identityAttempted=true;
    const logged = await withAuthentication(()=>client.authenticateIdentity(response.finalUrl,
      'https://id.tsinghua.edu.cn/do/off/ui/auth/login/check',account.username,account.password,account.fingerPrint,
      ()=>{throw fail('TWO_FACTOR_REQUIRED');},response));
    let target = logged.redirectUrl;
    if (new URL(target).protocol === 'http:') target = client.toLbRedirectUrl(target);
    response = await auth.requestWithRedirects({url:target,timeout:20000},client.jar);
  }
  // Some campus CAS callbacks return a 200 page with a scripted/meta redirect.
  for (let n=0;n<3;n++) {
    const callbackPage=['id.tsinghua.edu.cn','oauth.tsinghua.edu.cn'].includes(new URL(response.finalUrl).hostname)
      || response.body.length<8000 && textOnly(response.body).length<200;
    const m=/<meta[^>]*http-equiv=["']?refresh["']?[^>]*content=["'][^"']*?url=([^"']+)/i.exec(response.body)
      || (callbackPage?/location\.(?:href|replace)\s*[=(]\s*["']([^"']+)["']/i.exec(response.body):null);
    if (!m) break;
    let next=new URL(decodeHtml(m[1]),response.finalUrl).href;
    if(new URL(next).protocol==='http:')next=client.toLbRedirectUrl(next);
    assertReadUrl(next);
    response=await auth.requestWithRedirects({url:next,timeout:20000},client.jar);
  }
  collectSecrets(client.jar,account);
  return response;
}

async function ensureInfo(client,account) {
  if(!account)throw fail('ACCOUNT_CREDENTIALS_UNAVAILABLE');
  await withAuthentication(()=>client.ensureWebVpnSession(account.username,account.password,account.fingerPrint,()=>{throw fail('TWO_FACTOR_REQUIRED');}));
}

async function portalPost(client,endpoint,data) {
  if(!['getAppList','getAppSize'].includes(endpoint))throw fail('READ_ENDPOINT_NOT_REGISTERED');
  const r=await auth.requestWithRedirects({url:auth.INFO_PREFIX+'/b/yyfw/vyyfwxx/info/portal_fg/common/'+endpoint+
    '?_csrf='+encodeURIComponent(client.portalCsrf),method:'POST',data:JSON.stringify(data),
    header:{'Content-Type':'application/json;charset=utf-8'},timeout:20000},client.jar);
  if(r.statusCode===429)throw fail('RATE_LIMITED');
  if(r.statusCode===403)throw fail('FORBIDDEN');
  if(r.statusCode===401||loginRequired(r))throw fail('AUTH_REQUIRED');
  if(r.statusCode===503&&/维护|maintenance/i.test(r.body))throw fail('MAINTENANCE');
  let j;try{j=JSON.parse(r.body);}catch{throw fail('PORTAL_NOT_JSON');}
  if(r.statusCode!==200||j.result!=='success'||j.success===false)throw fail('PORTAL_DIRECTORY_FAILED');
  return j.object;
}

async function discover(client,account) {
  await ensureInfo(client,account);
  const total=parseCount(await portalPost(client,'getAppSize',{currentPage:1,ywlbmArray:[]}));
  const apps=new Map();let invalidIds=0;
  const paging=await collectPages(total,async page=>{
    const data=await portalPost(client,'getAppList',{currentPage:page,ywlbmArray:[]});
    if(!Array.isArray(data?.list)||data.list.some(g=>!Array.isArray(g.item)))throw fail('PORTAL_SCHEMA_CHANGED');
    return data.list.flatMap(g=>g.item);
  });
  const rowCount=paging.items.length;
    for(const item of paging.items) {
      const validId=/^[a-f0-9]{32}$/i.test(item.yyfwid||'');
      if(!validId)invalidIds++;
      let entry='';try{const u=new URL(decodeHtml(item.href));if(['https:','http:'].includes(u.protocol))entry=safeUrl(u.href);}catch{}
      const key=validId?item.yyfwid:crypto.createHash('sha256').update(String(item.yyfwmc)+String(item.href)).digest('hex').slice(0,24);
      apps.set(key,{id:(validId?'portal:':'directory:')+key,name:textOnly(item.yyfwmc||''),...(validId?{roaming_id:item.yyfwid}:{}),
        entry_url:entry,category:textOnly(item.ywlbmc||item.fwejlbmc_xs||''),department:textOnly(item.fzbmmc||''),
        description:redact(textOnly(item.yyfwjs||''),secrets).slice(0,700),source:'https://info.tsinghua.edu.cn/f/info/portal_fg/student/yyfwxxindex',
        capability:'DISCOVERED_NOT_BUSINESS_VERIFIED'});
    }
  const warnings=[...paging.warnings];
  if(rowCount!==apps.size)warnings.push('DUPLICATE_DIRECTORY_IDS');
  if(invalidIds)warnings.push('INVALID_ROAMING_IDS');
  return {status:'DIRECTORY_READ_VERIFIED',expected:total,rowCount,count:apps.size,duplicateCount:rowCount-apps.size,
    invalidRoamingIds:invalidIds,complete:paging.complete&&warnings.length===0,warnings,pages:paging.pages,missing_pages:paging.missing_pages,page_errors:paging.page_errors,
    checkedAt:new Date().toISOString(),services:[...apps.values()]};
}

async function modelToken(client,account,rejected){
 const modelOrigin='https://madmodel.cs.tsinghua.edu.cn';
      const tokenHash=t=>crypto.createHash('sha256').update(String(t?.token||'')).digest('hex');
      const valid=t=>t&&t.expiresAt>Date.now()&&activeSession.apps[modelOrigin]?.tokenHash===tokenHash(t);
      async function getToken(rejected){
        const token=credentials.readToken();
        if(!rejected&&valid(token))return token;
        return withAuthentication(async()=>{
          const current=credentials.readToken();
          if(valid(current)&&(!rejected||current.token!==rejected))return current;
          if(!account)throw fail('ACCOUNT_CREDENTIALS_UNAVAILABLE');
          const fresh=await client.getMadModelToken({...account,twoFactorHandler:()=>{throw fail('TWO_FACTOR_REQUIRED');}});
          credentials.writeToken(fresh.token,fresh.expiresAt,fresh.cookie);
          activeSession.apps[modelOrigin]={...(activeSession.apps[modelOrigin]||{}),tokenHash:tokenHash(fresh),authentication_state:'TOKEN_ISSUED',checkedAt:new Date().toISOString()};
          return fresh;
        });
      }

 return getToken(rejected);
}

async function main(command,control={}) {
  const originalFetch=globalThis.fetch;
  activeControl=control;
  globalThis.fetch=(url,options={})=>{
    validateUrl(String(url));control.signal?.throwIfAborted();
    const signal=control.signal?(options.signal?AbortSignal.any([options.signal,control.signal]):control.signal):options.signal;
    return originalFetch(url,{...options,signal});
  };
  try {
    if(command.command==='login'&&command.force)authGate.reset();
    const account=credentials.readAccount();
    const store=new SessionStore({dir:stateDir,account,dpapi:helper.dpapi,locks,atomicWrite,currentAccount:credentials.readAccount});
    const base=await store.load(control),jar=new CampusCookies(base.cookies),client=new auth.MadmodelAuthClient(jar);
    activeSession={store,base,jar,apps:structuredClone(base.apps)};
    let serviceOrigin=new URL(command.service_origin||command.landing_url||command.url||
      (['model','models'].includes(command.command)?'https://madmodel.cs.tsinghua.edu.cn/':command.command==='login'?'https://learn.tsinghua.edu.cn/':'https://info.tsinghua.edu.cn/')).origin;
    validateUrl(serviceOrigin);
    collectSecrets(jar,account);
    let result;
    if(command.command==='import_browser_state') {
      const data=JSON.parse(helper.dpapi('unprotect',Buffer.from(String(command.sealedState||''),'base64')));
      let bootstrap;
      try{bootstrap=JSON.parse(helper.dpapi('unprotect',Buffer.from(String(data.syncToken||''),'base64')));}catch{throw fail('BROWSER_SYNC_TOKEN_INVALID');}
      const synced=browserSync(jar,data,bootstrap,store.account,activeSession.apps);
      serviceOrigin=bootstrap.serviceOrigin;
      result={status:'BROWSER_SESSION_ENCRYPTED',importedCookies:data.cookies.length,
        syncToken:helper.dpapi('protect',Buffer.from(JSON.stringify({...synced,revision:activeSession.base.revision}))).toString('base64')};
    } else if(command.command==='action_identity') {
      result={account_id:store.account};
    } else if(command.command==='ensure_learn_session') {
      const origin='https://learn.tsinghua.edu.cn',signature=learningSignature();
      if(!signature||activeSession.apps[origin]?.providerSignature!==signature){
        if(!account)throw fail('ACCOUNT_CREDENTIALS_UNAVAILABLE');
        const token=await withAuthentication(()=>helper.learnSession(client,account,false));
        importLearning(jar,token);markLearning();
      }
      result={status:'LEARN_PROVIDER_BOUND_TO_ACCOUNT'};
    } else if(command.command==='login') {
      await ensureInfo(client,account);
      const token=await withAuthentication(()=>helper.learnSession(client,account,!!command.force));
      const imported=importLearning(jar,token);
      markLearning();
      result={status:'AUTHENTICATED',info:true,learn:imported.freshProviderAuthenticated,courseCount:imported.courseCount};
    } else if(command.command==='discover') result=await discover(client,account);
    else if(command.command==='download')result=await downloadFile(client,account,command.url,command.destination,command.max_bytes);
    else if(command.command==='models'||command.command==='model'&&process.env.THU_MODEL_READ!=='0') {
      if(process.env.THU_MODEL_READ==='0')throw fail('MODEL_EXTENSION_DISABLED');
      const service=require('./models'),config=up('./config');
      const directory=await service.catalog({cached:activeSession.apps[service.ORIGIN]?.modelCatalog,
        refresh:!!command.refresh,parseModelList:up('./core/model-discovery').parseModelList,signal:control.signal});
      if(directory.freshness!=='STALE')
        activeSession.apps[service.ORIGIN]={...(activeSession.apps[service.ORIGIN]||{}),modelCatalog:directory};
      if(command.command==='models')result=directory;
      else result=await service.generate({command,directory,configuredModel:config.model,
        getToken:async rejected=>{const t=await modelToken(client,account,rejected);collectSecrets(jar,account);return t;},
        createClient:()=>up('./core/upstream-client').createUpstreamClient({...config,
          upstream:auth.MADMODEL_VPN_PREFIX+'/v1/chat/completions',tunnelMode:true,
          upstreamHeaderTimeout:90000,streamIdleTimeout:90000,streamTotalTimeout:180000}),
        applyThinking:up('./core/thinking').applyThinking,signal:control.signal,progress:control.progress,
        redact:value=>redact(value,secrets)});
    }
    else if(command.command==='model') {
      const getToken=rejected=>modelToken(client,account,rejected);
      let t=await getToken(),response,output='',started=false,refreshed=false;
      const config=up('./config'),{createUpstreamClient}=up('./core/upstream-client');
      const signal=control.signal?AbortSignal.any([control.signal,AbortSignal.timeout(180000)]):AbortSignal.timeout(180000);
      for(let attempt=0;attempt<2;attempt++){
        collectSecrets(jar,account);
        response=await createUpstreamClient({...config,upstream:auth.MADMODEL_VPN_PREFIX+'/v1/chat/completions',tunnelMode:true,
          upstreamHeaderTimeout:90000,streamIdleTimeout:90000,streamTotalTimeout:180000}).request({token:t.token,cookie:t.cookie,
          signal,payload:{model:command.model||config.model,
            messages:[{role:'user',content:String(command.prompt||'')}],stream:true,
            max_tokens:Math.min(8192,Math.max(1,Number(command.max_tokens)||1024)),chat_template_kwargs:{thinking:false}},
          onOpen:()=>{started=true;},onChunk:o=>{started=true;output+=o?.choices?.[0]?.delta?.content||'';}});
        if(attempt===0&&shouldRetryModel(response,started)){t=await getToken(t.token);refreshed=true;continue;}
        break;
      }
      if(response.type==='completion')output=response.body?.choices?.[0]?.message?.content||'';
      if(response.type==='upstream-error'&&response.status===401)throw fail('MODEL_AUTH_REJECTED');
      if(response.type==='upstream-error'&&response.status===403)throw fail('MODEL_FORBIDDEN');
      if(!['stream','completion'].includes(response.type)||!output.trim())throw fail('MODEL_'+response.type.toUpperCase().replace(/-/g,'_'));
      result={status:'GENERATION_VERIFIED',model:command.model||config.model,content:redact(output,secrets),usage:response.usage||null,authentication_state:'AUTHENTICATED',refreshed};
    } else if(['read','browser_state'].includes(command.command)) {
      let url=command.url;
      if(command.roaming_id) {
        if(command.authenticate===false)throw fail('ROAMING_REQUIRES_AUTHENTICATION');
        if(!/^[a-f0-9]{32}$/i.test(command.roaming_id))throw fail('INVALID_SERVICE_ID');
        await ensureInfo(client,account);
        url=await withAuthentication(()=>client.resolveRoamingTarget(command.roaming_id,'campus-service',{...account,twoFactorHandler:()=>{throw fail('TWO_FACTOR_REQUIRED');}}));
        if(new URL(url).protocol==='http:') {
          const parsed=new URL(url);
          if(!parsed.hostname.endsWith('.tsinghua.edu.cn')||parsed.username||parsed.password)throw fail('NON_TSINGHUA_ROAMING_URL');
          url=client.toLbRedirectUrl(url);
        }
      }
      validateUrl(url);
      if(command.via_webvpn) {if(command.authenticate!==false)await ensureInfo(client,account);url=toTunnel(url);}
      const readClient=command.authenticate===false?new auth.MadmodelAuthClient(new CampusCookies()):client;
      let response=await read(readClient,url,account,command.authenticate!==false);
      let state=authenticationState(response,serviceOrigin);
      if(command.authenticate!==false&&['EXPIRED','LOGIN_REQUIRED'].includes(state.authentication_state)&&!activeControl.identityAttempted){
        activeControl.recoveryAttempted=true;
        if(serviceOrigin==='https://learn.tsinghua.edu.cn'){
          if(!account)throw fail('ACCOUNT_CREDENTIALS_UNAVAILABLE');
          const token=await withAuthentication(()=>helper.learnSession(client,account,true));
          importLearning(jar,token);
          markLearning();
        }else if(command.via_webvpn||command.roaming_id){
          await ensureInfo(client,account);
        }else{
          // Only follow this app's own official entry; never invent a login API.
          await read(client,serviceOrigin+'/',account,true);
        }
        response=await read(client,url,account,false);
      }
      // A SPA callback may still need its ticket in JS. Do not discard that
      // callback by replacing it with the service's ordinary landing URL.
      if(command.landing_url && !['BROWSER_REQUIRED','AUTH_REQUIRED'].includes(pageResult(response).status)) {
        const landing=assertReadUrl(command.landing_url);
        if(safeUrl(response.finalUrl)!==safeUrl(landing.href))response=await read(readClient,landing.href,account,command.authenticate!==false);
      }
      result=pageResult(response,command);
      state=authenticationState(response,serviceOrigin);
      Object.assign(result,state,{checked_at:new Date().toISOString(),reason:state.reason,public_read:command.authenticate===false});
      if(state.authentication_state==='AUTHENTICATED')activeSession.apps[serviceOrigin]={...(activeSession.apps[serviceOrigin]||{}),checkedAt:result.checked_at,authentication_state:state.authentication_state};
      if(command.command==='browser_state') {
        result={status:result.status,sealedState:sealBrowser(jar,account,serviceOrigin,response.finalUrl,activeSession.apps,activeSession.base.revision)};
      }
    } else throw fail('UNKNOWN_COMMAND');
    try {
      control.signal?.throwIfAborted();
      await store.commit(activeSession.base,jar,activeSession.apps,control);
    } catch(error) {
      if(command.command==='model'&&result?.model)error.modelEvidence={...result,automatic_replay:false};
      throw error;
    }
    return result;
  } finally {globalThis.fetch=originalFetch;activeControl={};activeSession=null;secrets=[];}
}

if(require.main===module && process.argv.includes('--stdio')) {
  const queue=new SerialQueue(main,message=>process.stdout.write(JSON.stringify(message)+'\n'));
  let buffer='';process.stdin.setEncoding('utf8');
  process.stdin.on('data',part=>{
    buffer+=part;
    for(;;){
      const end=buffer.indexOf('\n');if(end<0)break;
      if(end>2*1024*1024){queue.close();process.exitCode=1;process.stdin.destroy();return;}
      const line=buffer.slice(0,end);buffer=buffer.slice(end+1);
      try{queue.accept(JSON.parse(line));}catch{/* Never echo malformed input. */}
    }
    if(buffer.length>2*1024*1024){queue.close();process.exitCode=1;process.stdin.destroy();}
  });
  process.stdin.on('end',()=>queue.close());
  process.stdin.on('error',()=>queue.close());
  process.stdout.on('error',()=>{queue.close();process.exitCode=1;process.stdin.destroy();});
} else if(require.main===module) {
  let raw='';process.stdin.setEncoding('utf8');
  process.stdin.on('data',part=>{raw+=part;if(raw.length>1024*1024){process.exitCode=1;process.stdin.destroy();}});
  process.stdin.on('end',()=>Promise.resolve().then(()=>main(JSON.parse(raw))).then(result=>{
    process.stdout.write(JSON.stringify({ok:true,result})+'\n');
  }).catch(error=>{const code=typeof error.code==='string'&&/^[A-Z_]+$/.test(error.code)?error.code:
    error.name==='TimeoutError'?'NETWORK_TIMEOUT':'CAMPUS_ACCESS_FAILED';
    process.stdout.write(JSON.stringify({ok:false,error:{code}})+'\n');process.exitCode=1;}));
}
module.exports={pageResult,downloadFile,main};
