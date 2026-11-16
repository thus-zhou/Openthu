const {CookieJar}=require('./madmodel-auth');
const originalFetch=globalThis.fetch;
globalThis.fetch=async(url,options)=>{
 if(String(url)==='https://madmodel.cs.tsinghua.edu.cn/')
  return new Response('<script src="/assets/index-fixture.js"></script>');
 if(String(url)==='https://madmodel.cs.tsinghua.edu.cn/assets/index-fixture.js')
  return new Response(JSON.stringify([{id:'synthetic-model',label:'Synthetic',thinkingParam:null}]));
 return originalFetch(url,options);
};
module.exports={dpapi:(_mode,input)=>input,restoreSession:()=>new CookieJar()};
