# Downloader independente de curso

Status: verified

## Resultado solicitado
Remover nomes de cursos/provedores de módulos, arquivos internos e lógica. Regras de navegação e mídia devem vir de perfil/configuração e catálogo, sem mudar código para outro curso. Preservar títulos e organização dos áudios como dados literais do site. Testar dry run antes de continuar o catálogo integral pendente.

## Escopo e implementação
Renomear módulos e metadados para nomes neutros. Separar perfis de navegação, padrões de players e catálogo de identidade do mecanismo compartilhado (download, AAC32, capa, validação, iCloud e limpeza). Preservar funcionalidades existentes com transportes direto, JSON incorporado e HLS por template configuráveis. Migrar dados e caminhos de recibos atomicamente com backup e trava exclusiva depois que o worker atual drenar. Não remover nem rebaixar uploads confirmados.

## Aceitação e testes
Código, documentação e nomes de arquivos versionados sem marcas/cursos/IDs concretos. Configuração privada fora do repositório contém apenas os dados necessários ao site. Mesma interface exercitada com dois catálogos/perfis sintéticos distintos e perfil real. Testes de sucesso, identidade errada, timeouts, cancelamento, capas, native upload e retomada. Dry run sem download, conversão, upload, exclusão ou gravação no diretório de execução; validar catálogos, destinos, colisões, recibos e resolução real das primeiras aulas pendentes. Executar gate real python -m unittest -v, inspecionar diff e dry run, então retomar o catálogo integral com entregas existentes reaproveitadas.

## Riscos e recuperação
Worker validado mantém downloads durante a refatoração; drenar antes da migração. Preservar parciais e recibos; backups externos anteriores permitem recuperar a migração. URLs de mídia e sessão só em memória. Commit, push da feature branch e PR autorizados pelas instruções vigentes; aguardar CI antes de concluir. Nenhuma dependência da aplicação pode conter regra de curso; dados reais ficam em perfil/catálogo do usuário. Goal integral continua ativo.


## Revisão de código e evidências
Standards: módulos e comandos neutros; perfis reais e histórico preservados fora do repositório. Diff revisto contra ca59a63 incluindo novos arquivos; sem contratos concretos no código, nomes versionados ou documentação. Preservadas capas, nomes literais, recibos fsynced, upload nativo e cancelamento com drenagem. Oito ciclos simultâneos exercitados com resolução serial, hashes e recibos íntegros.

Spec: 150 testes passaram em 52,131 s. Os casos de schema desconhecido e symlink ancestral falharam antes da correção e passaram depois. Wheel compilou e contém os doze módulos genéricos e apenas comandos neutros. Dry run real: 67 aulas, 33 completas, 3 preparadas, 31 pendentes, quatro resoluções autenticadas bem-sucedidas, concorrência oito. Dry run sintético prova ausência de escrita/download/encode em dois catálogos independentes. Retomada de recibos prepared/uploaded/complete e rejeição de hashes/identidades conflitantes cobertas.

Correção do gate existente: a checagem de higiene anterior rejeitava o exemplo público de configuração e podia mascarar falhas do grep. Agora permite somente esse exemplo e propaga erro/match do scan; caso real e índice Git alternativo com configuração privada foram exercitados.

Sem findings de código pendentes. Migração final do diretório de execução depende da drenagem do worker validado; não é bloqueio para o PR. CI aprovado nas execuções de push e PR; retomada real com oito aulas simultâneas verificada. Não marcar a goal integral como concluída antes da entrega e auditoria de todas as aulas.


## Verificação final
Catálogo integral: 67/67 entregas nativas confirmadas, 67 caminhos exatos, 67 hashes de fonte distintos e capas de vídeo em todos os áudios. Preset conferido em todos os recibos; nenhum original ou áudio convertido restante no diretório de trabalho. Os dois menus do site foram reconciliados novamente: 134 links, 67 identidades/títulos únicos, nenhuma falta ou sobra. Recibos e manifests dos lotes independentes consolidados depois da saída dos workers; diretórios históricos arquivados. Parciais antigos (162.267.268 bytes) removidos somente após confirmação nativa, hash inalterado e vínculo ao caminho de staging da aula correta.

Instalação local atualizada pelo wheel, com backup anterior externo. Os doze módulos instalados conferem byte a byte com o worktree; comandos e módulos antigos removidos. Dry run da instalação, executado fora do repositório, passou nas 67 aulas: 67 completas e zero pendentes/preparadas. Todos os workers e seus keep-awakes terminaram. O monitor encerrou, e a auditoria final está nos dados privados do diretório de execução. Gate local: 150 testes; CI de push e PR aprovado. Esta alteração final registra evidências e não muda código executável.
