'use strict';
// Local Windows Codex IPC adapter. No network listener, dependency or second agent.
const net = require('node:net');
const {randomUUID} = require('node:crypto');
const fs = require('node:fs');
const PIPE = '\\\\.\\pipe\\codex-ipc';
const VERSIONS = {'thread-owner-discovery':1,'thread-follower-read-model-settings':1,'thread-follower-start-turn':2,'thread-follower-steer-turn':1};
class DesktopIPC {
  constructor() { this.pending=new Map();this.buffer=Buffer.alloc(0);this.clientId='initializing-client';this.listeners=new Set(); }
  async connect() {
    this.socket=net.createConnection(PIPE);
    this.socket.on('data',c=>this.consume(c));
    this.socket.on('error',e=>this.fail(e));
    this.socket.on('close',()=>this.fail(new Error('Desktop IPC closed; mutation outcome may be unknown. Do not retry automatically.')));
    await new Promise((resolve,reject)=>{this.socket.once('connect',resolve);this.socket.once('error',reject)});
    this.clientId=(await this.request('initialize',{clientType:'agent-rooms-bridge'})).result.clientId;
  }
  fail(error) {for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(error)}this.pending.clear();}
  write(message) {const body=Buffer.from(JSON.stringify(message));if(body.length>8*1024*1024)throw Error('Request too large');const h=Buffer.alloc(4);h.writeUInt32LE(body.length);this.socket.write(Buffer.concat([h,body]));}
  consume(chunk) {
    this.buffer=Buffer.concat([this.buffer,chunk]);
    while(this.buffer.length>=4){const n=this.buffer.readUInt32LE(0);if(!n||n>256*1024*1024){this.socket.destroy(new Error('Invalid IPC frame'));return}if(this.buffer.length<n+4)return;
      let msg;try{msg=JSON.parse(this.buffer.subarray(4,n+4))}catch(e){this.socket.destroy(e);return}this.buffer=this.buffer.subarray(n+4);
      if(msg.type==='client-discovery-request'){this.write({type:'client-discovery-response',requestId:msg.requestId,response:{canHandle:false}});continue}
      if(msg.type==='response'){const p=this.pending.get(msg.requestId);if(!p)continue;clearTimeout(p.timer);this.pending.delete(msg.requestId);msg.resultType==='success'?p.resolve(msg):p.reject(new Error(msg.error||'IPC error'));}
      else if(msg.type==='broadcast')for(const listener of this.listeners)listener(msg);
    }
  }
  request(method,params,targetClientId,timeoutMs=15000) {
    const requestId=randomUUID();
    return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{this.pending.delete(requestId);reject(Error('IPC timeout; mutation outcome may be unknown. Do not retry automatically.'))},timeoutMs);
      this.pending.set(requestId,{resolve,reject,timer});
      try{this.write({type:'request',requestId,sourceClientId:this.clientId,method,params,version:VERSIONS[method]||0,targetClientId,timeoutMs})}catch(e){clearTimeout(timer);this.pending.delete(requestId);reject(e)}
    });
  }
  async owner(threadId){return (await this.request('thread-owner-discovery',{hostId:'local',conversationId:threadId})).handledByClientId;}
  async snapshot(threadId,owner) {
    return new Promise((resolve,reject)=>{
      const end=()=>{clearTimeout(timer);this.listeners.delete(onMessage);this.write({type:'broadcast',sourceClientId:this.clientId,method:'thread-stream-following-changed',version:1,targetClientIds:[owner],params:{conversationId:threadId,hostId:'local',following:false}})};
      const onMessage=m=>{if(m.method==='thread-stream-state-changed'&&m.params?.conversationId===threadId&&m.params?.change?.type==='snapshot'){end();resolve(m.params.change.conversationState)}};
      const timer=setTimeout(()=>{end();reject(Error('Desktop snapshot timeout'))},10000);this.listeners.add(onMessage);
      this.write({type:'broadcast',sourceClientId:this.clientId,method:'thread-stream-following-changed',version:1,targetClientIds:[owner],params:{conversationId:threadId,hostId:'local',following:true}});
    });
  }
  close(){this.socket?.destroy();}
}
async function execute(request) {
  const {method,params={}}=request;
  if(!['thread/owner','thread/settings','thread/state','turn/start','turn/steer'].includes(method))throw Error('Unsupported bridge method');
  const threadId=params.threadId;if(typeof threadId!=='string'||!threadId.trim())throw Error('threadId required');
  const ipc=new DesktopIPC();
  try{
    await ipc.connect();const owner=await ipc.owner(threadId);
    if(method==='thread/owner')return {threadId,ownerClientId:owner,transport:PIPE};
    if(method==='thread/settings')return (await ipc.request('thread-follower-read-model-settings',{conversationId:threadId},owner)).result;
    if(method==='thread/state')return await ipc.snapshot(threadId,owner);
    if(!Array.isArray(params.input)||params.input.length===0||params.input.some(i=>i.type!=='text'||typeof i.text!=='string'||!i.text.trim()))throw Error('This adapter currently accepts non-empty text input only');
    params.input=params.input.map(i=>({type:'text',text:i.text,text_elements:[]}));
    const state=await ipc.snapshot(threadId,owner);
    if(method==='turn/start'&&state.threadRuntimeStatus?.type==='active')throw Error('Thread is active; use turn/steer. No message sent.');
    if(method==='turn/steer'&&state.threadRuntimeStatus?.type!=='active')throw Error('Thread is not active; use turn/start. No message sent.');
    const clientUserMessageId=params.clientUserMessageId||randomUUID();
    if(method==='turn/start')return (await ipc.request('thread-follower-start-turn',{conversationId:threadId,turnStart:{request:{threadId,input:params.input,clientUserMessageId},context:{inheritThreadSettings:true}}},owner,30000)).result.result;
    return (await ipc.request('thread-follower-steer-turn',{conversationId:threadId,input:params.input,clientUserMessageId,restoreMessage:{text:params.input.map(i=>i.text).join('\n'),context:{}},attachments:[]},owner,30000)).result.result;
  }finally{ipc.close()}
}
module.exports={DesktopIPC,execute};
if(require.main===module){
  (async()=>{const request=JSON.parse(fs.readFileSync(0,'utf8').replace(/^\uFEFF/,''));const result=await execute(request);console.log(JSON.stringify({id:request.id??null,result}));})().catch(e=>{console.log(JSON.stringify({error:{message:e.message}}));process.exitCode=1});
}
