# handoff

*[Read in English](README.en.md)*

**Uma sessão morre no meio da tarefa. O transcript continua no disco. Isso aqui lê ele.**

`tools/handoff.py` transforma os transcripts JSONL que Claude Code, OpenAI
Codex e Gemini CLI já gravam em um único `HANDOFF.md` — um resumo factual e
consciente do Git que o próximo agente (ou o próximo você) pode retomar no
mesmo checkout, sem perder o que já foi feito.

Sem daemon. Sem servidor. Sem banco de dados. Sem dependências para instalar.
Um único arquivo Python, só biblioteca padrão.

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
     └── consolidação opcional via LLM
     │
     ▼
HANDOFF.md
     │
     ▼
Codex lê e continua o trabalho
```

---

## Por quê

As opções usuais para continuar uma sessão de IA depois de bater a cota, a
janela fechar, ou trocar de agente são:

- um `HANDOFF.md` que você escreve e atualiza manualmente, ou
- uma plataforma de memória completa: servidor, MCP, banco vetorial, daemon.

Isso aqui fica no meio do caminho. Lê os transcripts que os agentes **já
gravam** — sem hooks, sem interceptação, sem estado novo para manter — e
transforma tudo em um handoff automaticamente, em segundos, inteiramente na
sua máquina.

## O que é garantido

- **Somente leitura, sempre.** Nada em `~/.claude/`, `~/.codex/` ou
  `~/.gemini/` é editado, movido, renomeado ou apagado em nenhuma hipótese.
- **O Git é a fonte da verdade, sempre.** Ordem de evidência: Git e o
  filesystem, depois o transcript real, depois qualquer interpretação —
  nunca ao contrário. Uma frase no transcript não é prova de que o código
  existe.
- **Funciona com zero configuração e zero acesso à rede.** A consolidação
  opcional via LLM vem desligada por padrão; todo comando produz um
  `HANDOFF.md` completo e útil sem ela.
- **Nunca envia o transcript bruto para lugar nenhum.** Uma sessão de mais de
  100 MiB é reduzida a um Evidence Pack limitado e com segredos redigidos
  (algumas dezenas de KiB) antes de qualquer coisa ser escrita em disco ou
  cogitada para uma chamada de LLM — e dá para inspecionar exatamente esse
  payload (`ai-input-preview.md`) antes de ele ser enviado.
- **Uma cópia só atende qualquer projeto.** Aponte o mesmo script para
  qualquer checkout com `--repo` ou `$HANDOFF_REPO`; não precisa instalar
  nada por repositório.
- **Processa em streaming, nunca carrega um transcript inteiro na memória.**
  Uma sessão real de 112 MiB do Codex é processada em ~2,4 s com pico de
  ~31 MiB de heap Python.

## Agentes suportados

| Agente          | Onde fica o transcript                                        | Status |
| --------------- | -------------------------------------------------------------- | ------ |
| Claude Code     | `~/.claude/projects/<slug>/<session-uuid>.jsonl`              | Validado contra sessões reais no disco |
| OpenAI Codex    | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`                | Validado contra uma sessão real de 112 MiB (~2,4 s para processar, ~31 MiB de pico de heap) |
| Gemini CLI      | `~/.gemini/tmp/<project-slug>/chats/session-*.jsonl`          | Construído a partir do código-fonte (não minificado) do próprio CLI + um probe real parcial; ainda não rodado contra uma sessão completa e autenticada — veja [docs/handoff.md](docs/handoff.md#gemini-cli) |

Adicionar outro agente significa escrever mais um adapter que emite o mesmo
formato de evento normalizado; nada mais na ferramenta muda.

---

## Requisitos

- Python 3 (só biblioteca padrão — nada de `pip install`)
- Git
- Claude Code, Codex e/ou Gemini CLI, para os agentes que você usar

## Como obter

```bash
git clone https://github.com/gabrielsouto/handoff.git
```

Mantenha esta pasta onde quiser — ela não precisa viver dentro dos projetos
com os quais trabalha. Opcionalmente, rode `init` uma vez em cada
repositório onde for usar, a partir de dentro dele:

```bash
cd /caminho/do/seu/projeto
python3 /caminho/do/handoff/tools/handoff.py init
```

O `init` cria `.handoff/`, grava um `.handoff/config.json` padrão, adiciona
`/HANDOFF.md` e `/.handoff/` ao `.git/info/exclude` (nunca ao seu
`.gitignore`, de forma idempotente), e acrescenta uma seção **Agent
handoff** ao `AGENTS.md` se ele já existir (nunca cria um do zero).

Há três wrappers finos neste repositório para encurtar o comando de onde
quer que você guarde a ferramenta — `./handoff` (sh), `handoff.cmd`,
`handoff.ps1` — todos repassando cada argumento para `tools/handoff.py`:

```bash
./handoff recover claude --repo /caminho/do/seu/projeto
```

---

## Começando rápido

```bash
python3 tools/handoff.py doctor       # o que a ferramenta enxerga nesta máquina?
python3 tools/handoff.py status       # resumo curto do repo + sessões
```

**Troca planejada de agente** — você está prestes a parar e entregar de propósito:

```bash
python3 tools/handoff.py snapshot claude --ai
```

Depois, no Codex:

> Leia AGENTS.md e HANDOFF.md e continue o trabalho atual. Inspecione o Git e
> o código antes de fazer qualquer mudança.

**Emergência** — uma sessão morreu e você nunca conseguiu pedir um resumo:

```bash
python3 tools/handoff.py recover claude --ai
```

Omita `--ai` em qualquer um dos dois casos se não tiver um endpoint de LLM
configurado — você continua recebendo um `HANDOFF.md` completo e honesto, só
que sem a síntese narrativa do modelo para as dez seções de análise.

---

## Comandos

```bash
python3 tools/handoff.py init                  # uma vez por repositório
python3 tools/handoff.py doctor                # diagnóstico completo
python3 tools/handoff.py status                # resumo curto
python3 tools/handoff.py snapshot claude       # handover planejado
python3 tools/handoff.py snapshot codex  --ai
python3 tools/handoff.py recover  claude --ai  # a sessão morreu
python3 tools/handoff.py recover  gemini
python3 tools/handoff.py consolidate --ai      # refaz o resumo, mesmo snapshot
python3 tools/handoff.py history               # lista handoffs arquivados
python3 tools/handoff.py show                  # imprime o HANDOFF.md
```

`snapshot` e `recover` rodam exatamente o mesmo pipeline; os dois nomes
existem para que a intenção fique clara no momento em que você usa um deles.
Todo comando aceita `--repo PATH` e `--verbose`, antes ou depois do
subcomando; `snapshot`/`recover` também aceitam `--session ID`,
`--session-file PATH`, `--ai` e `--dry-run`.

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

*(a saída do `--help` é sempre em inglês — é assim que a ferramenta responde,
independente do idioma deste README)*

Códigos de saída: `0` sucesso, `1` erro geral, `2` uso inválido, `3` sessão
não encontrada, `4` erro de Git, `5` erro de LLM (`--ai` em si nunca retorna
5 — uma falha de LLM cai para o handoff determinístico com exit 0).

---

## Como uma sessão é escolhida

Em ordem de força: o diretório de trabalho **gravado dentro da própria
sessão** (Claude/Codex: lido do início do transcript; Gemini: lido do seu
arquivo marcador `.project_root`) vence uma **pista pelo nome do diretório**,
que vence nada encontrado. A seleção automática pega a sessão `confirmed`
mais recente, depois a `hinted` mais recente, e **nunca** escolhe
silenciosamente uma sessão que pertence a outro checkout — se todas as
candidatas forem de outro projeto, o comando recusa e pede para você
escolher explicitamente:

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
    ├── git-state.txt               # snapshot legível do Git
    ├── conversation-tail.md        # evidência selecionada do transcript, truncada e rotulada
    ├── ai-input-preview.md         # exatamente o que seria enviado ao LLM (só com --ai)
    └── history/
        └── 2026-09-09_174530_claude_4114df3c.md
```

Tudo isso fica local e fora do Git via `.git/info/exclude`. O `HANDOFF.md`
nunca contém um diff completo — o próximo agente compartilha o mesmo
checkout e pode rodar `git diff`; o handoff é um mapa, não uma cópia.

## O LLM opcional

Por padrão não há integração com LLM nenhuma e nada aqui jamais abre um
socket. Preencha `base_url` e `model` em `.handoff/config.json` (qualquer
endpoint `/v1/chat/completions` compatível com OpenAI) ou exporte
`HANDOFF_LLM_BASE_URL` para ligar; defina `"enabled": false` para forçar o
desligamento. `--ai` é sempre seguro de passar — sem nada configurado, ou se
o endpoint estiver inalcançável, ele explica o motivo e cai para o handoff
determinístico em vez de falhar o comando inteiro.

Quando um endpoint *está* configurado, o modelo nunca vê o transcript bruto:
uma única passagem em streaming constrói primeiro um Evidence Pack limitado,
truncado e com segredos redigidos, e você pode auditar o payload exato com
`--dry-run` antes de qualquer coisa ser enviada.

## Notas de segurança

Uma redação best-effort roda sobre tudo antes de ser gravado ou enviado:
cabeçalhos `Authorization`/`Cookie`, atribuições `api_key=`/`token=`/
`password=`/`secret=`, `sk-…`, `sk-ant-…`, `ghp_…`, padrões de chave
AWS/Google, JWTs e blocos `PRIVATE KEY` — tudo vira `[REDACTED]`. Arquivos
que parecem um cofre de credenciais (`.env`, `auth.json`, `id_rsa`, `*.pem`,
…) têm o conteúdo descartado por completo em vez de redigido. Isso é defesa
em profundidade, não uma garantia de DLP — leia o
`.handoff/ai-input-preview.md` na primeira vez que apontar isto para um
endpoint novo.

---

## Testes

```bash
python3 -m py_compile tools/handoff.py
python3 -m unittest discover -s tests -t .
```

141 testes, só biblioteca padrão (`unittest` + um stub local de
`http.server` para o cliente do LLM). As fixtures são sintéticas,
construídas a partir dos formatos de registro realmente observados em
transcripts reais — nenhuma sessão real é commitada.

## Documentação

[docs/handoff.md](docs/handoff.md) é a referência completa (em inglês): os
formatos exatos de registro JSONL dos três agentes (incluindo a semântica de
patch-log `$set`/`$rewindTo` do Gemini), as regras de associação
sessão↔repositório, cada arquivo que a ferramenta grava, o schema completo
de configuração, e uma seção de troubleshooting para os erros que você
realmente vai encontrar.

## Limitações conhecidas

- O adapter do Gemini foi construído a partir do código-fonte que o próprio
  CLI distribui e de uma sessão que falhou antes de qualquer resposta do
  modelo — não de um round-trip completo, bem-sucedido e autenticado. Trate-o
  como menos testado em batalha do que os adapters de Claude/Codex até que
  seja rodado contra uma sessão real.
- A redação de segredos é heurística, não exaustiva.
- A integração com `AGENTS.md` só acrescenta a um arquivo que já existe;
  nunca cria um.

## Deliberadamente fora do escopo

Embeddings vetoriais, busca semântica sobre o histórico, um servidor HTTP,
MCP, uma interface web, suporte multiusuário, sincronização entre máquinas,
um banco de dados, watchers, hooks automáticos nos agentes, um plugin de VS
Code, qualquer serviço em segundo plano. Quando confiabilidade e uma feature
nova entram em conflito, confiabilidade vence.

## Licença

Ainda sem arquivo de licença — todos os direitos reservados por padrão até
que uma seja adicionada. Abra uma issue se quiser que isso seja formalmente
open-source sob uma licença específica.
