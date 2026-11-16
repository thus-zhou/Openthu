const fs=require('node:fs'),path=require('node:path');
const file=path.join(process.env.MADMODEL_STATE_DIR,'fixture-token.json');
module.exports={readAccount:()=>({username:'synthetic-user',password:'synthetic-password',fingerPrint:'synthetic-device'}),
 readToken:()=>{try{return JSON.parse(fs.readFileSync(file,'utf8'));}catch{return null;}},
 writeToken:(token,expiresAt,cookie)=>fs.writeFileSync(file,JSON.stringify({token,expiresAt,cookie}))};
