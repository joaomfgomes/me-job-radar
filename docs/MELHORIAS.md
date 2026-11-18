# Relatório: estado e o que falta

Estado em 18 de novembro de 2026. O caso de uso — vagas de IA no Porto,
avaliadas contra o CV pelo Claude Haiku 5.5 — está completo e a correr.
Este documento separa o que foi confirmado em produção do que continua em
aberto.

## Confirmado em produção

| O quê | Como foi confirmado |
|---|---|
| Chamadas reais ao Claude (título e leitura completa) | Vagas reais avaliadas no log, com veredictos coerentes |
| Extração de dados de mercado | Linhas em `job_insights` e `job_skills` após as primeiras leituras |
| Pesquisa só no Porto, 10 pesquisas por ciclo | Log do arranque e do primeiro ciclo |
| Fim dos bloqueios 429 nas pesquisas | Ciclo completo sem avisos depois do espaçamento de 2,5 s |
| Migração do `state.db` existente | O serviço arrancou sobre a base de dados antiga sem perder histórico |

## Afinado a 18 de novembro, com base em 90 dias de dados

Em dois dias com o limiar a 80% não houve nenhum alerta. A análise mostrou
que a causa estava nos filtros, não na falta de vagas:

| Problema medido | Alteração |
|---|---|
| A mesma vaga republicada foi avaliada várias vezes (2.175 casos em 90 dias), com veredictos diferentes: 33% num dia, 83% no outro | Uma vaga republicada nos últimos 30 dias herda o veredicto anterior e nunca é enviada duas vezes |
| No Porto, a faixa 60–79% tinha tantas vagas boas como a de 80%+ | Alerta a partir de 60%; resumo diário a partir de 50% |
| "4+ anos" com teto de 3 era o motivo de rejeição mais comum depois da leitura completa (58 vagas) | Margem de 1 ano: 4+ é enviada com aviso, 5+ é rejeitada |
| "Data Scientist" e "Python" traziam vagas do Porto que as pesquisas de IA não apanhavam; o Wellfound devolvia zero | Mais quatro palavras-chave no LinkedIn; Wellfound desligado |

Fontes avaliadas e não adotadas: a Landing.jobs tem API pública, mas só 10
vagas no Porto e nenhuma de IA. A ITJobs.pt tem API oficial com filtro de
distrito e encontrou pelo menos uma vaga de IA no Porto que o LinkedIn não
mostrou; precisa de uma chave gratuita, pedida em itjobs.pt/api.

## O que foi removido, e porquê

Com a pesquisa fixa no Porto, estas partes deixaram de ter utilidade e só
acrescentavam código para manter:

- **Gestão de localizações pelo Telegram** (`/countries`, validação ao vivo,
  localizações guardadas na base de dados). A localização vive agora só no
  `config.yaml`.
- **Rotação de pesquisas "secundárias".** Existia para repartir dezenas de
  pesquisas por vários ciclos; com dez, correm todas em cada ciclo.
- **Pesquisas escritas à mão** em paralelo com as geradas. Ficou uma única
  forma de as definir.
- **Filtro de trabalho remoto do LinkedIn.** Era enviado mas ignorado pelo
  servidor.
- **Cinco escritas separadas por vaga** na base de dados, substituídas por
  uma (`record_verdict`).

As tabelas `location_prefs` e `custom_locations` continuam no teu
`state.db`, sem uso; não foram apagadas para não destruir dados.

## Em aberto: qualidade da correspondência

1. **Calibrar a probabilidade de entrevista.** Hoje é uma estimativa do
   modelo guiada por uma grelha. Para se tornar uma taxa real falta registar
   candidaturas e resultados (um comando `/applied` e outro para o desfecho).
   Com algumas dezenas de casos já dá para comparar a estimativa com a
   realidade.
2. **Conjunto de avaliação.** Não há forma de medir se uma alteração ao
   prompt ou ao modelo melhora as decisões. Trinta a cinquenta vagas reais
   classificadas por ti permitiam comparar com números em vez de impressões,
   e decidir com dados se o Haiku chega ou se a leitura completa merece um
   modelo maior.
3. **Rever os novos limiares** (60%, margem de 1 ano) ao fim de duas
   semanas com `/stats`, `/rejected` e o resumo diário. Se os alertas
   tiverem demasiado ruído, subir para 65–70%.
4. **O CV tem uma experiência por preencher** (2023–2024). Enquanto estiver
   vazia não conta para a cobertura nem para a probabilidade.
5. **Normalização de competências.** "K8s" e "Kubernetes" contam como
   competências diferentes. Uma tabela de sinónimos pequena resolvia os
   casos mais frequentes.
6. **Reavaliar quando o CV muda.** "O CV tem esta competência" é gravado no
   momento da leitura; depois de atualizar o CV, as vagas antigas mantêm a
   resposta antiga e distorcem `/gaps` durante algum tempo.
7. **Vagas séniores não entram no mercado.** O pré-filtro rejeita "Senior"
   antes da leitura, por isso `/market` não mostra o que é pedido no nível
   seguinte.

## Em aberto: cobertura

1. **Faltam as APIs públicas dos sistemas de recrutamento** (Greenhouse, Lever, Ashby), que são estáveis e
   documentadas, ao contrário do LinkedIn.
2. **ITJobs.pt como segunda fonte.** API oficial, gratuita com chave;
   falta pedir a chave.

## Em aberto: operação

1. **Inundação quando o modelo falha.** Com a API em baixo, as vagas que
   passam o pré-filtro são todas enviadas, marcadas com "Not filtered".
   Melhor seria, após algumas falhas seguidas, enviar um único aviso e
   guardar as vagas para avaliar quando a API voltar.
2. **Sem cópia de segurança nem retenção do `state.db`.** Agora que guarda o
   histórico do mercado, perdê-lo deixou de ser inofensivo.
3. **Depende do portátil ligado.** Um servidor pequeno eliminava as falhas
   quando o computador dorme ou fica sem rede.
4. **Sem verificação de tipos.** O código tem anotações, mas nenhum `mypy`
   ou `pyright` as valida na integração contínua.
5. **Token do Telegram.** Esteve em logs locais antigos, já apagados, e
   nunca foi para o GitHub. Revogá-lo no BotFather continua a ser a opção
   segura.

## Ordem sugerida

1. Preencher a experiência em falta no CV e correr `cv_ingest`.
2. Deixar correr duas semanas e rever os limiares.
3. Pedir a chave da ITJobs.pt e acrescentá-la como fonte.
4. Cópia de segurança do `state.db`.
5. Registo de candidaturas e resultados, para calibrar a probabilidade.
6. Conjunto de avaliação, antes de mexer em prompts ou trocar de modelo.
