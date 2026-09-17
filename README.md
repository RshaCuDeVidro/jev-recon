# jev-recon

Triagem de subdomínios para pesquisa de segurança usando o **Jev** (TypeSafe AI)
como camada de decisão probabilística, e código Python comum decidindo o ranking.

O objetivo não é pedir para um LLM "dizer o que é interessante". É fazer muitas
perguntas pequenas e independentes sobre cada asset, transformar as respostas em
números, e compor a prioridade com aritmética que você controla.

```
LLM tradicional:  "me diga qual subdomínio é interessante"      (texto, instável, caro de iterar)
Jev:              "responda 7 perguntas sim/não sobre este asset" (probabilidade por pergunta)
Código Python:    priority = soma ponderada + ordenação           (seu, auditável, versionado)
```

---

## Estrutura

```
jev-recon/
├── jev_recon/
│   ├── cli.py            CLI, orquestração, saída no terminal
│   ├── config.py         .env, API key, base URL, modelo
│   ├── cache.py          cache de respostas por hash da request (re-rankear de graça)
│   ├── preprocess.py     filtros locais baratos, fatos extraídos por código, agrupamento
│   ├── signals.py        as perguntas (Choice / Score / Noul) e o corpo da request
│   ├── jev.py            cliente HTTP assíncrono: batching, concorrência, retries
│   └── rank.py           pesos, prioridade, ordenação (Python puro)
├── scripts/
│   ├── gen_sample.py             gera uma lista sintética grande para demo/carga
│   └── mock_typesafe_server.py   API falsa compatível, para demo e testes sem key
├── tests/                40 testes (unittest, sem dependências extras)
├── examples/             entrada, saída e logs de execuções reais
├── requirements.txt      httpx
├── pyproject.toml
└── .env.example
```

Dependência de runtime: **httpx**. Sem framework, sem banco, sem frontend.

## Instalação

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # ou: .venv/bin/pip install -e .
cp .env.example .env                             # e preencha a chave
```

`.env`:

```ini
TYPESAFE_API_KEY=ts_...
# opcionais
TYPESAFE_BASE_URL=https://api.typesafe.ai
TYPESAFE_DEFAULT_MODEL=jev-latest
```

Chave em https://console.typesafe.ai/settings/keys

## Uso

```bash
.venv/bin/python -m jev_recon subdomains.txt \
  --batch-size 20 \
  --concurrency 16 \
  --threshold 0.70 \
  --output interesting.json
```

Ou, depois de `pip install -e .`, o console script:

```bash
jev-recon subdomains.txt --concurrency 20 --explain 10
```

Exemplo de execução real (50.000 subdomínios, 24.707 candidatos, concurrency 16,
rodando contra o mock local da seção 7, que é de onde vêm os números de `usage`):
`examples/console-output.txt`

```
50000 subdomains
        ↓
24707 candidates  (24609 removed: duplicate 20974, invalid_syntax 546, ...)
        ↓
Jev analysis  (1236 requests · batch 20 · concurrency 16 · 9.1s)
        ↓
6 high-interest assets

TOP ASSETS
────────────────────────────────────
0.84  admin.graphql.massive-dynamic.io
0.76  dashboard.graphql.acme-corp.net
0.76  graphql.kibana.stark-industries.com
0.76  grafana.rpc.piedpiper.io
0.74  jenkins.auth.massive-dynamic.io
0.74  jenkins.scada.veridian.net

RUN
────────────────────────────────────
  scored         24707/24707   incomplete 0   unscored 0
  requests       1236 sent   retries 0   rate-limit pauses 0
  tokens         in 26,076,079  out 526,263   est. cost $1.0952   (jev-1.13.0)
```

`--dry-run` monta as requests e mostra o plano, sem gastar nada:

```
  requests planned      14
  candidates per request     20
  questions per request 142
  est. input tokens     374,065
  est. cost             $0.0157   (@ $0.042/Mtok, output free)
```

### Entrada

* `.txt`: um hostname por linha. Aceita lixo: `https://`, `:porta`, `user@`,
  `*.` (wildcard), `#` comentários, `host 1.2.3.4`, maiúsculas, IPs, URLs com path.
* `.json`: lista de strings ou de objetos com `hostname`.
* `.jsonl`: um objeto por linha.
* `--meta metadata.json`: enriquecimento opcional por hostname
  (`resolved_ips`, `http_status`, `title`, `server`, `ports`, `technologies`).
  Veja `examples/metadata.sample.json`. Se o input já for JSON com esses campos,
  eles entram automaticamente.

### Saída

`interesting.json` (assets com `priority >= --threshold`, ordenados):

```json
[
  {
    "hostname": "admin-api.example.com",
    "priority": 0.94,
    "signals": {
      "likely_production": 0.98,
      "likely_sensitive": 0.96,
      "likely_internal": 0.32,
      "likely_staging": 0.08,
      "likely_admin": 0.93,
      "likely_api": 0.97,
      "interesting_for_security_research": 0.95
    },
    "relative_pick": 0.215,
    "weights_used": {"production": 0.25, "sensitive": 0.25, "admin": 0.15, "api": 0.15, "interesting": 0.2},
    "missing_signals": [],
    "batch": {"id": 45, "research_yield": 2.0, "yield_confidence": 0.82, "batch_error": null},
    "pre": {"name_tokens": ["admin", "api"], "env_token": null, "privileged_labels": ["admin", "api"], "pre_rank": 2.6},
    "metadata": {},
    "incomplete": false
  }
]
```

* `signals`: as probabilidades (Noul) por pergunta, 0 a 1. `null` quando a
  resposta não veio.
* `relative_pick`: a pergunta `Choice` do lote. As probabilidades de uma Choice
  somam 1, então isso é **pressão relativa dentro do lote**, usado só como
  desempate. Nunca é lido como sinal absoluto.
* `batch.research_yield`: a pergunta `Score` do lote (0, 1 ou 2), com a
  `confidence` da própria resposta. Serve para gate: lote sem nada interessante
  não precisa de análise caríssima depois.
* `pre`: fatos calculados por código (tokens do nome, ambiente detectado,
  label privilegiado), não por modelo.
* `incomplete`: `true` quando alguma pergunta ficou sem resposta.

Extras: `--all-output all.json` (tudo, não só os high-interest),
`--report report.json` (contagens, preprocess, batching, pesos, uso, erros).

### Cache: re-rankear não se paga duas vezes

```bash
.venv/bin/jev-recon lista.txt --cache jev-cache.json --output v1.json
.venv/bin/jev-recon lista.txt --cache jev-cache.json \
  --weights 'sensitive=0.4,admin=0.3,interesting=0.3' --output v2.json
```

A chave do cache é o hash do corpo da request (modelo + state + perguntas), então
um hit só acontece quando o pedido é idêntico. A segunda rodada acima sai com
`requests 0 sent   cache 3 hits   est. cost $0.0000` em menos de um segundo, e o
ranking muda só por causa dos pesos. Respostas vindas do cache **não** são
somadas em `input_tokens`, porque já foram cobradas quando entraram. Apague o
arquivo para forçar respostas novas.

## 1. Pré-processamento local (antes de gastar API)

`preprocess.py`, tudo offline:

| ação | como |
| --- | --- |
| normalizar | tira scheme, path, query, porta, `user@`, `*.`, ponto final, baixa a caixa |
| eliminar duplicados | set, depois de normalizar (`API.x.com` == `api.x.com`) |
| validar | label 1-63, não começa/termina com hífen, TLD alfabético, total <= 253 |
| descartar lixo | IP literal, hostname de um label só, sufixo reservado (`.invalid`, `.test`), wildcard, labels placeholder (`foo`, `asdf`, `dummy`) |
| anotar (não descartar) | token de ambiente (`dev`, `test`, `qa`), label privilegiado (`admin`, `api`, `vpn`), label de ruído (`cdn`, `static`), profundidade, `pre_rank` |
| agrupar | `--max-per-parent N` limita quantos assets ficam por domínio registrado, mantendo os de maior `pre_rank` |

Ponto importante de projeto: **`test` e `dev` não são descartados**, são
anotados. Assets de staging quebrado são exatamente onde bug bounty costuma
render. Só o que nunca foi host real é jogado fora.

`pre_rank` é a heurística local (soma de tokens conhecidos). Ela serve para
ordenar dentro de um cap e para `--limit`. Nunca é a decisão final.

## 2. O que é enviado ao Jev

Uma request por lote. Estado e perguntas, no formato documentado em
`POST /v1/systemone`:

```json
{
  "model": "jev-latest",
  "state": {
    "task": "Each entry in `candidates` is a DNS name ...",
    "candidates": [
      {
        "id": "c00005",
        "hostname": "dev-api.example.com",
        "labels": ["dev-api", "example", "com"],
        "subdomain_depth": 3,
        "code_extracted": {
          "registered_parent": "example.com",
          "name_tokens": ["dev", "api"],
          "env_token": "development",
          "privileged_labels": ["api"],
          "noise_labels": [],
          "pre_rank": 1.15
        },
        "http_status": 403,
        "title": "403 Forbidden",
        "server": "gunicorn",
        "technologies": ["Python", "FastAPI"]
      }
    ]
  },
  "questions": {
    "c0::likely_admin": {
      "type": "noul",
      "instructions": "Does `candidates[0]` look like it exposes an administrative or privileged management interface?",
      "criteria": {"true": "...", "false": "..."}
    },
    "batch::top_pick": {
      "type": "choice",
      "instructions": "Exactly one candidate in `candidates` is the best first target ...",
      "criteria": {"candidates[0]": "dev-api.example.com", "candidates[1]": "..."}
    },
    "batch::research_yield": {
      "type": "score",
      "instructions": "How much does this batch of `candidates` contain at least one asset ...",
      "criteria": ["Little of interest ...", "Mixed ...", "Several plausible leads ..."]
    }
  }
}
```

Cada candidato gera 7 perguntas `Noul` (uma por sinal), mais 2 perguntas de lote.
Uma request de 20 candidatos carrega 142 perguntas.

Os três tipos são usados onde fazem sentido:

* **Noul** para os sinais por asset. O valor é absoluto e independente: sete
  nouls podem voltar todos baixos, e isso é informação. (Noul não tem
  `confidence` separada, por definição do próprio Jev: a probabilidade é o sinal.)
* **Choice** para "qual deste lote você olharia primeiro". Probabilidades somam 1,
  então serve como ranking relativo / desempate.
* **Score** para o rendimento do lote, com níveis ordenados e `confidence`, usado
  como gate.

Cada pergunta é literal e tem `criteria` nos dois lados (`true` e `false`)
explicando o que conta como sim e como não. Isso segue a orientação da doc
("write the exact condition in the instructions") porque o Jev responde a
pergunta escrita, não a que você quis escrever.

## 3. Batching e concorrência (como foi implementado)

Não existe endpoint de batch na API da TypeSafe. **Batching acontece dentro de
uma request**: você coloca vários itens no `state` e faz uma pergunta por item,
na mesma chamada. Todas as perguntas de uma request são avaliadas em paralelo,
então 20 candidatos x 7 sinais custam 1 round trip e 142 perguntas, não 20
round trips.

Isso dá dois botões independentes:

| botão | controla | efeito |
| --- | --- | --- |
| `--batch-size` | candidatos por request | menos requests, latência menor; lote grande demais dilui a atenção do modelo |
| `--concurrency` | requests em voo (asyncio + `Semaphore`) | throughput; 20 já satura o rate limit de requests/min |

Implementação em `jev.py`:

1. `effective_batch_size()` reduz o lote, antes de qualquer chamada, para caber
   nos limites documentados (`--max-questions-per-request`, default 220, e
   `--max-request-tokens`, default 24.000, contra o teto documentado de 32k para
   `state` + a pergunta mais longa e 64k no total).
2. `split_batches()` fatia os candidatos.
3. `asyncio.gather` + `asyncio.Semaphore(concurrency)`, com um `httpx.AsyncClient`
   compartilhado e `Limits(max_connections=concurrency)`.
4. Nenhum lote é descartado em silêncio: lote que falha gera assets com
   `priority: null`, `incomplete: true` e `batch.batch_error` preenchido, e o
   processo sai com código 1.

Nunca são disparadas 500.000 requests simultâneas. O teto é `--concurrency`, e o
default é 8.

**Onde o dinheiro vai** (medido, `--dry-run`): numa request de 20 candidatos,
`state` = 2.422 tokens (121 por candidato) e `questions` = 18.753 tokens (132 por
pergunta). O texto de `criteria` domina, e ele é reenviado uma vez por candidato,
porque cada pergunta é sobre um item. Conclusão prática: ~1.050 tokens por asset,
cerca de $0.000044 por asset a $0.042/Mtok de input (output é grátis), ou seja
~$1.10 por 25.000 assets. Baixar `--signals` corta o custo proporcionalmente
(3 sinais em vez de 7 = ~45% do custo). Aumentar `--batch-size` quase não muda o
custo; ele é um botão de latência.

## 4. Ranking em Python

`rank.py`, sem modelo nenhum no meio:

```python
priority = (
    production * 0.25 +
    sensitive  * 0.25 +
    admin      * 0.15 +
    api        * 0.15 +
    interesting* 0.20
)
```

* Denominador = soma de todos os pesos configurados. Sinal que não veio **derruba**
  o score em vez de ser renormalizado para fora: asset sem medição não pode
  parecer mais interessante que asset medido.
* Pesos são configuráveis e normalizados: `--weights 'production=0.3,staging=-0.1'`.
  Peso negativo penaliza staging, coisa que o default não faz (o default ignora
  `internal` e `staging`, exatamente como na fórmula acima).
* Ordenação: prioridade, depois `relative_pick` do lote, depois nome.
* `--threshold` decide o que vira "high-interest" (default 0.70).

### O que os pesos default fazem com dados reais

Medido numa amostra real: `likely_production` volta **baixo** (0.24 a 0.48) para
infra interna, porque a pergunta é literal sobre servir usuários ou clientes
reais, e um bastion ou um Postgres interno não serve cliente nenhum. Com os
pesos default, isso segura o topo da lista em ~0.63 e empurra justamente esses
assets para baixo:

```
0.63  us-east-1.argocd.globex.com.br     prod 0.39  sens 0.80  admin 0.89
0.62  eu-west-1.auth.globex.com.br       prod 0.38  sens 0.84  admin 0.81
0.60  eu-west-1.bastion.tyrell-corp.com  prod 0.27  sens 0.91  admin 0.86
```

Se o seu alvo é painel interno, e não superfície pública, tire peso de
`production` e ponha em `sensitive`/`admin`:

```
--weights 'sensitive=0.35,admin=0.30,api=0.10,interesting=0.25'

0.79  eu-west-1.bastion.tyrell-corp.com  prod 0.26  sens 0.91  admin 0.88
0.75  ap-south-1.db.tyrell-corp.com      prod 0.24  sens 0.88  admin 0.81
0.74  us-east-1.argocd.globex.com.br     prod 0.46  sens 0.81  admin 0.88
0.73  us-east-1.rdp.acme-corp.net        prod 0.40  sens 0.85  admin 0.81
```

Com `--cache` a segunda rodada custa zero e sai em menos de um segundo, então
calibrar peso é barato: roda uma vez, re-pesa quantas vezes quiser.

## 5. Erros e rate limits

Sem depender de SDK: o cliente HTTP implementa o que a doc recomenda
("retry with exponential backoff").

| situação | tratamento |
| --- | --- |
| `429 Too Many Requests` | backoff exponencial com jitter, honrando `retry-after` (segundos ou data HTTP). A pausa é **global**: um `_resume_at` compartilhado faz todos os workers esperarem, senão a concorrência recria o 429 |
| `529 Overloaded` / `5xx` / `408` / timeout / erro de rede | retry com backoff até `--max-retries` (default 4) |
| `422 Unprocessable Entity` | lote grande demais ou pergunta inválida. O cliente **divide o lote na metade e tenta de novo** (até 3 níveis), em vez de perder candidatos |
| `401` | erro fatal, com mensagem apontando a `TYPESAFE_API_KEY`; exit 2 |
| lote falhou de vez | assets saem com `priority: null`, `incomplete: true`, `batch_error`, aviso no stderr, exit 1. `--strict` aborta na primeira falha |
| `answers` ausente ou `type` diferente do esperado | tratado como sinal `null`, não como zero |
| nenhuma request | `--dry-run` mostra o plano completo e não chama a API |

Evidência real dos caminhos de erro em `examples/fault-injection.txt`: 429
honrando `retry-after`, 503 com retry, 422 dividindo lote, 297 splits, e ainda
assim 1.983/1.983 assets pontuados com 0 lotes perdidos.

## 6. Limites conhecidos do Jev (e como o projeto lida)

Da página de jaggedness do `jev-1.13`, aplicado aqui:

* **Leitura literal**: criteria explícito nos dois lados de cada Noul.
* **Não conta, não faz matemática**: nada de "quantos subdomínios têm X".
  Recorrência, ordenação e pesos ficam em código.
* **Context rot**: o `state` carrega só o necessário (nome, labels, fatos,
  metadata que você forneceu). `--batch-size` controla o quanto de material não
  relacionado vai junto: lote menor tende a mais precisão, lote maior a menos
  latência. Teste com o seu dado.
* **Conteúdo adversarial**: `title` e outros campos vêm de fora e são dados, não
  instruções. O Jev não trata state como hostil por padrão; se você roda isso em
  assets de terceiros, trate `title`/`technologies` como entrada não confiável e
  valide o resultado antes de agir.
* **Indireção**: as perguntas são uma decisão cada, e apontam o caminho no state
  (`candidates[3]`), em vez de esconder vários julgamentos numa pergunta.
* Pergunta holística (`interesting_for_security_research`) é mantida a pedido,
  mas o sinal que carrega a decisão é a composição dos outros: ela é a pergunta
  que um LLM tradicional responderia, e está aqui para ser comparada.

## 7. Testes e demo sem API key

```bash
.venv/bin/python -m unittest discover -s tests     # 40 testes, sem dependências extras
.venv/bin/pip install -e '.[dev]' && .venv/bin/python -m pytest -q
```

A suíte roda igual sob pytest e unittest, de qualquer diretório e sem instalar o
pacote (`tests/_bootstrap.py` põe a raiz e `scripts/` no `sys.path`; o unittest
não lê `conftest.py` e o pytest puro não adiciona o cwd).

Os testes cobrem o pré-processamento, a aritmética do ranking, e ponta a ponta
contra o mock: uma request por lote, concorrência (12 lotes com latência
artificial terminam bem antes de serial), 429 com `retry-after`, 503 com retry,
422 dividindo lote, chave inválida, endpoint morto (exit 1, assets preservados
como `incomplete`), pesos customizados, `--dry-run` e os arquivos de saída.

`scripts/mock_typesafe_server.py` responde no formato documentado, com injeção de
falha:

```bash
# terminal 1
.venv/bin/python scripts/mock_typesafe_server.py --port 8712

# terminal 2
.venv/bin/python scripts/gen_sample.py 50000 > subdomains.txt
.venv/bin/python -m jev_recon subdomains.txt \
  --base-url http://127.0.0.1:8712 --api-key mock-key-0123456789abcdef \
  --threshold 0.70 --explain 8
```

O mock não é um modelo: ele pontua por tokens do nome e um hash. Serve para
exercitar o pipeline inteiro (e os erros) sem chave e sem custo. Os números de
`usage` mostrados nas demos vêm da contabilidade do próprio mock; em produção,
leia `usage` da resposta real e o `model` que respondeu (o alias
`jev-latest` se move entre versões; o código registra isso no `--report`).

Flags de injeção: `--rate-limit-every N`, `--retry-after S`, `--fail-every N`,
`--reject-over-questions N`, `--latency S`.

## 8. Referências

* API: https://docs.typesafe.ai/api
* Primitivas (Choice, Score, Noul): https://docs.typesafe.ai/primitives
* State: https://docs.typesafe.ai/concepts/state
* Composite scoring: https://docs.typesafe.ai/patterns/composite-scoring
* Speculative fan-out: https://docs.typesafe.ai/patterns/fan-out
* Re-ranking: https://docs.typesafe.ai/cookbooks/rerank_typesafe
* Batching / parallel questions: https://docs.typesafe.ai/cookbooks/parallel_questions
* Jaggedness do Jev 1.13: https://docs.typesafe.ai/model-jaggedness/jev-1.13
* Modelos e limites: https://docs.typesafe.ai/models
