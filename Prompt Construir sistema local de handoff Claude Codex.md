# Projeto: sistema local e leve de handoff entre Claude Code e OpenAI Codex

Quero que você implemente neste repositório um sistema **simples, robusto, auditável e sem infraestrutura adicional** para permitir continuidade de trabalho entre agentes de IA, principalmente **Claude Code e OpenAI Codex**, quando uma sessão termina, bate limite de uso, perde contexto ou preciso trocar de agente.

O objetivo NÃO é construir uma plataforma de memória completa como `ai-memory`.

Quero uma solução intermediária entre:

```text
HANDOFF.md manual
```

e:

```text
servidor de memória
MCP
SQLite
Docker
Rust
embeddings
vector database
daemon
serviços externos
```

A solução deve funcionar usando apenas:

- Python 3
- biblioteca padrão do Python
- Git
- arquivos JSONL que Claude Code e Codex já gravam
- arquivos Markdown
- opcionalmente um modelo LLM interno para consolidação semântica

Não instalar dependências Python.

Não usar:

- pip
- poetry
- pipenv
- npm
- composer
- Docker
- Podman
- Rust
- SQLite
- Redis
- MCP
- banco vetorial
- daemon
- serviço residente
- cron
- systemd
- API externa pública

---

# 1. Contexto real do ambiente

O projeto está sendo desenvolvido em VS Code usando **Remote SSH**.

Servidor remoto Linux atual:

```text
srv-dev-168
```

Usuário:

```text
soutog@IN.PGE.RJ.GOV.BR
```

Home:

```text
/home/IN.PGE.RJ.GOV.BR/soutog
```

Projeto:

```text
/var/www/html/soutog/pgedigital
```

Tanto Claude Code quanto Codex estão atualmente instalados como extensões do VS Code no ambiente Remote SSH.

Os dois agentes executam no servidor remoto.

---

# 2. Onde estão as sessões

## Claude Code

As sessões do Claude relacionadas ao projeto estão em:

```text
~/.claude/projects/
```

Para o projeto atual especificamente:

```text
~/.claude/projects/-var-www-html-soutog-pgedigital/
```

Um exemplo real de uma sessão longa:

```text
/home/IN.PGE.RJ.GOV.BR/soutog/.claude/projects/-var-www-html-soutog-pgedigital/4114df3c-a38c-49c9-895a-baaab587aab2.jsonl
```

Esse arquivo possui aproximadamente 30 MB.

NÃO assuma que todos os arquivos terão essa estrutura ou tamanho.

O código deve detectar as sessões dinamicamente.

---

## Codex

As sessões do Codex ficam em:

```text
~/.codex/sessions/
```

Organizadas aproximadamente como:

```text
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
```

Exemplo real:

```text
/home/IN.PGE.RJ.GOV.BR/soutog/.codex/sessions/2026/09/09/rollout-2026-09-09T16-48-00-01a087b6-828a-7e82-a6d3-9cc9d5e932cf.jsonl
```

---

# 3. Regra importantíssima sobre os formatos JSONL

NÃO implemente o parser baseado apenas em suposições sobre o formato dos arquivos.

Antes de escrever os adapters:

1. Localize as sessões reais mais recentes.
2. Inspecione pequenas amostras.
3. Analise:
   - primeiras linhas;
   - últimas linhas;
   - tipos diferentes de registros;
   - mensagens do usuário;
   - respostas do assistente;
   - chamadas de ferramentas;
   - resultados;
   - erros;
   - possíveis eventos de compactação;
   - metadados da sessão;
   - `cwd`;
   - timestamps;
   - IDs.

Faça isso sem modificar os arquivos.

Nunca reescreva os JSONL originais.

Nunca copie uma sessão inteira para dentro do repositório.

Os JSONL originais são somente fontes read-only.

---

# 4. Objetivo conceitual

Quero conseguir trabalhar assim:

```text
Claude
   │
   │ trabalha por horas
   ▼
sessão JSONL
   │
   │
   ▼
handoff.py
   │
   ├── Git atual
   ├── mudanças
   ├── contexto relevante
   ├── erros
   ├── decisões
   ├── tarefas pendentes
   └── estado recente
           │
           ▼
       HANDOFF.md
           │
           ▼
         Codex
           │
           │ continua o trabalho
           ▼
       novo handoff
           │
           ▼
         Claude
```

O código atual do repositório deve continuar sendo a **fonte principal da verdade**.

A hierarquia conceitual deve ser:

```text
1. Git + filesystem atual
2. transcript real do agente
3. interpretação feita pelo LLM
```

Nunca o contrário.

Uma afirmação presente no transcript ou gerada pelo LLM NÃO prova que algo está implementado.

Somente código, Git, testes e runtime podem confirmar isso.

---

# 5. Arquivos a implementar

Quero inicialmente esta estrutura:

```text
pgedigital/
│
├── tools/
│   └── handoff.py
│
├── HANDOFF.md
│
└── .handoff/
    ├── config.json
    ├── session.json
    ├── git-state.txt
    ├── conversation-tail.md
    ├── ai-input-preview.md
    └── history/
```

Alguns arquivos podem não existir até o primeiro comando correspondente.

Não crie arquivos vazios sem necessidade.

---

# 6. Privacidade Git

Por padrão, os artefatos de handoff devem ser **locais e não commitados**.

Não altere `.gitignore` automaticamente.

Prefira usar:

```text
.git/info/exclude
```

O comando de inicialização deve adicionar, de maneira idempotente:

```text
HANDOFF.md
.handoff/
```

em:

```text
.git/info/exclude
```

Não duplique entradas.

Não remova regras preexistentes.

Se o repositório não for Git, mostre erro claro.

---

# 7. Linguagem

Implementar:

```text
tools/handoff.py
```

em Python 3.

Usar somente biblioteca padrão.

Permitido, por exemplo:

```python
argparse
collections
dataclasses
datetime
hashlib
json
os
pathlib
re
shlex
subprocess
sys
textwrap
urllib.request
urllib.error
```

Não adicionar `requirements.txt`.

Não usar módulos externos.

O script deve funcionar com:

```bash
python3 tools/handoff.py ...
```

---

# 8. Arquitetura interna

Não faça um script monolítico desorganizado.

Mesmo sendo um único arquivo, organize internamente.

Uma estrutura aceitável:

```python
@dataclass
class SessionInfo:
    ...

@dataclass
class NormalizedEvent:
    ...

class GitInspector:
    ...

class ClaudeAdapter:
    ...

class CodexAdapter:
    ...

class EvidenceBuilder:
    ...

class HandoffRenderer:
    ...

class LLMClient:
    ...
```

Não precisa seguir esses nomes literalmente, mas quero responsabilidades separadas.

---

# 9. Evento normalizado

Depois de interpretar Claude/Codex, normalize os eventos para uma representação comum.

Algo conceitualmente semelhante a:

```python
NormalizedEvent(
    timestamp=...,
    agent="claude",
    session_id="...",
    kind="message",
    role="user",
    text="...",
    tool_name=None,
    file_paths=[],
    is_error=False,
    raw_type="..."
)
```

Tipos conceituais úteis:

```text
user_message
assistant_message
tool_call
tool_result
tool_error
compaction
session_metadata
other
```

Não obrigue formatos diferentes a caber em um modelo incorreto.

Campos podem ser opcionais.

---

# 10. Leitura eficiente

Um transcript pode facilmente ter:

```text
30 MB
100 MB
ou mais
```

Não use:

```python
f.read()
```

para carregar o arquivo inteiro sem necessidade.

Processar JSONL linha por linha.

Uma linha inválida não deve abortar uma sessão inteira.

Registrar:

```text
N linhas inválidas ignoradas
```

quando aplicável.

Suportar UTF-8.

Tratar caracteres inválidos de maneira segura.

---

# 11. Detecção do repositório

Determinar a raiz por:

```bash
git rev-parse --show-toplevel
```

Nunca assumir:

```text
/var/www/html/soutog/pgedigital
```

como valor fixo no código.

Esse caminho é apenas o ambiente atual.

O script deve poder ser reutilizado em outros repositórios.

---

# 12. Detecção das sessões do Claude

Criar um adapter para Claude.

Objetivo:

```text
encontrar as sessões relacionadas ao repositório atual
```

Não depender exclusivamente da transformação:

```text
/var/www/html/soutog/pgedigital
->
-var-www-html-soutog-pgedigital
```

Ela pode ser usada como pista, mas o código deve validar o vínculo ao projeto quando dados internos da sessão permitirem.

Preferência de evidência:

1. `cwd` registrado dentro da sessão;
2. diretório da sessão associado ao path;
3. metadados;
4. heurística de caminho.

Para cada sessão encontrada obter pelo menos:

```text
agent
session_id
path
mtime
size
cwd, quando conhecido
```

Ordenar por atividade mais recente.

Permitir selecionar explicitamente:

```bash
--session SESSION_ID
```

ou:

```bash
--session-file PATH
```

---

# 13. Detecção das sessões do Codex

Implementar lógica equivalente para:

```text
~/.codex/sessions/**/rollout-*.jsonl
```

Não assumir que a data contida no caminho sempre corresponde ao último uso.

Usar `mtime` real.

Tentar obter `cwd` e session id dos próprios registros.

Suportar:

```bash
--session SESSION_ID
```

e:

```bash
--session-file PATH
```

---

# 14. Comando `doctor`

Implementar:

```bash
python3 tools/handoff.py doctor
```

Ele deve diagnosticar:

```text
Python version
git disponível
root do repositório
branch atual

Claude:
  diretório encontrado?
  quantidade aproximada de sessões
  sessão mais recente
  tamanho
  mtime

Codex:
  diretório encontrado?
  quantidade aproximada
  sessão mais recente
  tamanho
  mtime

DeepSeek:
  configurado?
  endpoint alcançável?
  modelo configurado?
```

Não mostrar:

```text
tokens
senhas
Authorization
credentials
```

Exemplo:

```text
Repository
  root: /var/www/html/soutog/pgedigital
  branch: item#12345

Claude
  status: OK
  latest: 4114df3c-...
  modified: 2026-09-09 15:45
  size: 29.1 MiB

Codex
  status: OK
  latest: 01a087b6-...
  modified: 2026-09-09 16:48
  size: 113 KiB

AI consolidation
  status: configured
  endpoint: http://10.120.191.20:8000
  model: DeepSeek-V4-Flash-0731
```

---

# 15. Comando `status`

Implementar:

```bash
python3 tools/handoff.py status
```

Mais sucinto que `doctor`.

Mostrar:

```text
repo
branch
commit

working tree
último handoff

última sessão Claude
última sessão Codex
```

---

# 16. Comando `init`

Implementar:

```bash
python3 tools/handoff.py init
```

Responsabilidades:

1. verificar Git;
2. criar `.handoff/`;
3. criar `.handoff/history/`;
4. criar `.handoff/config.json`, somente se ainda não existir;
5. adicionar regras ao `.git/info/exclude`;
6. não sobrescrever configuração existente;
7. executar diagnóstico básico;
8. imprimir próximos comandos.

---

# 17. Configuração

Usar:

```text
.handoff/config.json
```

Como esse diretório é local/excluído do Git, pode conter configuração específica da máquina.

Estrutura sugerida:

```json
{
  "llm": {
    "enabled": false,
    "base_url": "http://10.120.191.20:8000",
    "model": "DeepSeek-V4-Flash-0731",
    "api_key_env": "HANDOFF_LLM_API_KEY",
    "timeout_seconds": 120,
    "max_input_chars": 120000
  },
  "evidence": {
    "first_events": 20,
    "last_events": 60,
    "max_errors": 30,
    "max_tool_events": 80,
    "max_compactions": 20
  }
}
```

Valores podem ser refinados durante implementação.

Também permitir overrides por ambiente:

```text
HANDOFF_LLM_BASE_URL
HANDOFF_LLM_MODEL
HANDOFF_LLM_API_KEY
```

NÃO colocar API key em código.

No ambiente atual provavelmente nenhuma API key é necessária, mas suporte opcionalmente.

---

# 18. DeepSeek interno

Existe um modelo interno disponível:

```text
DeepSeek-V4-Flash-0731
```

Endpoint base:

```text
http://10.120.191.20:8000
```

Considere inicialmente que ele oferece uma API OpenAI-compatible.

Antes de implementar definitivamente o cliente:

1. valide isso de forma não destrutiva;
2. teste `/v1/models`;
3. confirme o modelo exposto;
4. confirme `/v1/chat/completions`.

Não quebre o sistema se o endpoint estiver fora do ar.

LLM é **opcional**.

Todo o restante deve funcionar sem LLM.

---

# 19. Cliente HTTP do LLM

Implementar usando somente biblioteca padrão:

```python
urllib.request
```

Não usar:

```text
requests
httpx
openai
```

Suportar:

```text
POST /v1/chat/completions
```

Estrutura conceitual:

```json
{
  "model": "DeepSeek-V4-Flash-0731",
  "messages": [
    {
      "role": "system",
      "content": "..."
    },
    {
      "role": "user",
      "content": "..."
    }
  ],
  "temperature": 0.1
}
```

Ajustar conforme a API real encontrada.

Timeout configurável.

Erros de rede devem gerar:

```text
WARNING: AI consolidation failed.
Falling back to deterministic handoff.
```

Nunca perder o handoff apenas porque o LLM falhou.

---

# 20. Segurança antes de enviar ao LLM

Mesmo sendo endpoint interno, aplicar minimização.

Nunca enviar propositalmente:

```text
~/.claude/.credentials.json
~/.codex/auth.json
.env
private keys
cookies
tokens de autenticação
senhas
Authorization headers
credentials
```

Criar uma função de redaction best-effort.

Detectar padrões comuns como:

```text
Authorization: Bearer ...
api_key=...
apikey=...
token=...
password=...
secret=...
sk-...
ghp_...
```

Substituir por:

```text
[REDACTED]
```

Não alegar que isso é DLP completo.

É apenas proteção adicional.

---

# 21. Nunca mandar transcript bruto de 30 MB

Essa é uma regra fundamental.

O DeepSeek NÃO deve receber o JSONL completo.

Criar primeiro um **Evidence Pack**.

O Evidence Pack deve conter informações escolhidas do transcript.

---

# 22. Estratégia de Evidence Pack

Durante uma única passagem pelo JSONL, coletar de maneira limitada:

## Início da sessão

Primeiros eventos relevantes, porque costumam conter:

```text
objetivo original
contexto inicial
restrições
pedido do usuário
```

Exemplo:

```text
20 eventos
```

---

## Fim da sessão

Manter em `deque` os últimos eventos relevantes.

Exemplo:

```text
60 eventos
```

Esses são provavelmente os mais importantes para responder:

```text
onde paramos?
```

---

## Mensagens do usuário

Preservar especialmente:

```text
primeiras mensagens relevantes
últimas mensagens
correções de requisito
novas restrições
```

Não precisa guardar todas ilimitadamente.

---

## Erros

Coletar de maneira limitada eventos contendo:

```text
erro de ferramenta
exception
falha de teste
command exit != 0
stack trace relevante
```

---

## Compactações / summaries

Se Claude/Codex registrar resumos de compactação, coletar.

Eles podem ser extremamente úteis porque já representam sínteses intermediárias da sessão.

---

## Alterações de arquivos

Dar atenção especial a tool calls envolvendo:

```text
write
edit
patch
create
delete
```

Especialmente quando afetarem arquivos atualmente detectados pelo Git como:

```text
modified
added
deleted
untracked
```

---

# 23. GitInspector

Implementar coleta determinística de Git.

No mínimo:

```bash
git rev-parse --show-toplevel
git rev-parse HEAD
git branch --show-current
git status --porcelain=v1
git diff --stat
git diff --name-status
git diff --cached --stat
git diff --cached --name-status
git log -10 --oneline --decorate
```

Também identificar:

```text
arquivos modificados
arquivos adicionados
arquivos deletados
arquivos não rastreados
```

NÃO colocar automaticamente o `git diff` inteiro no handoff.

O próximo agente está dentro do mesmo checkout e pode executar:

```bash
git diff
```

O handoff deve funcionar como mapa.

---

# 24. Arquivo `.handoff/git-state.txt`

Gerar um arquivo legível contendo algo como:

```text
Generated: ...
Repository: ...
Branch: ...
HEAD: ...

=== STATUS ===

...

=== DIFF STAT ===

...

=== CHANGED FILES ===

...

=== STAGED ===

...

=== RECENT COMMITS ===

...
```

---

# 25. Arquivo `.handoff/session.json`

Guardar metadata do snapshot.

Exemplo:

```json
{
  "generated_at": "...",
  "agent": "claude",
  "session_id": "4114df3c-a38c-49c9-895a-baaab587aab2",
  "source_path": "/home/.../4114....jsonl",
  "source_size": 30544858,
  "source_mtime": "...",
  "source_sha256": "...",
  "repository": "/var/www/html/soutog/pgedigital",
  "branch": "...",
  "head": "..."
}
```

Calcular SHA256 por streaming.

Não carregar o arquivo inteiro.

---

# 26. `.handoff/conversation-tail.md`

Gerar uma representação humana de eventos relevantes.

Exemplo:

```markdown
# Conversation evidence

Source agent: Claude
Session: ...

## Beginning

### USER
...

### ASSISTANT
...

---

## Errors / notable tool activity

### TOOL
...

---

## Recent activity

### USER
...

### ASSISTANT
...

### TOOL
...
```

Limitar tamanho.

Não duplicar megabytes de output de tools.

Tool results gigantes devem ser truncados explicitamente:

```text
[tool result truncated: original length 183204 chars]
```

---

# 27. `HANDOFF.md`

Esse é o arquivo principal que o próximo agente deve ler.

Quero estrutura estável.

Exemplo:

```markdown
# Current Handoff

> Generated at ...
> Source: Claude
> Session: ...
> Repository: ...
> Branch: ...
> HEAD: ...

## Goal

...

## Current State

...

## Confirmed Completed Work

...

## Relevant Files

- ...

## Technical Decisions

...

## Failed / Rejected Approaches

...

## Known Problems

...

## Pending Work

1. ...
2. ...

## Suggested Next Steps

1. ...
2. ...

## Unresolved Questions

...

## Git State

...

## Evidence

Detailed recent conversation evidence:
`.handoff/conversation-tail.md`

Detailed Git snapshot:
`.handoff/git-state.txt`

## Resume Instructions

1. Read `AGENTS.md`.
2. Read this file.
3. Inspect `git status`.
4. Inspect `git diff`.
5. Read the relevant source files.
6. Consult `.handoff/conversation-tail.md` only when additional historical context is needed.
7. Preserve unfinished work already present in the working tree.
8. Do not restart the implementation from scratch unless evidence proves the existing approach is unusable.
```

---

# 28. Handoff sem IA

Sem `--ai`, gerar `HANDOFF.md` determinístico.

Não inventar:

```text
objetivo
decisões
pendências
```

se não puderem ser determinados com segurança.

Pode escrever:

```text
Not automatically determined.
See conversation evidence.
```

ou:

```text
Não confirmado automaticamente.
```

Esse modo deve sempre funcionar.

---

# 29. Handoff com IA

Com:

```bash
python3 tools/handoff.py recover claude --ai
```

usar DeepSeek para transformar o Evidence Pack em um handoff semântico.

---

# 30. Prompt interno para o DeepSeek

Use um system prompt muito próximo desta intenção:

```text
Você está preparando um handoff entre dois agentes de programação.

Seu trabalho é interpretar evidências históricas de uma sessão anterior
e produzir um resumo operacional para o próximo agente continuar o trabalho.

FONTES DE VERDADE, em ordem:

1. Estado Git e filesystem atual.
2. Transcript real da sessão.
3. Sua própria interpretação.

Nunca inverta essa ordem.

Não presuma que algo foi implementado apenas porque foi discutido.

Não transforme uma intenção futura em trabalho concluído.

Não diga que um bug foi corrigido sem evidência suficiente.

Não diga que um teste passou sem evidência explícita.

Quando uma informação for incerta, marque:

"não confirmado"

Identifique somente:

1. objetivo atual;
2. estado da implementação;
3. trabalho efetivamente concluído;
4. arquivos relevantes;
5. decisões técnicas tomadas;
6. abordagens tentadas e descartadas;
7. erros e problemas encontrados;
8. trabalho pendente;
9. próximos passos;
10. questões não resolvidas.

Diferencie claramente:

- discutido;
- tentado;
- implementado;
- testado;
- confirmado.

Não reproduza grandes trechos do transcript.

Não escreva introduções genéricas.

Não invente contexto.

Produza Markdown conciso, técnico e orientado à continuação do trabalho.
```

---

# 31. Preview antes de enviar ao DeepSeek

Antes de qualquer request do LLM, gerar:

```text
.handoff/ai-input-preview.md
```

Esse arquivo deve conter exatamente, ou de forma equivalente, o material textual que será enviado ao LLM, depois da redaction.

Assim posso auditar o envio.

Implementar:

```bash
python3 tools/handoff.py recover claude --ai --dry-run
```

Nesse caso:

```text
gera preview
NÃO chama a API
```

---

# 32. Comando `snapshot`

Implementar:

```bash
python3 tools/handoff.py snapshot claude
```

e:

```bash
python3 tools/handoff.py snapshot codex
```

Objetivo:

```text
capturar estado atual de maneira factual
```

Deve:

1. detectar sessão;
2. coletar Git;
3. gerar metadata;
4. gerar conversation evidence;
5. gerar handoff determinístico;
6. salvar histórico.

Permitir:

```bash
--ai
```

para consolidação semântica.

---

# 33. Comando `recover`

Implementar:

```bash
python3 tools/handoff.py recover claude
```

e:

```bash
python3 tools/handoff.py recover codex
```

Esse é o modo de emergência.

Cenário:

```text
Claude trabalhou por horas
↓
quota acabou
↓
não consegui pedir um resumo
```

O comando deve reconstruir automaticamente um handoff usando:

```text
última sessão compatível
+
Git atual
+
Evidence Pack
```

Com:

```bash
--ai
```

usar DeepSeek.

---

# 34. Diferença entre snapshot e recover

`snapshot`:

```text
uso normal
```

`recover`:

```text
sessão morreu sem handoff
```

Internamente podem compartilhar quase todo o código.

Mas quero comandos semanticamente diferentes porque isso torna o fluxo fácil de lembrar.

---

# 35. Comando `consolidate`

Implementar:

```bash
python3 tools/handoff.py consolidate
```

e:

```bash
python3 tools/handoff.py consolidate --ai
```

Objetivo:

```text
reprocessar um snapshot já existente
```

Sem reler necessariamente toda a sessão quando não for preciso.

Por exemplo:

```text
conversation-tail.md
+
git-state.txt
+
session.json
↓
DeepSeek
↓
novo HANDOFF.md
```

---

# 36. Histórico

Antes de substituir `HANDOFF.md`, guardar a versão anterior quando relevante.

Estrutura:

```text
.handoff/history/
2026-09-09_174530_claude_4114df3c.md
2026-09-09_192100_codex_01a087b6.md
```

Não copiar transcripts.

Guardar apenas handoffs.

---

# 37. Comando `history`

Implementar:

```bash
python3 tools/handoff.py history
```

Listar:

```text
timestamp
agent
session
arquivo
```

Não precisa construir ferramenta sofisticada de busca.

`grep` continua sendo suficiente.

---

# 38. Comando `show`

Implementar:

```bash
python3 tools/handoff.py show
```

Mostrar o `HANDOFF.md` atual no terminal.

---

# 39. Seleção explícita de sessão

Todos os comandos relevantes devem aceitar:

```bash
--session ID
```

ou:

```bash
--session-file PATH
```

Exemplo real:

```bash
python3 tools/handoff.py recover claude \
  --session 4114df3c-a38c-49c9-895a-baaab587aab2 \
  --ai
```

Isso é essencial quando há múltiplas sessões simultâneas.

---

# 40. Sessão automática

Quando nenhum ID for especificado:

1. localizar apenas sessões plausivelmente pertencentes ao repositório atual;
2. ordenar por última atividade;
3. escolher a mais recente;
4. mostrar claramente a escolha.

Exemplo:

```text
Using Claude session:

4114df3c-a38c-49c9-895a-baaab587aab2
modified 2026-09-09 15:45:31
29.1 MiB
```

Não escolha silenciosamente uma sessão ambígua se houver evidência de que ela pertence a outro checkout.

---

# 41. Não modificar transcripts

Regra absoluta.

O script nunca deve:

```text
editar
truncar
mover
renomear
deletar
```

arquivos de:

```text
~/.claude/
~/.codex/
```

Somente leitura.

---

# 42. Resiliência

O script deve continuar útil quando:

```text
Claude não está instalado
Codex não está instalado
uma pasta não existe
DeepSeek está fora do ar
Git está em detached HEAD
sessão contém JSON inválido
tool result é enorme
transcript termina com linha incompleta
```

Cada caso deve resultar em erro ou warning compreensível.

Não gerar stack trace para erros de uso normais.

Modo debug pode existir:

```bash
--verbose
```

---

# 43. Exit codes

Use códigos coerentes.

Exemplo:

```text
0 sucesso
1 erro geral
2 uso inválido/configuração
3 sessão não encontrada
4 erro Git
5 erro LLM quando operação exigir estritamente IA
```

Mas, em:

```bash
recover --ai
```

falha do LLM deve preferencialmente cair para handoff determinístico e ainda terminar com sucesso acompanhado de warning.

---

# 44. Atomicidade

Ao gerar arquivos importantes:

```text
HANDOFF.md
session.json
git-state.txt
conversation-tail.md
```

evite deixar arquivos parcialmente escritos.

Preferir:

```text
escrever arquivo temporário
fsync quando razoável
rename/replace atômico
```

usando biblioteca padrão.

---

# 45. Datas

Usar timestamps ISO 8601.

Preferir timezone local corretamente detectado.

Exemplo:

```text
2026-09-09T17:42:31-03:00
```

---

# 46. Saída do terminal

Saída legível e curta.

Exemplo:

```text
Repository: /var/www/html/soutog/pgedigital
Agent: Claude
Session: 4114df3c-a38c-49c9-895a-baaab587aab2
Transcript: 29.1 MiB

Collecting Git state...
Parsing session...
Selected 117 relevant events.
Building evidence pack...
Redacted 3 possible secrets.
Calling DeepSeek-V4-Flash-0731...
Handoff written: HANDOFF.md
History: .handoff/history/2026-09-09_174530_claude_4114df3c.md
```

Não usar animações ou dependências externas.

---

# 47. Performance

Para um arquivo de aproximadamente 30 MB, quero processamento razoavelmente rápido.

Evitar:

```text
O(n²)
regex extremamente caras
reler arquivo inteiro muitas vezes
```

Idealmente uma passagem principal pelo JSONL.

SHA256 pode exigir outra passagem se necessário.

Aceitável.

---

# 48. Não interpretar demais sem LLM

O parser Python deve ser responsável por:

```text
extração
normalização
seleção
evidência
Git
metadata
```

Não tente transformar regex em inteligência artificial.

Evite heurísticas frágeis como:

```text
se mensagem contém "decidimos", isso é uma decisão confirmada
```

A interpretação semântica é responsabilidade opcional do DeepSeek.

---

# 49. Integration com `AGENTS.md`

Verifique se existe:

```text
AGENTS.md
```

Não sobrescrever.

Se existir, acrescentar somente se ainda não houver seção equivalente:

```markdown
## Agent handoff

Before continuing an existing task:

1. Read `HANDOFF.md` when it exists.
2. Inspect `git status` and `git diff`.
3. Preserve unfinished work from previous agents.
4. Read `.handoff/conversation-tail.md` when additional historical context is required.
5. Treat repository source and tests as authoritative over historical handoff text.

Before ending substantial work:

1. Update or generate the handoff.
2. Record completed work, decisions, blockers and next steps.
3. Do not paste large diffs into the handoff; the repository is the source of truth.
```

Porém:

- preserve completamente o restante do arquivo;
- não duplicar seção;
- não alterar regras já existentes sem necessidade.

Se não existir `AGENTS.md`, não crie automaticamente antes de me informar.

---

# 50. Documentação

Criar:

```text
docs/handoff.md
```

Documentar:

```text
propósito
arquitetura
onde Claude/Codex guardam sessões
comandos
configuração
DeepSeek opcional
segurança
fluxo normal
fluxo de emergência
troubleshooting
```

Incluir exemplos reais genéricos.

Não colocar tokens.

---

# 51. Opcional: VS Code Tasks

Depois que toda implementação principal estiver funcionando, verifique se existe:

```text
.vscode/tasks.json
```

Pode adicionar tarefas apenas se isso puder ser feito preservando integralmente tarefas existentes.

Tarefas úteis:

```text
Handoff: Status
Handoff: Doctor
Handoff: Snapshot Claude
Handoff: Snapshot Codex
Handoff: Recover Claude
Handoff: Recover Claude with AI
Handoff: Recover Codex
Handoff: Recover Codex with AI
```

Isso é opcional.

Não comprometa a implementação principal por isso.

---

# 52. Testes

Não quero framework externo.

Criar testes usando:

```python
unittest
```

se isso puder ser feito sem exagerar na estrutura.

Por exemplo:

```text
tests/tools/test_handoff.py
```

Testar principalmente funções determinísticas:

```text
normalização
redaction
truncamento
seleção de eventos
atomic write
parsing de fixtures
Git parsing
config
```

NÃO usar meus transcripts reais como fixture commitada.

Criar fixtures sintéticas mínimas baseadas somente nos formatos observados.

---

# 53. Validação contra ambiente real

Depois dos testes unitários, validar read-only usando as sessões reais.

## Claude

A sessão:

```text
4114df3c-a38c-49c9-895a-baaab587aab2
```

deve ser detectável no projeto atual.

NÃO altere o arquivo.

Executar inicialmente:

```bash
python3 tools/handoff.py doctor
```

Depois:

```bash
python3 tools/handoff.py recover claude \
  --session 4114df3c-a38c-49c9-895a-baaab587aab2
```

Inspecionar o resultado.

Somente depois testar AI:

```bash
python3 tools/handoff.py recover claude \
  --session 4114df3c-a38c-49c9-895a-baaab587aab2 \
  --ai \
  --dry-run
```

Inspecionar:

```text
.handoff/ai-input-preview.md
```

Se estiver seguro e coerente:

```bash
python3 tools/handoff.py recover claude \
  --session 4114df3c-a38c-49c9-895a-baaab587aab2 \
  --ai
```

---

# 54. Validação Codex

Detectar a sessão recente:

```text
~/.codex/sessions/2026/09/09/...
```

Executar:

```bash
python3 tools/handoff.py snapshot codex
```

Confirmar que o adapter consegue reconhecer:

```text
mensagens
session metadata
cwd
tool events
```

conforme o formato real encontrado.

---

# 55. Critérios de aceitação da versão 1

A implementação só está concluída quando estas situações funcionarem:

### A

```bash
python3 tools/handoff.py doctor
```

identifica corretamente:

```text
repo
Claude
Codex
DeepSeek
```

---

### B

```bash
python3 tools/handoff.py status
```

mostra o estado atual sem erro.

---

### C

```bash
python3 tools/handoff.py recover claude
```

detecta automaticamente uma sessão Claude pertencente ao projeto.

---

### D

A sessão real de ~30 MB pode ser processada sem carregamento integral em RAM.

---

### E

São gerados:

```text
HANDOFF.md
.handoff/session.json
.handoff/git-state.txt
.handoff/conversation-tail.md
```

---

### F

```bash
python3 tools/handoff.py recover claude --ai --dry-run
```

gera preview sem chamar o DeepSeek.

---

### G

```bash
python3 tools/handoff.py recover claude --ai
```

usa o DeepSeek quando disponível.

---

### H

Se o DeepSeek estiver indisponível, ainda é produzido um handoff determinístico.

---

### I

```bash
python3 tools/handoff.py snapshot codex
```

funciona usando os JSONL reais do Codex.

---

### J

Nenhum transcript original de Claude/Codex é alterado.

---

### K

Nenhuma dependência externa é instalada.

---

# 56. Experiência desejada no uso diário

## Início de uma nova tarefa

Continuo usando Claude ou Codex normalmente.

Não preciso iniciar nenhum serviço.

---

## Antes de trocar voluntariamente de agente

Posso executar:

```bash
python3 tools/handoff.py snapshot claude --ai
```

Depois abrir Codex e dizer:

```text
Leia AGENTS.md e HANDOFF.md e continue o trabalho atual.
Inspecione o Git e o código antes de fazer mudanças.
```

---

## Quando Claude bate a quota sem aviso

Executar:

```bash
python3 tools/handoff.py recover claude --ai
```

Depois continuar no Codex.

---

## Quando Codex precisa entregar para Claude

Executar:

```bash
python3 tools/handoff.py snapshot codex --ai
```

Depois abrir Claude.

---

# 57. Filosofia do projeto

Manter sempre estas propriedades:

```text
simples
local
sem servidor
sem daemon
sem banco
sem lock-in
legível por humanos
arquivos Markdown
Git continua soberano
transcripts originais continuam soberanos
LLM é opcional
falha do LLM não impede handoff
```

Evitar transformar essa ferramenta em uma plataforma.

Se surgir uma escolha entre:

```text
mais features
```

e:

```text
mais confiabilidade
```

prefira confiabilidade.

---

# 58. Não implementar agora

NÃO implementar nesta versão:

```text
vector embeddings
busca semântica histórica
servidor HTTP próprio
MCP
interface web
multiusuário
sincronização entre máquinas
banco de dados
monitoramento contínuo
watchers
hooks automáticos Claude/Codex
interceptação do VS Code
daemon
plugin VS Code
background service
```

Essas coisas fogem do objetivo.

---

# 59. Evolução futura

Estruture o código de modo que futuramente seja possível adicionar, sem reescrever tudo:

```text
Gemini
OpenCode
outros agentes
```

Para isso os adapters devem produzir eventos normalizados.

Mas não implemente esses agentes agora.

---

# 60. Procedimento de implementação

Faça o trabalho nesta ordem.

## Etapa 1

Inspecione:

```text
repositório
AGENTS.md
estrutura
Git
Python disponível
JSONL Claude
JSONL Codex
```

Não modifique nada ainda.

---

## Etapa 2

Documente internamente o formato real observado nos dois JSONL.

Não precisa me entregar um ensaio enorme.

Use isso para desenhar os adapters corretamente.

---

## Etapa 3

Implemente:

```text
GitInspector
config
ClaudeAdapter
CodexAdapter
event normalization
```

---

## Etapa 4

Implemente:

```text
EvidenceBuilder
redaction
truncation
```

---

## Etapa 5

Implemente geração:

```text
session.json
git-state.txt
conversation-tail.md
HANDOFF.md determinístico
history
```

---

## Etapa 6

Implemente CLI:

```text
init
doctor
status
snapshot
recover
consolidate
history
show
```

---

## Etapa 7

Implemente cliente opcional DeepSeek.

---

## Etapa 8

Implemente:

```text
--ai
--dry-run
--session
--session-file
--verbose
```

---

## Etapa 9

Adicione testes.

---

## Etapa 10

Teste contra a sessão real do Claude de ~30 MB.

---

## Etapa 11

Teste contra Codex.

---

## Etapa 12

Só então atualize documentação e opcionalmente `AGENTS.md`.

---

# 61. Regras de alteração do repositório

Não altere código da aplicação `pgedigital` para implementar essa ferramenta.

A princípio suas alterações devem se limitar a:

```text
tools/handoff.py
docs/handoff.md
tests/tools/...
AGENTS.md
.vscode/tasks.json
```

e arquivos locais ignorados:

```text
HANDOFF.md
.handoff/
.git/info/exclude
```

Se descobrir necessidade real de alterar outro arquivo, explique no resumo final.

---

# 62. Antes de concluir

Execute:

```bash
python3 -m py_compile tools/handoff.py
```

Execute os testes.

Execute:

```bash
python3 tools/handoff.py doctor
```

Execute uma recuperação real sem IA.

Execute AI em `--dry-run`.

Se o preview estiver aceitável, execute teste real do DeepSeek.

Revise:

```bash
git status
git diff
```

Certifique-se de que nenhum arquivo de sessão, credential ou conteúdo sensível entrou no Git.

---

# 63. Resumo final que quero de você

Quando terminar, não me dê apenas “implementado”.

Informe:

```text
1. arquivos criados/alterados;
2. arquitetura implementada;
3. formatos Claude/Codex encontrados;
4. como a associação sessão ↔ projeto funciona;
5. resultado dos testes;
6. resultado da sessão Claude real;
7. resultado do teste Codex;
8. resultado do DeepSeek;
9. limitações conhecidas;
10. comandos exatos que devo usar no dia a dia.
```

---

# 64. Regra final

Faça uma implementação funcional da versão 1.

Não transforme o escopo em um projeto enorme.

O objetivo principal é extremamente específico:

> quando Claude ou Codex interromperem uma sessão no meio de uma tarefa, quero conseguir gerar em segundos um handoff confiável para que o outro agente continue trabalhando no mesmo checkout com o mínimo possível de perda de contexto.

Esse objetivo tem prioridade sobre qualquer feature secundária.