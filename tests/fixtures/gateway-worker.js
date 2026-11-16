'use strict';
const {SerialQueue}=require('../../src/thu_assistant/scheduler');
const readline=require('node:readline'),fs=require('node:fs'),{setTimeout:sleep}=require('node:timers/promises');
const queue=new SerialQueue(async(command,{signal})=>{
 if(command.journal)fs.appendFileSync(command.journal,command.command+'\n');
 if(command.command==='crash')process.exit(7);
 await sleep(command.delay||1,undefined,{signal});
 return {value:command.value,pid:process.pid};
},message=>process.stdout.write(JSON.stringify(message)+'\n'));
readline.createInterface({input:process.stdin}).on('line',line=>queue.accept(JSON.parse(line))).on('close',()=>queue.close());
