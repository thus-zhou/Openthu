// Test-only identity transform; production state always uses the configured
// provider. When THU_SECURE_KEY_FILE is present, the same store commits through
// the private crypto child and the injected identity path is not used.
const path=require('node:path'),fs=require('node:fs');
const {SessionStore}=require('../../src/thu_assistant/session-store');
const {CampusCookies}=require('../../src/thu_assistant/cookies');
const locks=require(process.env.THU_TEST_LOCK_MODULE);
const dir=process.env.THU_ASSISTANT_STATE_DIR;
const options={dir,account:{username:'synthetic'},dpapi:(m,b)=>b,locks,
 atomicWrite:(f,d)=>{const tmp=f+'.'+process.pid+'.tmp';fs.writeFileSync(tmp,d);fs.renameSync(tmp,f);}};
if(process.env.THU_SECURE_KEY_FILE){
 const {createSecureCrypto,resolveCryptoConfig}=require('../../src/thu_assistant/crypto-bridge');
 options.crypto=createSecureCrypto(resolveCryptoConfig(process.env,dir));
 options.platform=process.platform;options.allowLegacy=false;delete options.dpapi;
}
const store=new SessionStore(options);
(async()=>{
 const base=await store.load(),jar=new CampusCookies(base.cookies);
 process.stdout.write('ready\n');
 process.stdin.once('data',async raw=>{
  try{jar.absorb('https://learn.tsinghua.edu.cn/',String(raw).trim()+'; Path=/; Secure');await store.commit(base,jar,{});process.stdout.write('saved\n');}
  catch(e){process.stdout.write(String(e.code)+'\n');}
  finally{if(options.crypto)options.crypto.close();process.stdin.destroy();}
 });
})().catch(()=>process.exit(1));
