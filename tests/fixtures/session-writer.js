// Test-only identity transform; production state always uses Windows DPAPI.
const path=require('node:path'),fs=require('node:fs');
const {SessionStore}=require('../../src/thu_assistant/session-store');
const {CampusCookies}=require('../../src/thu_assistant/cookies');
const locks=require(process.env.THU_TEST_LOCK_MODULE);
const dir=process.env.THU_ASSISTANT_STATE_DIR;
const store=new SessionStore({dir,account:{username:'synthetic'},dpapi:(m,b)=>b,locks,
 atomicWrite:(f,d)=>{const tmp=f+'.'+process.pid+'.tmp';fs.writeFileSync(tmp,d);fs.renameSync(tmp,f);}});
(async()=>{
 const base=await store.load(),jar=new CampusCookies(base.cookies);
 process.stdout.write('ready\n');
 process.stdin.once('data',async raw=>{
  try{jar.absorb('https://learn.tsinghua.edu.cn/',String(raw).trim()+'; Path=/; Secure');await store.commit(base,jar,{});process.stdout.write('saved\n');}
  catch(e){process.stdout.write(e.code+'\n');}
  finally{process.stdin.destroy();}
 });
})().catch(()=>process.exit(1));
