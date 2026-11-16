'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {CampusCookies,key}=require('./cookies');
const {withPidLock}=require('./scheduler');
const {looksSecureEnvelope}=require('./crypto-bridge');
const fail=code=>Object.assign(new Error(code),{code});
const identity=account=>crypto.createHash('sha256').update(String(account?.username||'anonymous')).digest('hex');
const SECURE_RECORD_ID='campus-session';
const BROWSER_RECORD_ID='browser-session';
const SECURE_SCOPE={purpose:'SESSION',accountDomain:'CAMPUS',serviceDomain:'LOCAL'};
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
function secureReadError(error){
  const code=typeof error?.code==='string'?error.code:'';
  if(['SECURE_ENVELOPE_CORRUPT','SECURE_RECORD_MISMATCH','SECURE_INVALID_RECORD_ID'].includes(code))return fail('STATE_CORRUPT');
  if(['SECURE_DECRYPT_FAILED','SECURE_ACCOUNT_MISMATCH','SECURE_PURPOSE_MISMATCH','SECURE_SERVICE_MISMATCH','SECURE_KEY_MISMATCH','SECURE_PAYLOAD_INVALID'].includes(code))return fail('STATE_DECRYPT_FAILED');
  if(code.startsWith('SECURE_KEY_')||code==='SECURE_KEY_NOT_CONFIGURED'||code==='SECURE_CRYPTO_UNAVAILABLE')return fail(code);
  return fail(code||'STATE_DECRYPT_FAILED');
}
class SessionStore {
  constructor({dir,account,dpapi,crypto:secureCrypto,legacyDpapi,platform,allowLegacy,locks,atomicWrite,currentAccount}){
    this.dir=dir;this.account=identity(account);this.accountName=String(account?.username||'anonymous');
    this.dpapi=dpapi||null;this.legacyDpapi=legacyDpapi||dpapi||null;this.crypto=secureCrypto||null;
    this.platform=platform||process.platform;
    this.allowLegacy=allowLegacy===undefined?Boolean(dpapi):Boolean(allowLegacy);
    this.locks=locks;this.atomicWrite=atomicWrite;
    this.currentAccount=currentAccount;
    this.file=path.join(dir,'campus-v2.dpapi');this.secureFile=path.join(dir,'campus-v3.secure');
    this.lock=path.join(dir,'campus-state-v2.lock');
  }
  get backend(){
    if(this.crypto)return 'secure-storage-header-v1';
    if(this.dpapi&&(this.allowLegacy||this.platform==='win32'))return 'legacy-dpapi';
    if(this.legacyDpapi&&this.platform==='win32')return 'legacy-dpapi';
    return null;
  }
  _scope(recordId,revision){
    return {...SECURE_SCOPE,account:this.accountName,
      ...(Number.isSafeInteger(revision)&&revision>=0?{revision}:{}),recordId};
  }
  _empty(){
    return {version:this.crypto?3:2,account:this.account,revision:0,cookies:[],apps:{},
      migration:{legacyPresent:fs.existsSync(this.file)||fs.existsSync(path.join(this.dir,'campus.dpapi')),
        policy:'REBUILD_ON_DEMAND',target:this.crypto?'SECURE_V3':'LEGACY_DPAPI'}};
  }
  _validate(state){
    if(!state||typeof state!=='object')throw fail('STATE_CORRUPT');
    if(![2,3].includes(state.version))throw fail('STATE_VERSION_UNSUPPORTED');
    if(state.account!==this.account)throw fail('STATE_ACCOUNT_MISMATCH');
    if(!Number.isSafeInteger(state.revision)||state.revision<0||!Array.isArray(state.cookies)||
       !state.apps||typeof state.apps!=='object'||Array.isArray(state.apps))throw fail('STATE_CORRUPT');
    new CampusCookies(state.cookies);
    return state;
  }
  async _loadSecure(){
    let encoded;
    try{encoded=fs.readFileSync(this.secureFile);}catch(e){if(e.code==='ENOENT')return null;throw fail('STATE_READ_FAILED');}
    if(encoded.length>128*1024*1024+8192)throw fail('STATE_CORRUPT');
    if(!this.crypto)throw fail('SECURE_KEY_NOT_CONFIGURED');
    let opened;
    try{opened=await this.crypto.unseal(encoded,this._scope(SECURE_RECORD_ID,undefined));}
    catch(error){throw secureReadError(error);}
    let state;
    try{state=JSON.parse(opened.plaintext.toString('utf8'));}catch{throw fail('STATE_CORRUPT');}
    if(state.version!==3)throw fail('STATE_VERSION_UNSUPPORTED');
    const scope=opened.context||{};
    if(scope.purpose!=='SESSION'||scope.account_domain!=='CAMPUS'||scope.service_domain!=='LOCAL')throw fail('STATE_CORRUPT');
    if(scope.record_id&&scope.record_id!==SECURE_RECORD_ID)throw fail('STATE_CORRUPT');
    if(!Number.isSafeInteger(scope.revision)||scope.revision!==state.revision)throw fail('STATE_CORRUPT');
    this._validate(state);
    return state;
  }
  _loadLegacy(){
    let encoded;
    try{encoded=fs.readFileSync(this.file,'utf8');}catch(e){if(e.code!=='ENOENT')throw fail('STATE_READ_FAILED');}
    if(encoded===undefined)return null;
    if(!/^[a-zA-Z0-9+/]+={0,2}\s*$/.test(encoded)||encoded.trim().length%4)throw fail('STATE_CORRUPT');
    const legacy=this.dpapi||this.legacyDpapi;
    if(!legacy)throw fail('SECURE_LEGACY_UNAVAILABLE');
    let plain;
    try{plain=legacy('unprotect',Buffer.from(encoded.trim(),'base64'));}
    catch{throw fail(this.dpapi||this.platform==='win32'?'STATE_DECRYPT_FAILED':'SECURE_LEGACY_UNAVAILABLE');}
    let state;
    try{state=JSON.parse(plain);}catch{throw fail('STATE_CORRUPT');}
    if(state.version!==2)throw fail('STATE_VERSION_UNSUPPORTED');
    this._validate(state);
    state.migration={...(state.migration||{}),source:'LEGACY_DPAPI',target:this.crypto?'SECURE_V3':'LEGACY_DPAPI',
      legacyPresent:true,legacyReadable:true,saved:false};
    return state;
  }
  async read(){
    if(this.currentAccount&&identity(this.currentAccount())!==this.account)throw fail('STATE_ACCOUNT_MISMATCH');
    const secure=await this._loadSecure();
    if(secure)return secure;
    const legacy=this._loadLegacy();
    if(legacy)return legacy;
    return this._empty();
  }
  async load(control={}){
    return withPidLock(this.locks,this.lock,async()=>{
      if(control.required!==false&&!this.crypto&&!this.dpapi&&this.platform!=='win32')
        throw fail('SECURE_KEY_NOT_CONFIGURED');
      if(control.required!==false&&this.crypto&&!fs.existsSync(this.secureFile)&&!fs.existsSync(this.file))
        await this.crypto.health().catch(error=>{throw secureReadError(error);});
      if(control.required===false){
        try{return await this.read();}
        catch(error){
          if(['SECURE_KEY_NOT_CONFIGURED','SECURE_LEGACY_UNAVAILABLE','SECURE_KEY_UNREADABLE','SECURE_KEY_UNAVAILABLE'].includes(error.code))
            return this._empty();
          throw error;
        }
      }
      return this.read();
    },{...control,busyCode:'STATE_BUSY'});
  }
  _atomicWriteBinary(file,data){
    fs.mkdirSync(path.dirname(file),{recursive:true,mode:0o700});
    const temporary=file+'.'+crypto.randomUUID()+'.tmp';
    try{
      fs.writeFileSync(temporary,data,{mode:0o600});
      // Windows FlushFileBuffers needs a writable handle; 'r+' also works on
      // POSIX and keeps the same fsync guarantee.
      const fd=fs.openSync(temporary,'r+');
      try{fs.fsyncSync(fd);}finally{fs.closeSync(fd);}
      fs.renameSync(temporary,file);
    }catch(error){
      try{fs.unlinkSync(temporary);}catch{}
      throw fail('STATE_WRITE_FAILED');
    }
  }
  _backup(name){
    try{fs.copyFileSync(path.join(this.dir,name),path.join(this.dir,name+'.before-v3'),fs.constants.COPYFILE_EXCL);}
    catch(e){if(!['ENOENT','EEXIST'].includes(e.code))throw fail('STATE_BACKUP_FAILED');}
  }
  async _commitSecure(next){
    const payload=JSON.stringify({...next,version:3,
      migration:{...(next.migration||{}),source:next.migration?.source||'NONE',target:'SECURE_V3',
        saved:true,verified:true,legacyRetained:fs.existsSync(this.file)}});
    let sealed;
    try{sealed=await this.crypto.seal(Buffer.from(payload),this._scope(SECURE_RECORD_ID,next.revision));}
    catch(error){throw secureReadError(error);}
    let verified;
    try{verified=await this.crypto.unseal(sealed.envelope,{...this._scope(SECURE_RECORD_ID,next.revision),expectedRevision:next.revision});}
    catch(error){throw secureReadError(error);}
    if(verified.plaintext.toString('utf8')!==payload)throw fail('STATE_ENCRYPT_FAILED');
    for(const name of ['campus.dpapi','campus-v2.dpapi','browser.dpapi'])this._backup(name);
    this._atomicWriteBinary(this.secureFile,sealed.envelope);
    return JSON.parse(payload);
  }
  _commitLegacy(next){
    const legacy=this.dpapi||this.legacyDpapi;
    if(!legacy||(!this.dpapi&&this.platform!=='win32'&&!this.allowLegacy))throw fail('SECURE_KEY_NOT_CONFIGURED');
    let protectedData;
    try{protectedData=legacy('protect',Buffer.from(JSON.stringify(next)));}catch{throw fail('STATE_ENCRYPT_FAILED');}
    if(!protectedData?.length)throw fail('STATE_ENCRYPT_FAILED');
    // Ciphertext-only rollback copies stay beside the protected originals.
    // EXCL preserves the first migration checkpoint across concurrent processes.
    for(const name of ['campus.dpapi','browser.dpapi']){
      try{fs.copyFileSync(path.join(this.dir,name),path.join(this.dir,name+'.before-v2'),fs.constants.COPYFILE_EXCL);}
      catch(e){if(!['ENOENT','EEXIST'].includes(e.code))throw fail('STATE_BACKUP_FAILED');}
    }
    this.atomicWrite(this.file,protectedData.toString('base64'));
    return next;
  }
  async commit(base,jar,apps,control={}){
    return withPidLock(this.locks,this.lock,async()=>{
      const latest=await this.read();
      if(latest.revision<base.revision)throw fail('STATE_REVISION_ROLLBACK');
      const cookies=Object.values(mergeMap(cookieMap(base.cookies),cookieMap(jar.export()),cookieMap(latest.cookies)));
      const mergedApps=mergeMap(base.apps,apps,latest.apps);
      const next={...latest,cookies,apps:mergedApps,revision:latest.revision+1,updatedAt:new Date().toISOString()};
      if(this.crypto)return this._commitSecure(next);
      return this._commitLegacy(next);
    },{...control,busyCode:'STATE_BUSY'});
  }
  async rebase(base,jar,apps,control={}){
    const latest=await this.load(control);
    const cookies=Object.values(mergeMap(cookieMap(base.cookies),cookieMap(jar.export()),cookieMap(latest.cookies)));
    const mergedApps=mergeMap(base.apps,apps,latest.apps);
    jar.replace(cookies);
    return {base:latest,apps:mergedApps};
  }
  async sealState(value){
    const payload=Buffer.from(JSON.stringify(value));
    if(this.crypto){
      const sealed=await this.crypto.seal(payload,this._scope(BROWSER_RECORD_ID,Number(value.revision)||0));
      return sealed.envelope;
    }
    const legacy=this.dpapi||this.legacyDpapi;
    if(!legacy||(!this.dpapi&&this.platform!=='win32'&&!this.allowLegacy))throw fail('SECURE_KEY_NOT_CONFIGURED');
    const protectedData=legacy('protect',payload);
    if(!protectedData?.length)throw fail('STATE_ENCRYPT_FAILED');
    return protectedData;
  }
  async unsealState(data){
    const buffer=Buffer.isBuffer(data)?data:Buffer.from(data);
    if(looksSecureEnvelope(buffer)){
      if(!this.crypto)throw fail('SECURE_KEY_NOT_CONFIGURED');
      let opened;
      try{opened=await this.crypto.unseal(buffer,this._scope(BROWSER_RECORD_ID,undefined));}
      catch(error){throw secureReadError(error);}
      let value;
      try{value=JSON.parse(opened.plaintext.toString('utf8'));}catch{throw fail('STATE_CORRUPT');}
      if(opened.context?.record_id&&opened.context.record_id!==BROWSER_RECORD_ID)throw fail('STATE_CORRUPT');
      if(Number.isSafeInteger(value.revision)&&Number.isSafeInteger(opened.context?.revision)&&
         value.revision!==opened.context.revision)throw fail('STATE_CORRUPT');
      return value;
    }
    const legacy=this.dpapi||this.legacyDpapi;
    if(!legacy||(!this.dpapi&&this.platform!=='win32'&&!this.allowLegacy))
      throw fail(this.platform==='win32'?'STATE_DECRYPT_FAILED':'SECURE_LEGACY_UNAVAILABLE');
    try{return JSON.parse(legacy('unprotect',buffer));}catch{throw fail('STATE_DECRYPT_FAILED');}
  }
}
module.exports={SessionStore,identity,mergeMap,cookieMap,equal,SECURE_RECORD_ID,BROWSER_RECORD_ID};
