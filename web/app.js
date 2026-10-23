import {ACTIVE, Connection, pairing, stage} from './model.js';
const $ = (id) => document.getElementById(id);
let connection = null, state = null, selected = null, busy = false, timer = null, generation = 0, refreshVersion = 0;
const initial = pairing(location.hash);
if (location.hash) history.replaceState(null, '', location.pathname+location.search);
$('token').value = initial.token; $('port').value = initial.port;
const messages = {
  check:['Verificar curso','Confere o catálogo e os recibos sem baixar ou alterar arquivos.'],
  deliver:['Iniciar entrega','O CLI baixa os arquivos e confirma o upload ao iCloud antes de liberar a cópia local.'],
  documents:['Entregar documentos','Entrega os anexos originais das aulas, sem converter os áudios.'],
  'reconcile-artwork':['Recuperar capas','Recupera originais pelo hash histórico, preservando os áudios entregues.'],
  'refresh-artwork':['Atualizar capas','Repete a busca de capas faltantes em entregas já concluídas.'],
};
function message(text) { $('message').textContent=text; $('message').hidden=!text; }
function render() {
  const active = state?.run && ACTIVE.has(state.run.status);
  $('run-fields').disabled = !connection || !state || busy || !!active || !selected;
  $('connection-status').textContent = connection && state ? '● Mac conectado' : '○ Mac desconectado';
  $('connection-status').classList.toggle('connected', !!connection && !!state);
  $('pairing').hidden = !!connection && !!state;
  $('disconnect').hidden = !connection;
  const operation = $('operation').value, compatible = ['check','deliver'].includes(operation);
  $('protected').disabled = !compatible;
  if (!compatible) $('protected').checked = false;
  $('probe').disabled = operation !== 'check';
  $('start-button').textContent = busy ? 'Aguarde…' : messages[operation][0]+' ↗';
  $('action-note').textContent = messages[operation][1];
  $('courses').replaceChildren();
  for (const job of state?.jobs || []) {
    const button=document.createElement('button'); button.type='button';
    button.className='course'+(job.id===selected ? ' selected' : '');
    button.setAttribute('aria-pressed', String(job.id===selected)); button.disabled=busy || !!active;
    const icon=document.createElement('span');icon.className='course-symbol';icon.textContent='♫';
    const text=document.createElement('span'), name=document.createElement('span'), total=document.createElement('span'), dot=document.createElement('span');
    name.className='course-name';name.textContent=job.label;total.className='course-total';total.textContent=`${job.total} aulas no catálogo`;dot.className='course-radio';
    text.append(name,total);button.append(icon,text,dot);button.addEventListener('click',()=>{selected=job.id;render();});$('courses').append(button);
  }
  if (!state?.jobs.length) {
    const empty=document.createElement('div');empty.className='empty';
    const title=document.createElement('h3'), description=document.createElement('p');
    title.textContent=state ? 'Seu catálogo está vazio.' : 'Uma biblioteca, no seu Mac.';
    description.textContent=state ? 'Cadastre seus cursos no arquivo local usado pelo course-web e reinicie a conexão.' : 'Conecte para ver os cursos cadastrados no CLI.';
    empty.append(title,description);$('courses').append(empty);
  }
  const run=state?.run;
  $('cancel-button').hidden = !run?.can_cancel || run.status==='cancelling' || !connection;
  $('cancel-button').disabled=busy;
  const titles={running:'Seu CLI está trabalhando.',cancelling:'Cancelando com segurança…',succeeded:'Operação concluída.',failed:'O CLI encontrou um problema.',cancelled:'Operação cancelada.',detached:'Uma execução anterior continua no Mac.',unknown:'A execução anterior terminou sem confirmação.'};
  $('progress-title').textContent=run ? titles[run.status] || 'Aguardando o CLI.' : 'Tudo pronto para começar.';
  const p=run?.progress;
  $('progress-detail').textContent = p ? `${stage(p.stage)}${p.title ? ' · '+p.title : ''}` : run?.status==='detached' ? 'Aguarde o processo anterior terminar. Este painel não cancela processos que não iniciou.' : run?.status==='unknown' ? 'Confira os recibos pelo CLI ou faça uma nova verificação antes de continuar.' : run?.status==='failed' ? `Código de saída ${run.exit_code}. Confira o log local e execute uma verificação antes de retomar.` : run?.status==='cancelling' ? 'O CLI aguarda as tarefas ativas e preserva os arquivos. Você pode continuar acompanhando aqui.' : run?.result ? `Verificação: ${run.result.total} aulas, ${run.result.complete ?? 0} concluídas, ${run.result.pending ?? 0} pendentes.` : run?.status==='succeeded' ? 'O CLI concluiu a operação solicitada.' : 'O andamento real do CLI aparece aqui quando você iniciar uma operação.';
  $('progress').max=p?.selected || 1;$('progress').value=p ? Math.min(p.delivered || 0,p.selected || 1) : run?.status==='succeeded' ? 1 : 0;
  if (run && ACTIVE.has(run.status) && !p) $('progress').removeAttribute('value');
  $('progress-count').textContent=p ? `${p.delivered || 0} de ${p.selected || 0} aulas entregues` : run?.status==='running' ? 'Em execução' : state ? 'Mac conectado' : 'Aguardando conexão';
  $('progress-extra').textContent=p?.free_gib != null ? `${p.free_gib} GiB livres no Mac` : 'Processamento local · Entrega pelo iCloud';
}
async function refresh(epoch=generation) {
  if (!connection) return;
  const current=connection, version=++refreshVersion;
  try {
    const next=await current.state();
    if (epoch !== generation || version !== refreshVersion || current !== connection) return;
    state=next; if (!state.jobs.some(j=>j.id===selected)) selected=state.jobs[0]?.id || null;
    render();
  } catch (error) {
    if (epoch !== generation || version !== refreshVersion) return;
    // A failed poll never marks the CLI cancelled or complete.
    state=null;message(error instanceof TypeError ? 'Não foi possível conectar ao Mac. Mantenha o course-web aberto e permita o acesso à rede local se o navegador pedir.' : error.message);render();
  } finally {
    if (epoch === generation && version === refreshVersion && connection) {clearTimeout(timer);timer=setTimeout(()=>refresh(epoch),2000);}
  }
}
$('connect-form').addEventListener('submit',async event=>{
  event.preventDefault(); clearTimeout(timer); generation++;state=null; message('');
  try { connection=new Connection(Number($('port').value),$('token').value);render();await refresh(); }
  catch (error) {connection=null;message(error.message);render();}
});
$('operation').addEventListener('change',render);
$('disconnect').addEventListener('click',()=>{
  generation++;clearTimeout(timer);connection=null;state=null;$('token').value='';selected=null;
  message('Painel desconectado. Uma execução já iniciada continua sob controle do CLI no Mac.');render();
});
$('run-form').addEventListener('submit',async event=>{
  event.preventDefault();if (!connection || !state || !selected || busy) return;
  busy=true;message('');render();
  const values={concurrency:Number($('concurrency').value),start:Number($('start').value),encoder:$('encoder').value,min_free_gib:Number($('reserve').value),upload_timeout:Number($('timeout').value),protected_pauses:$('protected').checked};
  if ($('limit').value) values.limit=Number($('limit').value);
  if ($('operation').value==='check') values.probe=Number($('probe').value);
  try {await connection.start({job_id:selected,operation:$('operation').value,options:values,request_id:crypto.randomUUID()});clearTimeout(timer);await refresh();}
  catch (error) {message(error.message);}
  finally {busy=false;render();}
});
$('cancel-button').addEventListener('click',async()=>{
  if (!connection || !state?.run || busy) return;
  busy=true;render();
  try {await connection.cancel(state.run.id);clearTimeout(timer);await refresh();}
  catch (error) {message(error.message);}
  finally {busy=false;render();}
});
window.addEventListener('hashchange',()=>{
  const incoming=pairing(location.hash);
  if (location.hash) history.replaceState(null,'',location.pathname+location.search);
  if (incoming.token) {$('token').value=incoming.token;$('port').value=incoming.port;$('connect-form').requestSubmit();}
});
render();
if (initial.token) $('connect-form').requestSubmit();
