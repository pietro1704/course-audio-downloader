export const ACTIVE = new Set(['running', 'cancelling', 'detached']);
export const STAGES = {'start-lesson':'Preparando a aula',resolve:'Localizando o áudio',download:'Baixando o áudio',encode:'Preparando o áudio',validate:'Conferindo o resultado','copy-to-icloud':'Copiando para o iCloud','wait-for-upload':'Aguardando confirmação do iCloud','cleanup-local':'Liberando a cópia local','evict-icloud-cache':'Liberando cache local',parallel:'Processando aulas',running:'Executando',finished:'Finalizado',failed:'Falha',interrupted:'Cancelado'};
export function stage(value) { return STAGES[value] || 'CLI em execução'; }
export function pairing(hash) {
  const values = new URLSearchParams(hash.replace(/^#/, ''));
  return {token:values.get('token') || '', port:Number(values.get('port') || 8765)};
}
export function endpoint(port) {
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Porta local inválida.');
  return `http://127.0.0.1:${port}`;
}
export class Connection {
  constructor(port, token, fetcher = globalThis.fetch) {
    this.base = endpoint(port); this.token = token; this.fetcher = fetcher.bind(globalThis);
    if (!token || token.length > 512) throw new Error('Informe a chave privada exibida pelo course-web.');
  }
  async request(path, body) {
    const response = await this.fetcher(this.base+path, {
      method:body ? 'POST' : 'GET', mode:'cors', credentials:'omit', cache:'no-store', redirect:'error',
      signal:AbortSignal.timeout(10000),
      headers:{Authorization:`Bearer ${this.token}`, ...(body ? {'Content-Type':'application/json'} : {})},
      ...(body ? {body:JSON.stringify(body)} : {}),
    });
    if (!response.ok) {
      if (response.status === 401) throw new Error('A chave expirou ou está incorreta. Conecte novamente pelo terminal.');
      if (response.status === 403) throw new Error('Este site não está autorizado pelo course-web. Confira a origem usada no terminal.');
      if (response.status === 409) throw new Error('Operação recusada: confira a execução ativa, as opções e os arquivos cadastrados no CLI.');
      throw new Error('O CLI local não conseguiu atender à solicitação. Confira o terminal.');
    }
    return response.json();
  }
  state() { return this.request('/api/state'); }
  start(request) { return this.request('/api/runs', request); }
  cancel(id) { return this.request('/api/cancel', {id}); }
}
