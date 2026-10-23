const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.hidden=false;this.checked=true;this.listeners={};}
 append(...nodes){for(const n of nodes){n.parent=this;this.children.push(n);}}
 replaceChildren(){this.children=[];}
 setAttribute(k,v){this[k]=v;}
 addEventListener(k,f){this.listeners[k]=f;}
 querySelectorAll(tag){return this.children.flatMap(n=>[...(n.tagName===tag?[n]:[]),...n.querySelectorAll(tag)]);}
 remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}
}
const ids=new Map();
const document={getElementById(id){if(!ids.has(id))ids.set(id,new Element(id));return ids.get(id);},createElement:tag=>new Element(tag)};
const sandbox={document,location:{hash:'',pathname:'/'},history:{replaceState(){}},URLSearchParams,
 crypto:{randomUUID:()=> 'nonce'},setInterval(){},fetch:async()=>({ok:true,json:async()=>({course:null,job:null})})};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync('local_portal/assets/app.js','utf8'),sandbox);
function render(value){sandbox.input=value;vm.runInContext('state=input;render()',sandbox);}
const lesson={id:'one',title:'First',display_title:'01. First',attachments:[{id:'pdf',name:'notes.pdf'}]};
const course={id:'course',title:'Example',lessons:[lesson]};
render({course,job:null});
assert.equal(document.getElementById('lesson-list').querySelectorAll('span').some(x=>x.textContent==='notes.pdf · acompanha a aula'),true);
const audioFile={id:'audio',lesson_id:'one',kind:'audio',name:'first.m4a'};
const job={id:'run',phase:'audio',files:[audioFile],total:2,audios_complete:1,ready:false};
render({course,job});
const player=document.getElementById('lesson-list').querySelectorAll('audio')[0];
assert.equal(player.src,'/api/file?id=audio');assert.equal(document.getElementById('archive').hidden,true);
player.currentTime=42;
render({course,job:{...job,files:[audioFile,{id:'document',lesson_id:'one',kind:'document',name:'notes.pdf'}]}});
assert.equal(document.getElementById('lesson-list').querySelectorAll('audio')[0],player);
assert.equal(player.currentTime,42);
assert.equal(document.getElementById('lesson-list').querySelectorAll('a')[0].textContent,'notes.pdf');
assert.equal(document.getElementById('lesson-list').querySelectorAll('span').some(x=>x.textContent==='notes.pdf · acompanha a aula'),false);
render({course,job:{...job,phase:'complete',ready:true}});
assert.equal(document.getElementById('lesson-list').querySelectorAll('audio')[0],player);
assert.equal(document.getElementById('archive').hidden,false);
console.log('Attachments, streaming before completion and stable player identity: OK');
