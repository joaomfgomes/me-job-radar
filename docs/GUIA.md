# Guia do código

Este documento explica como o job-radar funciona por dentro, pela ordem em
que as coisas acontecem. O [README](../README.md) explica como instalar e
usar; aqui o objetivo é perceber o código.

## A ideia em três frases

De dez em dez minutos o programa pergunta ao LinkedIn e ao Wellfound que
vagas novas existem. Cada vaga nova passa por uma série de filtros, do mais
barato para o mais caro, e só as que encaixam no teu CV chegam ao Telegram.
Tudo o que o modelo lê fica guardado numa base de dados SQLite, de onde
saem as estatísticas e a análise do mercado.

## O percurso de uma vaga

```
config.yaml ──► pipeline.py ──► sources/*.py ──► store.py (já vi esta vaga?)
                    │
                    ▼
              evaluation.py  ── decide: alertar ou reter
                    │   usa: prefilter.py, lang_filter.py,
                    │        sources/linkedin_jd.py, scoring.py ──► llm.py ──► Claude
                    ▼
        notify.py (Telegram)  +  store.py (regista o veredicto e os dados de mercado)
```

1. **`main.py`** arranca tudo: lê a configuração, cria os objetos e agenda
   dois trabalhos (o ciclo de pesquisa e o resumo diário). Depois fica a
   ouvir comandos do Telegram na thread principal. É o único sítio que sabe
   como as peças encaixam; todos os outros módulos recebem aquilo de que
   precisam como argumento, e é isso que permite testá-los com objetos
   falsos.
2. **`pipeline.py`** corre um ciclo. Para cada pesquisa ativa pede as vagas
   à fonte, pergunta à base de dados quais são novas e entrega cada uma ao
   avaliador. Só marca uma vaga como vista depois de o alerta ser entregue.
3. **`evaluation.py`** é onde se decide. É o ficheiro mais importante para
   perceber o comportamento do programa.
4. **`notify.py`** formata e envia a mensagem; **`store.py`** guarda o
   veredicto numa única escrita (`record_verdict`).

## A cascata de avaliação

Cada etapa rejeita a vaga ou passa-a à seguinte. A ordem existe para gastar
o mínimo: primeiro o que é grátis, depois uma chamada pequena ao modelo, e
só no fim o pedido ao LinkedIn e a leitura completa.

| # | Etapa | Custo | O que rejeita | Código do resultado |
|---|---|---|---|---|
| 1 | Pré-filtro | grátis | Palavras proibidas no título ou na empresa, "Senior"/"Lead" no título | `prefilter` |
| 2 | Anos declarados | grátis | Fontes que indicam mais anos do que o teu limite + `years_stretch` | `years` |
| 3 | Nota do título | 1 chamada pequena | Funções claramente fora do alvo | `title_low` |
| 4 | Descrição | 1 pedido ao LinkedIn | Nada (se falhar, alerta na mesma) | — |
| 5 | Língua | grátis | Descrições numa língua que não lês | `language` |
| 6 | Leitura completa | 1 chamada | Anos a mais, demasiado sénior, localização, cobertura abaixo de 60% | `years`, `not_junior`, `location`, `low_coverage` |

Uma vaga que passa tudo fica com `alerted`. O código do resultado é o que o
comando `/stats` conta.

### Três regras para não perder vagas

- **Cobertura mínima de 60%.** Estudos de recrutamento indicam que quem
  cumpre metade a 60% dos requisitos é chamado a entrevista quase tantas
  vezes como quem cumpre quase todos. Nos dados do Porto, a faixa 60–79%
  tinha tantas vagas boas como a de cima. Entre 50% e 59% vai para o resumo
  diário.
- **Anos com margem.** Uma vaga pode pedir até `years_stretch` (1) anos a
  mais do que o teu `max_years_experience` e ser enviada, com um aviso
  "⚠️ Asks for 4+ years…". Só acima disso é rejeitada.
- **Vagas republicadas avaliadas uma vez.** Os recrutadores voltam a
  publicar a mesma vaga com outro id de poucos em poucos dias. Uma vaga com
  a mesma empresa e o mesmo título (ignorando maiúsculas e marcas como
  "(m/f/d)") de outra avaliada nos últimos 30 dias herda esse veredicto, com
  o resultado `repost`, e nunca é enviada duas vezes. Antes, a mesma vaga
  podia dar 33% num dia e 83% no outro. A exceção são as vagas enviadas sem
  filtro por falha da API: essas são avaliadas a sério quando reaparecem.

### "Fail open"

Se algo externo falhar (o LinkedIn não devolve a descrição, a API do Claude
está em baixo, a chave é inválida), a vaga **é enviada na mesma**, com a
linha "⚠️ Not filtered" e o resultado `alerted_unfiltered`. A escolha é
deliberada: perder uma vaga boa por causa de uma falha técnica é pior do
que receber um alerta a mais. É por isto que, quando a API fica sem saldo,
começam a chegar vagas irrelevantes: é o sintoma de que o filtro está
desligado, não de que está a funcionar mal.

## O que o modelo faz e o que não faz

Toda a conversa com o Claude passa por **`llm.py`**, que tem um único
método: `parse()`. Recebe instruções, um texto e um esquema (uma classe
Pydantic) e devolve um objeto já validado. Não há JSON para interpretar à
mão.

Os textos das instruções e os esquemas estão em **`scoring.py`**. A leitura
completa devolve três blocos numa só chamada:

- **Correspondência.** A lista dos requisitos obrigatórios da vaga, cada um
  marcado como cumprido ou não, com a evidência do CV. A percentagem de
  cobertura **não é pedida ao modelo**: é calculada em Python a partir
  dessa lista (`DeepScoreResult.coverage_pct`). Assim consegues ver
  exatamente porque é que uma vaga deu 70%.
- **Probabilidade de entrevista.** Uma estimativa de 0 a 100, orientada por
  uma grelha escrita no prompt (5–15 se falha um requisito eliminatório,
  35–60 se cumpre a maioria, etc.). É uma estimativa do modelo, não uma taxa
  medida: aparece no alerta mas **nunca decide** se a vaga é enviada.
- **Dados de mercado.** Competências pedidas (obrigatórias ou não, e se o
  teu CV as mostra), tipo de função, senioridade, regime de trabalho, tipo
  de contrato e salário, quando indicado.

O modelo também responde a duas perguntas de sim ou não: se a vaga é
adequada a um perfil júnior (`is_junior_appropriate`) e se a localização
serve (`location_fit`), comparando a descrição com o campo
`location_preferences` do `profile.yaml`.

### Porque é que o candidato vai no "system prompt"

As instruções e o teu perfil/CV são iguais em todas as chamadas; só a vaga
muda. Por isso seguem no prompt de sistema, marcado como "cacheável", e a
vaga segue na mensagem do utilizador. O texto repetido é cobrado a um
décimo do preço. A função `candidate_context()` tem de produzir sempre o
mesmo texto, byte a byte, ou a cache deixa de funcionar.

## De onde vêm os dados do teu CV

- **`profile.yaml`** é escrito por ti: funções-alvo, limite de anos,
  línguas, preferências de localização, regras de exclusão. Exprime
  intenção, coisa que um CV não diz.
- **`profile.generated.yaml`** é gerado por `cv_ingest.py` a partir do
  `cv.pdf`: experiência, projetos, competências, certificações, formação.
  São factos. Volta a correr o comando sempre que alterares o CV.

O `cv_ingest.py` tem uma proteção contra invenções: campos por preencher no
CV (`[PREENCHER: ...]`) são substituídos por `[UNSPECIFIED]` antes de o
texto chegar ao modelo, e nunca são passados ao avaliador como se fossem
factos.

## A base de dados (`store.py`)

| Tabela | O que guarda |
|---|---|
| `seen_jobs` | Uma linha por vaga vista: título, empresa, resultado da avaliação, cobertura, probabilidade, requisitos |
| `job_insights` | Uma linha por vaga lida por completo: função, senioridade, regime, salário |
| `job_skills` | Uma linha por competência pedida numa vaga, e se o teu CV a tem |
| `seeded_searches` | Pesquisas que já fizeram a primeira execução silenciosa |

A primeira vez que uma pesquisa corre, as vagas existentes são registadas
sem alertar. É isso que impede uma avalanche de mensagens ao adicionar uma
localização ou ao apagar o `state.db`.

## Estatísticas e mercado (`commands.py`)

| Comando | Responde a |
|---|---|
| `/stats [dias]` | Quantas vagas vi, onde ficou cada uma, cobertura e probabilidade médias, melhores oportunidades |
| `/market [dias]` | Que competências o mercado pede (✅ tens, ❌ não tens), regimes de trabalho, senioridade, salários |
| `/gaps [dias]` | Que competências pedidas faltam no teu CV, por ordem de procura |

Como só são lidas por completo as vagas cujo título parece relevante, o
`/market` descreve o **teu mercado-alvo**, não o mercado todo. E "tens"
significa "está escrito no perfil ou no CV": uma competência que dominas
mas nunca escreveste aparece como lacuna até a acrescentares.

## Pesquisas e localização

O `config.yaml` tem duas listas: `locations` (onde procurar) e `sources`
(as palavras-chave de cada site). Cada palavra-chave é pesquisada em cada
localização; hoje isso dá onze pesquisas no LinkedIn, todas no Porto, em
todos os ciclos. Além dos títulos de IA, há termos mais largos ("Data
Scientist", "Python", "NLP", "Inteligência Artificial") que trazem vagas
que os títulos de IA não apanham; a nota do título descarta o ruído por
uma fração de cêntimo. O Wellfound está desligado (zero vagas no Porto),
mas o código continua disponível: basta voltar a pô-lo no `config.yaml`.
Os sites conhecidos estão registados em `sources/__init__.py`.

A pesquisa do LinkedIn sem login ignora os filtros de senioridade e de
trabalho remoto (foi testado: devolve as mesmas vagas com e sem eles). Por
isso essas duas decisões são tomadas a partir da própria vaga: o pré-filtro
trata das palavras no título e a leitura completa compara a descrição com
`location_preferences`.

Os pedidos ao LinkedIn são espaçados 2,5 segundos e, se a resposta for um
bloqueio (HTTP 429), há uma segunda tentativa após uma pausa. Uma pesquisa
que falhe três ciclos seguidos gera um aviso no Telegram.

## Onde mexer para…

| Quero… | Ficheiro |
|---|---|
| Mudar as palavras-chave ou acrescentar uma cidade | `config.yaml` |
| Mudar o que aceito (anos, línguas, localização, exclusões) | `profile.yaml` |
| Ser mais ou menos exigente | `min_coverage_pct` e `deep_score_threshold` em `config.yaml` |
| Usar um modelo mais forte na leitura completa | `deep_model` em `config.yaml` |
| Mudar o que o modelo avalia ou extrai | `DEEP_INSTRUCTIONS` e `_DeepSchema` em `scoring.py` |
| Mudar a ordem ou as regras dos filtros | `evaluation.py` |
| Mudar o aspeto do alerta | `notify.py` |
| Acrescentar um comando | `commands.py` (função `dispatch`) |
| Acrescentar um site de emprego | Nova classe em `sources/`, com `fetch()` e `close()` |

## Testes

```bash
uv run --extra dev pytest
```

Correm sem rede: o LinkedIn, o Telegram e o Claude são substituídos por
objetos falsos (`tests/conftest.py`). Os testes de `test_pipeline_evaluation.py`
são a melhor documentação do comportamento esperado da cascata, e os de
`test_pipeline.py` do ciclo (primeira execução silenciosa, avisos de falha,
resumo diário). O GitHub Actions corre os mesmos testes, o lint e uma
auditoria de dependências em cada pull request.
