# Benchmark: o ranking semântico adiciona valor sobre heurística?

`scripts/make_benchmark.py` e `scripts/benchmark.py`. A pergunta que ele responde:
com nomes que não dizem nada, só a evidência (HTTP/título/tech) separa o que
interessa, e **quanto** cada método recupera.

## Desenho

300 hosts. Todos os nomes saem do **mesmo** conjunto de templates inócuos
(`node-07`, `svc-3`, `cache-12`, `sqlproxy-66`...), e a classe é atribuída
aleatoriamente e depois sustentada por evidência. Ou seja: o nome não carrega
informação nenhuma sobre o rótulo, por construção, e um método que só lê nome
está medindo ruído. É isso que torna o teste honesto em vez de teatro.

Dois tiers de "interessante", porque é aí que a diferença aparece:

| tier | o que é | um regex acha? |
| --- | --- | --- |
| `obvious` | a evidência nomeia um produto privilegiado: Jenkins, GitLab, phpMyAdmin, Portainer, Kibana, Grafana, MinIO, Proxmox, RabbitMQ, Prometheus | sim |
| `subtle` | título genérico (`Console`, `Portal`, `Overview`, `Manage`) atrás de HTTP 401/403. Nenhum título contém palavra da lista de keywords, e o status é invisível para um regex sobre título+tech | não |

Cinco métodos: `random` (controle), `name heuristic` (`preprocess.pre_rank`,
só tokens do nome), `evidence keywords` (o regex ingênuo que a pessoa escreve
primeiro), `jev (names)` e `jev (names+evidence)`.

## Resultado (média de 3 conjuntos, 300 hosts cada, 90 com evidência)

```
metodo                   P@10%   R@10%   |   P@20%   R@20%
----------------------------------------------------------
random                   0.333   0.111   |   0.311   0.207
name heuristic           0.278   0.093   |   0.295   0.196
evidence keywords        0.978   0.326   |   0.689   0.459
jev (names)              0.200   0.067   |   0.233   0.156
jev (names+evidence)     0.967   0.322   |   0.956   0.637
```

O que dá pra afirmar com isso:

1. **Só nome é sorteio.** `jev (names)` fica no nível do `random`, e tem que
   ficar, porque o conjunto foi construído assim. Não é falha do modelo: sem
   evidência não existe sinal a extrair. Quem vende "rankeia subdomínio pelo
   nome" está vendendo ruído com cara de método.
2. **Com evidência, o ganho é real e cresce com o corte.** Em P@10% os dois
   empatam (0.967 x 0.978), mas em P@20% o regex desaba para 0.689 enquanto o
   Jev segura 0.956, com recall 0.637 contra 0.459. Traduzindo: a lista de
   palavras-chave esgota o que tem para achar em ~30% dos alvos; o Jev continua
   encontrando depois disso.
3. **No tier subtle, um regex acerta zero.** Por construção não há palavra para
   casar. O Jev também ia mal (2.3 de 31, abaixo do aleatório), e o motivo é a
   jaggedness documentada: ele responde a pergunta escrita. As `criteria` falavam
   de nomes e não mencionavam resposta gated. Depois de escrever a condição de
   verdade ("a title that is a generic management word on a page that answers
   HTTP 401 or 403"), subiu para 4.7 de 31, contra 2.7 do aleatório e 0.0 do
   regex:

```
tier subtle, acertos no top 30 (de 31 alvos)
random                2.7  ->  2.7
evidence keywords     0.0  ->  0.0
jev (names)           2.7  ->  2.3
jev (names+evidence)  2.3  ->  4.7
```

4. **Custo honesto de perder uma coisa para ganhar outra:** com o `criteria`
   mais longo, o recall no tier obvious caiu de 0.441 para 0.413. Aumentar a
   precisão de uma pergunta pode deslocar outra. O composto em P@20% melhorou,
   mas a oscilação é real e é por isso que existe medição.

## O que este benchmark NÃO prova

- O rótulo vem da evidência HTTP, então `evidence keywords` e as duas linhas do
  Jev são avaliadas em material da mesma família do rótulo. Isso mede "o
  pipeline recupera o que a evidência diz", **não** "acha vulnerabilidade real".
- O conjunto é sintético e o tier `subtle` é uma hipótese explícita minha sobre
  o que um humano consideraria interessante. Se essa hipótese estiver errada, o
  número do tier está errado junto.
- Em dado real não existe rótulo. Ali a única coisa que dá para medir é
  concordância e discordância entre métodos, e inspecionar os casos onde eles
  divergem. O que este script dá é o tamanho do efeito quando o rótulo existe.

## Reproduzir

```bash
.venv/bin/python scripts/make_benchmark.py --hosts 300 --gold 90 --seed 11 --out bench-11/
.venv/bin/python scripts/benchmark.py --bench bench-11/ --cache bench-11/cache.json \
    --out bench-11/results.json
```

Cada conjunto custa cerca de $0.027 (duas condições, 15 requests cada).
O script se recusa a rodar quando o filtro local descarta algum host, para
comparação não virar medição do filtro.
