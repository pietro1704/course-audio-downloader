const $=id=>document.getElementById(id);
let state={course:null,job:null},busy=true,inspecting=false,courseKey='',lessonRows=new Map();
const active=new Set(['starting','audio','audio-complete','attachments','attachment-complete','cancelling','verifying']);
function message(text){$('message').textContent=text;$('message').hidden=!text;}
async function api(path,body){
 const response=await fetch(path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',redirect:'error',
  headers:body?{'Content-Type':'application/json'}:{},...(body?{body:JSON.stringify(body)}:{})});
 if(!response.ok){
  if(response.status===401)throw new Error('Reabra o aplicativo para iniciar esta sessão.');
  const detail=await response.json().catch(()=>({}));
  const reasons={'login-required':'Entre no site do curso no seu navegador e tente novamente.',
   'authenticated-browser-failed':'Não foi possível abrir a sessão do navegador. Confira a instalação do navegador interno do aplicativo.',
   'course-discovery-timeout':'Não foi possível identificar a lista de aulas nesta página.',
   'no-course-lessons-found':'Nenhuma aula foi identificada nesta página.',
   'unsupported-course-url':'Este endereço não corresponde a uma página de curso ou aula.'};
  throw new Error(reasons[detail.error]||'Não foi possível concluir a operação. Confira a sessão do curso e tente novamente.');
 }
 return response.json();
}
function button(text,action,secondary=false){const b=document.createElement('button');b.type='button';b.className='mini'+(secondary?' secondary':'');b.textContent=text;b.disabled=busy||!!(state.job&&active.has(state.job.phase));b.addEventListener('click',action);return b;}
function render(){
 const job=state.job,isActive=job&&active.has(job.phase),course=state.course;
 $('load-course').disabled=busy||!!isActive;$('course-url').disabled=busy||!!isActive;
 $('course').hidden=!course;$('progress-card').hidden=!job;
 const nextCourseKey=course?JSON.stringify([course.id,job?.id]):'';
 if(course&&nextCourseKey!==courseKey){
  courseKey=nextCourseKey;
  $('course-title').textContent=course.title;$('course-count').textContent=course.lessons.length+' aulas encontradas';
  $('course-description').textContent=course.description||'';
  $('lesson-list').replaceChildren();lessonRows.clear();const parents=new Map();
  for(const module of course.modules_info||[]){
   const group=document.createElement('section');group.className='module';const head=document.createElement('div');head.className='module-heading';
   const title=document.createElement('h3');title.textContent=module.title;head.append(title,button('Baixar módulo ↓',()=>start('module',module.id)));
   group.append(head);$('lesson-list').append(group);parents.set(module.id,group);
  }
  for(const lesson of course.lessons){
   const row=document.createElement('div');row.className='lesson';const head=document.createElement('div');head.className='lesson-top';
   const text=document.createElement('div');text.className='lesson-title';text.textContent=lesson.display_title;
   const duration=document.createElement('span');duration.className='duration';duration.textContent=lesson.duration||'';text.append(duration);
   const actions=document.createElement('div');actions.className='lesson-actions';actions.append(button('Baixar aula ↓',()=>start('lesson',lesson.id)));
   head.append(text,actions);row.append(head);
   const files=document.createElement('div');files.className='files';
   row.append(files);lessonRows.set(lesson.id,{files,known:new Set(),pending:new Map()});
   (parents.get(lesson.module_id)||$('lesson-list')).append(row);
  }
 }
 if(course){
  $('download-course').disabled=busy||!!isActive;$('silence-removal').disabled=busy||!!isActive;
  for(const node of $('lesson-list').querySelectorAll('button'))node.disabled=busy||!!isActive;
  for(const lesson of course.lessons){
   const row=lessonRows.get(lesson.id);if(!row)continue;
   const files=(job?.files||[]).filter(f=>f.lesson_id===lesson.id);
   for(const file of files){
    if(row.known.has(file.id))continue;row.known.add(file.id);
    if(file.kind==='audio'){
     const audio=document.createElement('audio');audio.controls=true;audio.preload='none';audio.src='/api/file?id='+file.id;
     audio.setAttribute('aria-label','Ouvir '+lesson.title);row.files.append(audio);
    }else{
     const link=document.createElement('a');link.href='/api/file?id='+file.id;link.textContent=file.name;row.files.append(link);
    }
   }
   if(lesson.attachments_state==='pending'&&!row.pending.has('inventory')){
    const item=document.createElement('span');item.className='attachment-pending';item.textContent='Anexos serão conferidos ao baixar esta aula';
    row.files.append(item);row.pending.set('inventory',item);
   }
   if(files.some(f=>f.kind==='document')){row.pending.get('inventory')?.remove();row.pending.delete('inventory');}
   for(const attachment of lesson.attachments||[]){
    const done=files.some(f=>f.kind==='document'&&(f.attachment_id===attachment.id||f.name===attachment.name));
    if(done){row.pending.get(attachment.id)?.remove();row.pending.delete(attachment.id);}
    else if(!row.pending.has(attachment.id)){
     const item=document.createElement('span');item.className='attachment-pending';
     item.textContent=attachment.name+' · acompanha a aula';row.files.append(item);row.pending.set(attachment.id,item);
    }
   }
  }
 }
 if(job){
  const phases={starting:'Preparando o download…',audio:'Baixando e preparando áudio…','audio-complete':'Áudio concluído',attachments:'Conferindo os anexos…','attachment-complete':'Anexo concluído',complete:'Download concluído.',failed:'A operação encontrou um problema.',cancelled:'Download cancelado.',cancelling:'Cancelando com segurança…',verifying:'Conferindo os arquivos finais…'};
  $('progress-title').textContent=phases[job.phase]||'Operação em andamento.';
  $('progress-detail').textContent=job.title||'';
  $('progress').max=job.total||1;$('progress').value=job.audios_complete||0;
  $('progress-count').textContent=`${job.audios_complete||0}/${job.total||0} áudios · ${job.attachments_complete||0} anexos`;
  $('cancel').hidden=!isActive||job.phase==='cancelling';$('cancel').disabled=busy;
  $('archive').hidden=!job.ready;$('archive').href='/api/archive?id='+job.id;
 }
}
async function refresh(){
 if(inspecting)return;
 try{state=await api('/api/state');render();}catch(error){message(error.message);}
}
async function start(scope,id){
 if(busy)return;busy=true;message('');render();
 try{await api('/api/download',{scope,id,silence_removal:$('silence-removal').checked,request_id:crypto.randomUUID()});await refresh();}
 catch(error){message(error.message);}finally{busy=false;render();}
}
$('course-form').addEventListener('submit',async event=>{
 event.preventDefault();if(busy)return;busy=true;inspecting=true;message('Buscando as informações do curso…');render();
 try{await api('/api/course',{url:$('course-url').value});inspecting=false;await refresh();message('');}
 catch(error){message(error.message);}finally{busy=false;inspecting=false;render();}
});
$('download-course').addEventListener('click',()=>{if(state.course)start('course',state.course.id);});
$('cancel').addEventListener('click',async()=>{if(state.job){try{await api('/api/cancel',{id:state.job.id});await refresh();}catch(error){message(error.message);}}});
(async()=>{
 render();
 const session=new URLSearchParams(location.hash.replace(/^#/, '')).get('session');
 if(location.hash)history.replaceState(null,'',location.pathname);
 try{if(session)await api('/api/session',{session});await refresh();}catch(error){message(error.message);}finally{busy=false;render();}
 setInterval(refresh,1000);
})();
