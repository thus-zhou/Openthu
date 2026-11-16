'use strict';
const {equal}=require('./session-store');
const {key,allowedUrls,inScope}=require('./cookies');
const fail=code=>Object.assign(new Error(code),{code});
function loginRequired(response){
  const body=String(response.body||''),url=new URL(response.finalUrl);
  if(['id.tsinghua.edu.cn','idp.tsinghua.edu.cn'].includes(url.hostname)||/login_timeout/i.test(url.pathname))return true;
  if(/sm2publicKey|<input[^>]+type=["']password/i.test(body))return true;
  if(url.hostname==='learn.tsinghua.edu.cn'){
    const visible=body.replace(/<(script|style|template)\b[^>]*>[\s\S]*?<\/\1>/gi,'').replace(/<[^>]*>/g,'').replace(/\s+/g,'').replace(/退出登录|退出登陆/g,'');
    return /未登录|请先登录|请重新登录|登录超时|登录已超时|登录失效|登录已失效|会话已过期|会话失效/.test(visible)||/^\/f\/login\/?$/.test(url.pathname);
  }
  return false;
}
function authenticationState(response,service){
  const body=String(response.body||''),u=new URL(response.finalUrl);
  if(response.statusCode===403)return {authentication_state:'FORBIDDEN',reason:'HTTP_403'};
  if(response.statusCode===401)return {authentication_state:'EXPIRED',reason:'HTTP_401'};
  if(loginRequired(response))return {authentication_state:'LOGIN_REQUIRED',reason:'OFFICIAL_LOGIN_PAGE'};
  if(service==='https://info.tsinghua.edu.cn'&&/我的工作台/.test(body)&&/退出/.test(body))return {authentication_state:'AUTHENTICATED',reason:'PERSONAL_WORKBENCH'};
  if(service==='https://learn.tsinghua.edu.cn'&&u.hostname==='learn.tsinghua.edu.cn'&&/退出登录|注销/.test(body))return {authentication_state:'AUTHENTICATED',reason:'LEARN_LOGOUT_MARKER'};
  return {authentication_state:'UNKNOWN',reason:response.statusCode>=200&&response.statusCode<300?'READABLE_NOT_AUTH_PROOF':'HTTP_ERROR'};
}
function shouldRetryModel(response,started){return !started&&response?.type==='upstream-error'&&response.status===401;}
function browserSync(jar,data,bootstrap,account,apps){
  if(bootstrap.version!==2||!Number.isSafeInteger(bootstrap.revision))throw fail('INVALID_BROWSER_STATE');
  if(bootstrap.account!==account)throw fail('STATE_ACCOUNT_MISMATCH');
  if(!Array.isArray(data.cookies)||data.cookies.length>500||!Array.isArray(bootstrap.cookieRecords)||!Array.isArray(bootstrap.cookies)||!Array.isArray(data.origins||[]))throw fail('INVALID_BROWSER_STATE');
  const urls=allowedUrls(bootstrap.serviceOrigin,bootstrap.url);
  const cookies=data.cookies.filter(c=>{
    const domain=String(c?.domain||'').replace(/^\./,'');
    const hostOnly=!String(c?.domain||'').startsWith('.');
    return domain && inScope({domain,path:c?.path||'/',hostOnly},urls);
  });
  const old=new Map(bootstrap.cookies.map(c=>[key({...c,domain:c.domain.replace(/^\./,'')}),c]));
  const current=new Map(cookies.map(c=>[key({...c,domain:String(c.domain).replace(/^\./,'')}),c]));
  const baseline=new Map(bootstrap.cookieRecords.map(c=>[key(c),c]));
  const latest=new Map(jar.export().map(c=>[key(c),c]));
  for(const k of new Set([...old.keys(),...current.keys()])){
    const before=old.get(k),after=current.get(k);
    // Playwright reports an omitted SameSite as Lax. Preserve the original when
    // all attributes actually exposed by the browser are unchanged.
    const comparable=c=>c?{...c,sameSite:c.sameSite||'Lax'}:c;
    if(equal(comparable(before),comparable(after)))continue;
    if(!after&&!latest.has(k))continue; // Already expired/deleted on the HTTP side.
    if(!equal(baseline.get(k),latest.get(k)))throw fail('STATE_CONFLICT');
    if(after)jar.setBrowser(after,urls);
    else if(baseline.has(k))jar.remove(baseline.get(k));
  }
  const origins=(data.origins||[]).filter(o=>{
    if(!Array.isArray(o.localStorage))throw fail('INVALID_BROWSER_STATE');
    return o.origin===bootstrap.serviceOrigin||(new URL(bootstrap.url).hostname==='webvpn.tsinghua.edu.cn'&&o.origin==='https://webvpn.tsinghua.edu.cn');
  });
  const previous=bootstrap.appOrigins||[],remote=apps[bootstrap.serviceOrigin]?.origins||[];
  if(!equal(origins,previous)&&!equal(previous,remote)&&!equal(origins,remote))throw fail('STATE_CONFLICT');
  apps[bootstrap.serviceOrigin]={...(apps[bootstrap.serviceOrigin]||{}),origins:equal(origins,previous)?remote:origins};
  // The next comparison must describe what this browser actually saw, not
  // unseen cookies/localStorage concurrently written by another HTTP client.
  const saved=new Map(jar.export().map(c=>[key(c),c]));
  const records=[...current].map(([k,after])=>{
    const comparable=c=>c?{...c,sameSite:c.sameSite||'Lax'}:c;
    return equal(comparable(old.get(k)),comparable(after))?baseline.get(k):saved.get(k);
  }).filter(Boolean);
  return {...bootstrap,cookies,cookieRecords:records,origins,appOrigins:origins};
}
module.exports={authenticationState,shouldRetryModel,browserSync,loginRequired};
