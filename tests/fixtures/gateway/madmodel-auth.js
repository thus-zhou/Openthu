const fs=require('node:fs'),path=require('node:path');
const dir=process.env.MADMODEL_STATE_DIR;
const counter=name=>{const file=path.join(dir,name);let n=0;try{n=Number(fs.readFileSync(file));}catch{}fs.writeFileSync(file,String(n+1));};
class CookieJar {
 constructor(){this.cookies={};}
 headerFor(url){return (this.cookies[new URL(url).origin]||[]).map(c=>c.name+'='+c.value).join('; ');}
 absorb(url,value){this.cookies[new URL(url).origin]=[{name:'fixture',value:String(value),path:'/'}];}
}
class MadmodelAuthClient {
 constructor(jar){this.jar=jar;}
 async ensureWebVpnSession(user,password,fp,twoFactor){
  if(!fs.existsSync(path.join(dir,'auth.lock')))throw Object.assign(new Error(),{code:'AUTH_LOCK_MISSING'});
  if(!this.jar.cookies['https://info.tsinghua.edu.cn']){
   counter('identity-count');
   if(process.env.THU_TEST_BAD_PASSWORD==='1')throw Object.assign(new Error(),{code:'BAD_CREDENTIALS'});
   if(process.env.THU_TEST_MFA==='1')twoFactor();
   this.jar.absorb('https://info.tsinghua.edu.cn/','fixture=session; Path=/; Secure');
  }
  this.portalCsrf='synthetic-csrf';
 }
 async getMadModelToken(){
  if(!fs.existsSync(path.join(dir,'auth.lock')))throw Object.assign(new Error(),{code:'AUTH_LOCK_MISSING'});
  counter('refresh-count');return {token:'synthetic-token',expiresAt:Date.now()+3600000,cookie:'synthetic-cookie'};
 }
}
module.exports={CookieJar,MadmodelAuthClient,MADMODEL_VPN_PREFIX:'https://webvpn.tsinghua.edu.cn/model',
 requestWithRedirects:async({url},jar)=>{
  if(new URL(url).pathname==='/public'&&jar.headerFor(url))throw Object.assign(new Error(),{code:'PUBLIC_READ_SENT_COOKIE'});
  jar.absorb(new URL(url).origin,'fixture=fixture-cookie; Path=/; Secure');
  return {finalUrl:url,statusCode:200,body:'<title>Offline fixture</title><body>'+('fixture page '.repeat(20))+'</body>'};
 }};
