'use strict';
const {CookieJar,Cookie,domainMatch}=require('tough-cookie');
const {validateUrl,toTunnel}=require('./security');
const fail=code=>Object.assign(new Error(code),{code});
const key=c=>[c.domain,c.path,c.key||c.name].join('\t');
const school=host=>host==='tsinghua.edu.cn'||host.endsWith('.tsinghua.edu.cn');
const AUTH_ORIGINS=['https://id.tsinghua.edu.cn','https://idp.tsinghua.edu.cn','https://oauth.tsinghua.edu.cn','https://webvpn.tsinghua.edu.cn'];
function allowedUrls(service,finalUrl=service){
  validateUrl(service);validateUrl(finalUrl);
  return [...new Set([service,finalUrl,...AUTH_ORIGINS,toTunnel(service),toTunnel('https://info.tsinghua.edu.cn/')])];
}
function inScope(c,urls){
  return urls.some(raw=>{
    const u=new URL(raw);
    if(c.hostOnly?c.domain!==u.hostname:!domainMatch(u.hostname,c.domain))return false;
    // WebVPN multiplexes apps under one host. Do not inject other apps' path cookies.
    if(c.domain==='webvpn.tsinghua.edu.cn'&&c.path.startsWith('/https/')){
      const prefix=c.path.split('/').slice(0,3).join('/');
      return u.pathname===prefix||u.pathname.startsWith(prefix+'/');
    }
    return true;
  });
}
class CampusCookies {
  constructor(records=[]){this.replace(records);}
  replace(records){
    this.jar=new CookieJar(undefined,{rejectPublicSuffixes:true,allowSpecialUseDomain:false});this.attributes=new Map();
    for(const record of records){
      if(!record||!school(String(record.domain))||typeof record.hostOnly!=='boolean')throw fail('STATE_COOKIE_INVALID');
      const c=Cookie.fromJSON(record);if(!c||!c.key||!c.path?.startsWith('/'))throw fail('STATE_COOKIE_INVALID');
      // Max-Age is normalized to an absolute expiry at receipt, never extended by reads.
      delete c.maxAge;
      this.jar.store.putCookie(c,()=>{});
      this.attributes.set(key(c),{sourceMaxAge:record.sourceMaxAge,sourceExpires:record.sourceExpires});
    }
  }
  export(){
    const now=Date.now();
    return (this.jar.serializeSync().cookies||[]).filter(c=>c.expires==='Infinity'||!c.expires||Date.parse(c.expires)>now).map(c=>{
      const value={...c,...this.attributes.get(key(c))};delete value.lastAccessed;return value;
    });
  }
  absorb(url,input){
    validateUrl(url);
    for(const combined of (Array.isArray(input)?input:[input])){
      if(!combined)continue;
      for(const raw of String(combined).split(/,(?=\s*[^;,=\s]+=[^;]*)/)){
        const c=Cookie.parse(raw);if(!c)continue;
        const received=new Date(),max=c.maxAge,expires=c.expires;
        if(max!=null){c.expires=max==='Infinity'?'Infinity':new Date(max<=0?0:received.getTime()+Number(max)*1000);delete c.maxAge;}
        try{
          const stored=this.jar.setCookieSync(c,url,{now:received,ignoreError:false});
          if(stored)this.attributes.set(key(stored),{sourceMaxAge:max,sourceExpires:expires instanceof Date?expires.toISOString():expires});
        }catch{/* Invalid server Cookie scope is rejected, not widened. */}
      }
    }
  }
  headerFor(url){validateUrl(url);return this.jar.getCookieStringSync(url);}
  valueFor(url,name){validateUrl(url);return this.jar.getCookiesSync(url).find(c=>c.key===name)?.value||'';}
  clearOrigins(urls){
    const hosts=urls.map(u=>validateUrl(u).hostname);
    this.replace(this.export().filter(c=>!hosts.includes(c.domain)));
  }
  get cookies(){
    const result={};
    for(const c of this.export())(result['https://'+c.domain]||=[]).push({name:c.key,value:c.value,path:c.path});
    return result; // Legacy read-only compatibility; never used to store attributes.
  }
  browserCookies(urls){
    return this.export().filter(c=>inScope(c,urls)).map(c=>({name:c.key,value:c.value,
      domain:(c.hostOnly?'':'.')+c.domain,path:c.path,expires:c.expires&&c.expires!=='Infinity'?Date.parse(c.expires)/1000:-1,
      secure:!!c.secure,httpOnly:!!c.httpOnly,...(c.sameSite?{sameSite:{strict:'Strict',lax:'Lax',none:'None'}[c.sameSite]}:{})}));
  }
  setBrowser(c,urls){
    const domain=String(c.domain||'').replace(/^\./,'');
    const hostOnly=!String(c.domain||'').startsWith('.');
    if(!school(domain)||!inScope({domain,path:c.path||'/',hostOnly},urls))throw fail('BROWSER_COOKIE_SCOPE');
    if(typeof c.name!=='string'||typeof c.value!=='string'||/[\r\n;]/.test(c.value)||!/^[!#$%&'*+.^_`|~\w-]+$/.test(c.name))throw fail('BROWSER_COOKIE_INVALID');
    const options={key:c.name,value:c.value,domain,path:c.path||'/',hostOnly,secure:!!c.secure,httpOnly:!!c.httpOnly,
      expires:c.expires>0?new Date(c.expires*1000):c.expires===0?new Date(0):'Infinity',creation:new Date(),
      ...(c.sameSite?{sameSite:String(c.sameSite).toLowerCase()}:{})};
    const record=new Cookie(options).toJSON();
    this.replace([...this.export().filter(x=>key(x)!==key(record)),record]);
  }
  remove(record){this.replace(this.export().filter(c=>key(c)!==key(record)));}
}
module.exports={CampusCookies,key,allowedUrls,inScope,AUTH_ORIGINS};
