const fs=require('node:fs'),path=require('node:path'),{setTimeout:sleep}=require('node:timers/promises');
module.exports={createUpstreamClient:()=>({request:async options=>{
 const dir=process.env.MADMODEL_STATE_DIR;
 if(fs.existsSync(path.join(dir,'auth.lock')))throw Object.assign(new Error(),{code:'AUTH_LOCK_HELD_DURING_MODEL'});
 if(fs.existsSync(path.join(dir,'campus','campus-state-v2.lock')))throw Object.assign(new Error(),{code:'STATE_LOCK_HELD_DURING_MODEL'});
 const countFile=path.join(dir,'model-count');
 const count=fs.existsSync(countFile)?Number(fs.readFileSync(countFile))+1:1;fs.writeFileSync(countFile,String(count));
 fs.writeFileSync(path.join(dir,'model-started'),String(process.pid));
 await sleep(Number(process.env.THU_TEST_MODEL_DELAY||10),undefined,{signal:options.signal});
 const mode=process.env.THU_TEST_MODEL_MODE;
 if(mode==='started401')options.onOpen();
 if(mode==='401once'&&count===1||mode==='401always'||mode==='started401')return {type:'upstream-error',status:401};
 if(mode==='403'||mode==='429')return {type:'upstream-error',status:Number(mode)};
 if(mode==='network')return {type:'network-error'};
 return {type:'completion',body:{choices:[{message:{content:'synthetic reply'}}]}};
}})};
