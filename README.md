# handoff

*[Read in English](README.en.md)*

**A sessão morre no meio da tarefa. O transcript fica no disco. É dele que sai o handoff.**

`tools/handoff.py` transforma os transcripts JSONL que Claude Code, OpenAI
Codex e Gemini CLI já gravam em disco num único `HANDOFF.md`: um resumo
factual, ancorado no estado real do Git, que o próximo agente — ou você
mesmo, horas depois — usa para retomar o trabalho no mesmo checkout sem
perder o que já tinha sido feito.

Sem daemon. Sem servidor. Sem banco de dados. Sem dependência para instalar.
Um arquivo Python, apenas com a biblioteca padrão.

```
Claude trabalha por horas
     │
     ▼
sessão JSONL no disco       (Claude Code / Codex / Gemini CLI já gravam isso)
     │
     ▼
tools/handoff.py
     │
     ├── estado atual do Git
     ├── evidência selecionada do transcript
     └── consolidação opcional por LLM
     │
     ▼
HANDOFF.md
     │
     ▼
Codex lê e continua o trabalho
```

---

## Por quê

Para retomar uma sessão de IA depois de estourar a cota, fechar a janela ou
trocar de agente, as opções costumam ser duas:

- escrever e manter um `HANDOFF.md` na mão; ou
- montar uma plataforma de memória completa — servidor, MCP, banco vetorial,
  daemon.

Esta ferramenta fica no meio-termo. Ela lê os transcripts que os agentes **já
gravam** (sem hook, sem interceptação, sem estado novo para manter) e monta o
handoff sozinha, em segundos, tudo na sua máquina.

## Garantias

- **Somente leitura, sempre.** Nada em `~/.claude/`, `~/.codex/` ou
  `~/.gemini/` é editado, movido, renomeado ou apagado.
- **O Git manda.** A ordem de evidência é Git e filesystem primeiro, depois o
  transcript, e só então qualquer interpretação — nunca o contrário. Uma
  frase no transcript não prova que o código existe.
- **Funciona sem configuração e sem rede.** A consolidação por LLM é opcional
  e vem desligada; todo comando entrega um `HANDOFF.md` completo sem ela.
- **O transcript bruto nunca sai da máquina.** Uma sessão de mais de 100 MiB
  vira um Evidence Pack limitado, truncado e com segredos redigidos (algumas
  dezenas de KiB) antes de qualquer coisa ir para o disco ou para uma chamada
  de LLM — e dá para auditar esse payload (`ai-input-preview.md`) antes do
  envio.
- **Uma cópia serve todos os projetos.** Aponte o mesmo script para qualquer
  checkout com `--repo` ou `$HANDOFF_REPO`; nada é instalado por repositório.
- **Leitura em streaming.** Uma sessão real de 112 MiB do Codex é processada
  em ~2,4 s, com pico de ~31 MiB de heap.
- **Memória dos agentes é indexada, nunca ingerida.** O `HANDOFF.md` lista o
  que existe e quão recente é, mas nunca lê o conteúdo — é interpretação
  passada de outro agente, não evidência. ([detalhes](#memória-dos-agentes))

## Agentes suportados

| Agente          | Onde fica o transcript                                        | Status |
| --------------- | -------------------------------------------------------------- | ------ |
| Claude Code     | `~/.claude/projects/<slug>/<session-uuid>.jsonl`              | Validado contra sessões reais em disco |
| OpenAI Codex    | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`                | Validado contra uma sessão real de 112 MiB (~2,4 s, pico de ~31 MiB de heap) |
| Gemini CLI      | `~/.gemini/tmp/<project-slug>/chats/session-*.jsonl`          | Escrito a partir do código-fonte não minificado que o próprio CLI distribui e de um probe real parcial; ainda não rodou contra uma sessão completa e autenticada — veja [docs/handoff.md](docs/handoff.md#gemini-cli) |

Adicionar outro agente é escrever mais um adapter que emita o mesmo evento
normalizado; o resto da ferramenta não muda.

---

## Requisitos

- Python 3 (só a biblioteca padrão — nada de `pip install`)
- Git
- Claude Code, Codex e/ou Gemini CLI, conforme os agentes que você usa

## Instalação

```bash
git clone https://github.com/gabrielsouto/handoff.git
```

Guarde essa pasta onde quiser: ela não precisa ficar dentro dos projetos em
que atua. Opcionalmente, rode `init` uma vez em cada repositório, de dentro
dele:

```bash
cd /caminho/do/seu/projeto
python3 /caminho/do/handoff/tools/handoff.py init
```

O `init` cria `.handoff/`, grava um `.handoff/config.json` padrão, adiciona
`/HANDOFF.md` e `/.handoff/` ao `.git/info/exclude` (nunca ao seu
`.gitignore`, e de forma idempotente) e acrescenta uma seção **Agent
handoff** ao `AGENTS.md` caso ele já exista — nunca cria um do zero.

Há três wrappers finos no repositório para encurtar a chamada de onde quer
que você guarde a ferramenta: `./handoff` (sh), `handoff.cmd` e
`handoff.ps1`. Todos repassam os argumentos para `tools/handoff.py`:

```bash
./handoff recover claude --repo /caminho/do/seu/projeto
```

---

## Início rápido

```bash
python3 tools/handoff.py doctor       # o que a ferramenta enxerga nesta máquina
python3 tools/handoff.py status       # resumo do repositório e das sessões
```

**Troca planejada de agente**, quando você vai parar e entregar de propósito:

```bash
python3 tools/handoff.py snapshot claude --ai
```

Depois, no Codex:

> Leia AGENTS.md e HANDOFF.md e continue o trabalho atual. Inspecione o Git e
> o código antes de fazer qualquer mudança.

**Emergência**, quando a sessão morreu e não deu tempo de pedir um resumo:

```bash
python3 tools/handoff.py recover claude --ai
```

Sem endpoint de LLM configurado, basta omitir o `--ai`: o `HANDOFF.md` sai
completo do mesmo jeito, apenas sem a síntese do modelo nas dez seções de
análise.

---

## Comandos

```bash
python3 tools/handoff.py init                  # uma vez por repositório
python3 tools/handoff.py doctor                # diagnóstico completo
python3 tools/handoff.py status                # resumo curto
python3 tools/handoff.py snapshot claude       # entrega planejada
python3 tools/handoff.py snapshot codex  --ai
python3 tools/handoff.py recover  claude --ai  # a sessão morreu
python3 tools/handoff.py recover  gemini
python3 tools/handoff.py consolidate --ai      # refaz o resumo, mesmo snapshot
python3 tools/handoff.py history               # lista os handoffs arquivados
python3 tools/handoff.py show                  # imprime o HANDOFF.md
```

`snapshot` e `recover` rodam exatamente o mesmo pipeline; os dois nomes
existem para que a intenção fique óbvia na hora do uso. Todo comando aceita
`--repo PATH` e `--verbose`, antes ou depois do subcomando; `snapshot` e
`recover` aceitam também `--session ID`, `--session-file PATH`, `--ai` e
`--dry-run`.

```
$ python3 tools/handoff.py --help
usage: handoff.py [-h] [--repo PATH] [--verbose]
                  {init,doctor,status,snapshot,recover,consolidate,history,show} ...

Generate a handoff between Claude Code, OpenAI Codex and Gemini CLI from the
transcripts they already write.

positional arguments:
  {init,doctor,status,snapshot,recover,consolidate,history,show}
    init                create .handoff/, config and git exclude rules
    doctor              diagnose repository, agents and AI endpoint
    status              short summary of repo, handoff and sessions
    snapshot            capture the current state (normal use)
    recover             rebuild a handoff after a session died
    consolidate         rebuild HANDOFF.md from the existing snapshot
    history             list archived handoffs
    show                print the current HANDOFF.md
```

*(a saída do `--help` é em inglês, como toda a interface da ferramenta)*

Códigos de saída: `0` sucesso, `1` erro geral, `2` uso inválido, `3` sessão
não encontrada, `4` erro de Git, `5` erro de LLM. O `--ai` nunca retorna 5:
se o LLM falhar, ele cai para o handoff determinístico e termina com 0.

---

## Como a sessão é escolhida

Em ordem de força, o diretório de trabalho **gravado dentro da própria
sessão** (Claude e Codex trazem no começo do transcript; o Gemini, no arquivo
marcador `.project_root`) vence a **pista pelo nome do diretório**, que por
sua vez vence a ausência de qualquer evidência. A escolha automática pega a
sessão confirmada mais recente, depois a mais recente por pista, e **nunca**
assume em silêncio uma sessão de outro checkout: se todas as candidatas forem
de outro projeto, o comando recusa e pede uma escolha explícita.

```bash
python3 tools/handoff.py recover claude --session 4114df3c-a38c-49c9
python3 tools/handoff.py recover codex  --session-file ~/.codex/sessions/2026/09/09/rollout-....jsonl
```

## O que é gravado

```
<seu-projeto>/
├── HANDOFF.md                      # o que o próximo agente lê
└── .handoff/
    ├── config.json                 # configuração local (criada pelo `init`, nunca sobrescrita)
    ├── session.json                # metadados do snapshot + SHA256 do transcript, em streaming
    ├── git-state.txt               # retrato legível do Git
    ├── conversation-tail.md        # evidência selecionada do transcript, truncada e rotulada
    ├── ai-input-preview.md         # exatamente o que seria enviado ao LLM (só com --ai)
    └── history/
        └── 2026-09-09_174530_claude_4114df3c.md
```

Tudo isso fica local e fora do Git, via `.git/info/exclude`. O `HANDOFF.md`
nunca carrega um diff inteiro: o próximo agente está no mesmo checkout e pode
rodar `git diff`. O handoff é um mapa, não uma cópia.

Uma seção **Agent Memory** também é sempre incluída — veja
[Memória dos agentes](#memória-dos-agentes) abaixo.

## O LLM opcional

Por padrão não existe integração com LLM nenhuma, e nada aqui abre um socket.
Para ligar, preencha `base_url` e `model` em `.handoff/config.json` (qualquer
endpoint `/v1/chat/completions` compatível com a API da OpenAI) ou exporte
`HANDOFF_LLM_BASE_URL`; para desligar de vez, use `"enabled": false`.

Passar `--ai` é sempre seguro: sem nada configurado, ou com o endpoint fora do
ar, ele explica o motivo e cai para o handoff determinístico em vez de
derrubar o comando inteiro.

Mesmo com endpoint configurado, o modelo continua sem ver o transcript bruto.
Uma única passagem em streaming monta antes o Evidence Pack — limitado,
truncado e com segredos redigidos — e o `--dry-run` mostra o payload exato
antes de qualquer envio.

## Segurança

Antes de gravar ou enviar qualquer coisa, uma redação best-effort passa por
todo o conteúdo: cabeçalhos `Authorization` e `Cookie`, atribuições de
`api_key=`, `token=`, `password=` e `secret=`, chaves `sk-…`, `sk-ant-…` e
`ghp_…`, padrões de chave da AWS e do Google, JWTs e blocos `PRIVATE KEY`
viram `[REDACTED]`. Arquivos com cara de credencial (`.env`, `auth.json`,
`id_rsa`, `*.pem`…) têm o conteúdo descartado por inteiro, em vez de
redigido.

É defesa em profundidade, não garantia de DLP. Na primeira vez que apontar a
ferramenta para um endpoint novo, leia o `.handoff/ai-input-preview.md`.

## Memória dos agentes

Claude Code, Codex e Gemini CLI guardam alguma forma de memória ao lado dos
transcripts: o Claude tem um diretório `memory/` por projeto (arquivos de
fato mais um índice `MEMORY.md`), o Codex mantém dois SQLite globais
(`memories_1.sqlite`, `goals_1.sqlite`) e o Gemini pode carregar um
`memoryScratchpad` dentro de uma sessão. Os arquivos de contexto na raiz do
repo (`CLAUDE.md`, `GEMINI.md`) entram na mesma categoria.

A ferramenta **indexa, não ingere.** A ordem de evidência (Git > transcript >
interpretação) existe justamente para desconfiar disso: memória é a
interpretação passada de um agente, congelada e desconectada do que a
produziu, sem nenhum jeito aqui de saber se ainda vale. Então:

- `doctor` e todo `HANDOFF.md` listam o que existe, onde, e quão recente é —
  contagem de arquivos, tamanhos, datas, status de rastreado/não-rastreado no
  Git;
- nada disso é aberto, parseado ou misturado nas dez seções de análise;
- os SQLite do Codex em particular nunca são abertos — o schema é interno e
  não documentado, então é só presença e tamanho, via `stat()`.

A única exceção deliberada é o `memoryScratchpad` do Gemini: ele já vive
dentro do arquivo de sessão que a ferramenta parseia, é escopo de uma sessão
só (não um store global entre projetos), e o código-fonte do próprio CLI
rastreia um sinal explícito de obsolescência — qualquer mensagem ou rewind
gravado depois do último save do scratchpad o marca como stale. Com base
nisso, ele aparece como um evento rotulado no `conversation-tail.md` (marcado
como "possibly stale" ou "fresh", e explicitamente como não-confirmado), o
mesmo tratamento já dado aos resumos de compactação `<state_snapshot>`.

Puxar o **conteúdo** da memória para dentro do handoff seria uma decisão
separada e explicitamente opt-in — não existe nada assim aqui hoje. Detalhes
em [docs/handoff.md](docs/handoff.md#10-agent-memory).

---

## Testes

```bash
python3 -m py_compile tools/handoff.py
python3 -m unittest discover -s tests -t .
```

156 testes, só biblioteca padrão (`unittest` mais um stub local de
`http.server` para o cliente de LLM). As fixtures são sintéticas, montadas a
partir dos formatos de registro observados em transcripts reais — nenhuma
sessão real é commitada.

## Documentação

[docs/handoff.md](docs/handoff.md) é a referência completa, em inglês: os
formatos exatos de registro JSONL dos três agentes (incluindo a semântica de
patch-log `$set`/`$rewindTo` do Gemini), as regras de associação entre sessão
e repositório, cada arquivo gravado, o schema de configuração e uma seção de
troubleshooting.

## Limitações conhecidas

- O adapter do Gemini foi escrito a partir do código-fonte que o próprio CLI
  distribui e de uma sessão que falhou antes da primeira resposta do modelo,
  não de uma conversa completa e autenticada. Até rodar contra uma sessão
  real, considere-o menos maduro que os de Claude e Codex.
- A redação de segredos é heurística, não exaustiva.
- A integração com `AGENTS.md` só acrescenta a um arquivo existente; nunca
  cria um.

## Fora do escopo, por decisão

Embeddings vetoriais, busca semântica no histórico, servidor HTTP, MCP,
interface web, multiusuário, sincronização entre máquinas, banco de dados,
watchers, hooks automáticos nos agentes, plugin de VS Code, qualquer serviço
em segundo plano. Quando confiabilidade e feature nova entram em conflito,
confiabilidade vence.

## Licença

[MIT](LICENSE) © 2026 Gabriel Souto.
