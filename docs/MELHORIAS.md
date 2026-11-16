# Relatório: o que falta e o que pode melhorar

Estado em 16 de novembro de 2026, depois da migração para o Claude Haiku 5.5.
Os pontos estão ordenados por impacto dentro de cada secção.

## 1. Por verificar

Estas alterações estão cobertas por testes offline, mas ainda não correram
contra serviços reais.

| O quê | Porquê | Como confirmar |
|---|---|---|
| Chamadas reais ao Claude | No momento da migração o `.env` não tinha `ANTHROPIC_API_KEY`, por isso nenhuma chamada foi feita. O esquema de resposta e a cache do prompt só foram testados com substitutos. | Depois de pôr a chave: `grep -i "scoring" jobradar.log`. Deve aparecer "Scoring enabled" e nenhum "scoring failed". |
| Qualidade do Haiku 5.5 na leitura completa | A leitura completa usava antes um modelo maior. O Haiku é muito mais barato, mas a avaliação de requisitos é a tarefa mais difícil do programa. | Comparar uma semana de `/rejected` e do resumo diário com o teu próprio juízo. Se falhar, mudar só `deep_model` para `claude-sonnet-5-5`. |
| Filtro de localização | A regra "aceita Porto ou remoto a partir de Portugal" é avaliada pelo modelo a partir do texto da vaga. | Ver em `/stats` quantas ficam em "Location does not fit" e abrir algumas em `/rejected`. |
| Pesquisa "European Union" | Localização nova, larga, nunca testada em produção. Pode trazer muito ruído ou esbarrar no limite de 10 resultados por pedido. | `/status` ao fim de um dia: contagem e erros dessa pesquisa. |

## 2. Qualidade da correspondência com o CV

1. **Calibrar a probabilidade de entrevista.** Hoje é uma estimativa do
   modelo guiada por uma grelha. Para se tornar uma taxa real falta registar
   o que aconteceu: um comando `/applied <vaga>` e outro para o resultado
   (sem resposta, entrevista, rejeição). Com algumas dezenas de candidaturas
   já dá para comparar a estimativa com a realidade e ajustar a grelha.
2. **Conjunto de avaliação.** Não existe forma de medir se uma alteração ao
   prompt ou ao modelo melhorou ou piorou as decisões. Trinta a cinquenta
   vagas reais classificadas por ti (serve / não serve, e porquê) permitiam
   comparar modelos e prompts com números em vez de impressões.
3. **O limiar de 80% pode ser demasiado apertado.** O objetivo é apanhar o
   máximo de vagas compatíveis; 80% de cobertura deixa de fora vagas em que
   falta um requisito em cinco. Rever ao fim de uma ou duas semanas, com os
   números de `/stats` e o resumo diário de quase-alertas.
4. **Normalização de competências.** "K8s" e "Kubernetes", ou "GenAI" e
   "Generative AI", contam hoje como competências diferentes. Uma tabela de
   sinónimos pequena resolvia os casos mais frequentes.
5. **Reavaliar quando o CV muda.** A coluna "o CV tem esta competência" é
   gravada no momento da leitura. Depois de atualizar o CV, as vagas antigas
   continuam com a resposta antiga, o que distorce `/gaps` durante algum
   tempo.
6. **Vagas séniores não entram no mercado.** O pré-filtro rejeita "Senior"
   antes de a descrição ser lida, por isso o `/market` não vê o que é pedido
   no nível seguinte. Ler essas vagas só para estatística (sem alertar)
   mostraria para onde evoluir, ao custo de mais pedidos ao LinkedIn.

## 3. Cobertura de vagas

1. **Não é possível pesquisar "remoto" no LinkedIn sem login.** O filtro é
   ignorado. A solução atual (pesquisar largo e filtrar depois) funciona,
   mas gasta leituras em vagas presenciais noutros sítios.
2. **Mais fontes para remoto e para Portugal.** O Wellfound só está ligado
   ao Porto. Faltam sites de trabalho remoto e portais portugueses, e as
   APIs públicas dos sistemas de recrutamento (Greenhouse, Lever, Ashby),
   que são estáveis e documentadas, ao contrário do LinkedIn.
3. **Limite de 10 resultados por pedido.** Numa pesquisa larga como a da
   União Europeia, 10 resultados numa janela de 30 minutos podem não chegar.
   Dividir por países ou por palavras-chave aumenta a cobertura.
4. **Sem recuo perante bloqueios.** Quando o LinkedIn responde 429, a
   pesquisa falha e volta a tentar no ciclo seguinte ao mesmo ritmo. Um
   intervalo crescente reduzia o risco de bloqueio prolongado.

## 4. Robustez e operação

1. **Inundação quando o modelo falha.** Com a API em baixo ou sem saldo,
   todas as vagas que passam o pré-filtro são enviadas. Agora vêm marcadas
   com "Not filtered", mas continuam a chegar. Melhor: ao fim de algumas
   falhas seguidas, enviar um único aviso e guardar as vagas para avaliar
   quando a API voltar.
2. **Sem integração contínua.** Os testes e o lint só correm à mão. Um
   workflow do GitHub Actions com `pytest`, `ruff` e `pip-audit` em cada
   pull request apanhava regressões antes do merge.
3. **Sem registo de custos.** As respostas do Claude trazem o número de
   tokens usados e lidos da cache; nada disso é guardado. Sem isso não se
   sabe quanto custa por dia nem se a cache está a funcionar.
4. **A base de dados cresce sem limite e não tem cópia de segurança.** Agora
   que guarda o histórico do mercado, perder o `state.db` deixou de ser
   inofensivo. Falta uma cópia diária e uma política de retenção.
5. **Depende do portátil ligado.** Um servidor pequeno sempre ligado
   eliminava as falhas quando o computador dorme ou está sem rede.
6. **Token do Telegram.** Esteve escrito em ficheiros de log antigos (já
   apagados; nunca foi para o GitHub). O bot funciona sem alterações, mas
   revogar o token no BotFather continua a ser a opção segura.

## 5. Código

1. **`store.py` tem mais de 500 linhas.** Está organizado por secções, mas
   as consultas de estatística e de mercado podiam passar para um módulo
   próprio.
2. **Sem verificação de tipos.** O código tem anotações mas nenhum `mypy`
   ou `pyright` a validá-las.
3. **`commands.py` mistura consulta e formatação.** Funciona e está testado;
   separar as duas coisas facilitava acrescentar outro canal além do
   Telegram (por exemplo, um relatório semanal em HTML).
4. **Estatísticas só em texto.** Não há gráficos nem evolução ao longo do
   tempo (por exemplo, a procura de uma competência mês a mês). Os dados já
   estão na base de dados para isso.

## Ordem sugerida

1. Pôr a `ANTHROPIC_API_KEY`, reiniciar e confirmar no log que a avaliação
   funciona (secção 1).
2. Deixar correr uma semana e rever limiares com `/stats` e `/rejected`.
3. Integração contínua e cópia de segurança do `state.db`.
4. Registo de candidaturas e resultados, para calibrar a probabilidade.
5. Mais fontes de vagas remotas.
