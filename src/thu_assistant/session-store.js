'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {CampusCookies,key}=require('./cookies');
const {withPidLock}=require('./scheduler');
const fail=code=>Object.assign(new Error(code),{code});
const identity=account=>crypto.createHash('sha256').update(String(account?.username||'anonymous')).digest('hex');
function canonical(value){
  if(Array.isArray(value))return value.map(canonical);
  if(value&&typeof value==='object')return Object.fromEntries(Object.keys(value).sort().filter(k=>value[k]!==undefined&&!(value.domain&&value.key&&['creation','lastAccessed'].includes(k))).map(k=>[k,canonical(value[k])]));
  return value;
}
const equal=(a,b)=>JSON.stringify(canonical(a))===JSON.stringify(canonical(b));
function mergeMap(base,local,remote){
  const out={...remote};
  for(const k of new Set([...Object.keys(base),...Object.keys(local)])){
    if(equal(base[k],local[k]))continue;
    if(!equal(remote[k],base[k])&&!equal(remote[k],local[k]))throw fail('STATE_CONFLICT');
    if(local[k]===undefined)delete out[k];else out[k]=local[k];
  }
  return out;
}
const cookieMap=records=>Object.fromEntries(records.map(c=>[key(c),c]));
class SessionStore {
  constructor({dir,account,dpapi,locks,atomicWrite,currentAccount}){
    this.dir=dir;this.account=identity(account);this.dpapi=dpapi;this.locks=locks;this.atomicWrite=atomicWrite;
    this.currentAccount=currentAccount;
    this.file=path.join(dir,'campus-v2.dpapi');this.lock=path.join(dir,'campus-state-v2.lock');
  }
  read(){
    if(this.currentAccount&&identity(this.currentAccount())!==this.account)throw fail('STATE_ACCOUNT_MISMATCH');
    let encoded;
    try{encoded=fs.readFileSync(this.file,'utf8');}catch(e){if(e.code!=='ENOENT')throw fail('STATE_READ_FAILED');}
    if(encoded===undefined)return {version:2,account:this.account,revision:0,cookies:[],apps:{},
      migration:{legacyPresent:fs.existsSync(path.join(this.dir,'campus.dpapi')),policy:'REBUILD_ON_DEMAND'}};
    if(!/^[a-zA-Z0-9+/]+={0,2}\s*$/.test(encoded)||encoded.trim().length%4)throw fail('STATE_CORRUPT');
    let plain;
    try{plain=this.dpapi('unprotect',Buffer.from(encoded.trim(),'base64'));}catch{throw fail('STATE_DECRYPT_FAILED');}
    let state;try{state=JSON.parse(plain);}catch{throw fail('STATE_CORRUPT');}
    if(state.version!==2)throw fail('STATE_VERSION_UNSUPPORTED');
    if(state.account!==this.account)throw fail('STATE_ACCOUNT_MISMATCH');
    if(!Number.isSafeInteger(state.revision)||state.revision<0||!Array.isArray(state.cookies)||!state.apps||typeof state.apps!=='object'||Array.isArray(state.apps))throw fail('STATE_CORRUPT');
    new CampusCookies(state.cookies);
    return state;
  }
  async load(control={}){return withPidLock(this.locks,this.lock,()=>this.read(),{...control,busyCode:'STATE_BUSY'});}
  async commit(base,jar,apps,control={}){
    return withPidLock(this.locks,this.lock,()=>{
      const latest=this.read();
      if(latest.revision<base.revision)throw fail('STATE_REVISION_ROLLBACK');
      const cookies=Object.values(mergeMap(cookieMap(base.cookies),cookieMap(jar.export()),cookieMap(latest.cookies)));
      const mergedApps=mergeMap(base.apps,apps,latest.apps);
      const next={...latest,cookies,apps:mergedApps,revision:latest.revision+1,updatedAt:new Date().toISOString()};
      let protectedData;
      try{protectedData=this.dpapi('protect',Buffer.from(JSON.stringify(next)));}catch{throw fail('STATE_ENCRYPT_FAILED');}
      if(!protectedData?.length)throw fail('STATE_ENCRYPT_FAILED');
      // Ciphertext-only rollback copies stay beside the protected originals.
      // EXCL preserves the first migration checkpoint across concurrent processes.
      for(const name of ['campus.dpapi','browser.dpapi']){
        try{fs.copyFileSync(path.join(this.dir,name),path.join(this.dir,name+'.before-v2'),fs.constants.COPYFILE_EXCL);}
        catch(e){if(!['ENOENT','EEXIST'].includes(e.code))throw fail('STATE_BACKUP_FAILED');}
      }
      this.atomicWrite(this.file,protectedData.toString('base64'));
      return next;
    },{...control,busyCode:'STATE_BUSY'});
  }
  async rebase(base,jar,apps,control={}){
    const latest=await this.load(control);
    const cookies=Object.values(mergeMap(cookieMap(base.cookies),cookieMap(jar.export()),cookieMap(latest.cookies)));
    const mergedApps=mergeMap(base.apps,apps,latest.apps);
    jar.replace(cookies);
    return {base:latest,apps:mergedApps};
  }
}
module.exports={SessionStore,identity,mergeMap,cookieMap,equal};
