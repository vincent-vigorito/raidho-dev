# raidho — plugin Claude Code

> Trasforma qualunque progetto software in una **knowledge base self-maintained + memoria identitaria + ricerca semantica del codice**, gestita end-to-end dall'agent dentro Claude Code.

**Stato**: v0.31.0 — disponibile su GitHub; CI verde, prove di upgrade negli host ancora da completare. Plugin CLI standalone. License MIT.

## Cosa fa, in 7 punti

1. **Wiki strutturato per progetto** in `.raidhowiki/wiki/` (entities, concepts, sources, analysis, sessions) mantenuto dall'agent via tool MCP CRUD + lint + rename + backlinks.
2. **Memoria identitaria** in 4 layer: wiki semantico + user profile + soul agent + sessions journal.
3. **Ricerca semantica del codice** (`code.search`): hybrid 3-livelli (ripgrep → LLM rerank → vector embedding sqlite-vec). Provider pluggable (OpenRouter default, Voyage AI, OpenAI, local sentence-transformers). Description con trigger prescrittivi USE/SKIP così l'agent sceglie autonomamente vs `Grep` in base alla natura della query (semantica/concettuale → code.search, nome esatto → Grep).
4. **Roadmap task come 4° file speciale**: `roadmap.md` con priority/owner/est, 6 tool MCP, slash command `/raidho-task`, focus top-5 P0/P1 al SessionStart per continuity multi-agent.
5. **Auto-summary di sessione** in background allo SessionEnd (subprocess detached, non blocca `/exit`).
6. **Skill management 3-livelli** (v0.8.0): SKILL.md con frontmatter strutturato in `.raidhowiki/skills/<slug>/`, discovery multi-source (project + user-global + plugin), progressive disclosure (`skill.list` → `skill.load` → `skill.read_file`), e write-side agent-managed (`skill.save / patch / edit / delete / write_file / remove_file`) per memoria procedurale persistente. Catalog Level 0 auto-iniettato al SessionStart.
7. **Knowledge graph wiki ↔ codice** (v0.9.0): embedding condiviso tra wiki pages e code chunks → k-NN cross-kind (`graph.semantic_neighbors`) scopre "questa entity copre quale file?" e duplicati semantici. `graph.report` produce `GRAPH_REPORT.md` con god nodes + cluster + surprise edges (alta similarity, niente `[[wikilink]]`) + auto-mapping wiki→code per token reduction agent. `graph.html` genera visualizer Cytoscape standalone con sidebar search. Re-embed automatico: inline nei `wiki.upsert_*` + PostToolUse hook su Write/Edit + SessionEnd consistency check.

## Install

### Prerequisiti

- Claude Code CLI
- Python 3.9+ (CI su 3.9 / 3.10 / 3.12, macOS e Linux). Per la ricerca vettoriale serve un interprete che carichi estensioni sqlite: il Python di sistema macOS **non** lo fa → `brew install python@3.12`
- `ripgrep` (`rg`) per ricerca lessicale e fallback: `brew install ripgrep` su macOS, `sudo apt-get install ripgrep` su Debian/Ubuntu.
- (Opzionale per code search) `pip install sqlite-vec httpx`

### Install via marketplace

Dentro Claude Code in un progetto qualunque:

```
/plugin marketplace add https://github.com/vincent-vigorito/raidhodev.git
/plugin install raidho@raidhodev
```

CC clona automaticamente il repo in `~/.claude/plugins/marketplaces/raidhodev/`. Aggiornamento successivo:

```
/plugin update raidho@raidhodev
```

Se un progetto ha un wiki scaffoldato con una versione precedente del plugin, dopo l'update lancia `/raidho-upgrade` nel progetto per portarlo al layout corrente (non-distruttivo).

Per dev locale del plugin (contributor only): clone manuale in `~/Documents/raidhodev/` e `marketplace add /Users/$(whoami)/Documents/raidhodev` su path locale.

### Setup primo progetto

```bash
cd ~/Documents/my-project
claude
```

Dentro Claude Code:

```
/raidho-init                # scaffolda .raidhowiki/ (wiki + meta + config + triade AGENTS/SOUL/TOOLS)
/raidho-config              # AskUserQuestion: scegli provider + model embedding
/raidho-index-code          # build vector index del codebase
```

Poi nella chat usa naturalmente: *"cosa è X?"*, *"trova il code che gestisce auth"*, *"aggiungi task per refactor Y"* — l'agent richiama i tool MCP appropriati.

### Setup API key embedding

`.raidhowiki/.secrets.env` (gitignored automaticamente):

```bash
echo "OPENROUTER_API_KEY=sk-or-..." >> .raidhowiki/.secrets.env
# o VOYAGE_API_KEY / OPENAI_API_KEY a seconda del provider scelto
```

Il server MCP `raidho_memory` **auto-loada** all'avvio — niente shell setup. Restart CC dopo il primo setup.

## Cross-harness setup (Codex · Grok · Antigravity · OpenCode)

Il core (`mcp_memory_server.py` + `mcp_code_server.py`) è **JSON-RPC 2.0 over stdio**
standard, stdlib pure, zero dipendenze → gira su **qualunque host MCP**, non solo Claude
Code. Stessi 3 env ovunque: `RAIDHO_SCOPE` (`project`|`hub`|`agent`), `RAIDHO_ROOT` (path del
root), `RAIDHO_TOOL_GROUPS` (filtro opzionale, default tutti i gruppi).

Verificato con handshake `initialize` + `tools/list` su stdio puro: `raidho_memory` espone
28 tool (con `memory,wiki,roadmap`), `raidho_code` 1 tool (`execute_python`, **opt-in**: `RAIDHO_CODE_EXEC=1`, vedi [`SECURITY.md`](./SECURITY.md)). Nessuna
modifica al plugin: cambia solo *dove* dichiari il server. `<RAIDHODEV>` = path del plugin
installato (`~/.claude/plugins/marketplaces/raidhodev`) o di un clone locale del repo.

**Claude Code** — `.mcp.json` del progetto (formato di riferimento, scritto da `/raidho-init`):

```json
{
  "mcpServers": {
    "raidho_memory": {
      "command": "python3",
      "args": ["<RAIDHODEV>/scripts/mcp_memory_server.py"],
      "env": { "RAIDHO_SCOPE": "project", "RAIDHO_ROOT": "/abs/project", "RAIDHO_TOOL_GROUPS": "memory,wiki,roadmap,code" }
    }
  }
}
```

**OpenAI Codex** — `~/.codex/config.toml` (o `.codex/config.toml` project-scoped):

```toml
[mcp_servers.raidho_memory]
command = "python3"
args = ["<RAIDHODEV>/scripts/mcp_memory_server.py"]

[mcp_servers.raidho_memory.env]
RAIDHO_SCOPE = "project"
RAIDHO_ROOT = "/abs/project"
RAIDHO_TOOL_GROUPS = "memory,wiki,roadmap,code"
```

Oppure via CLI:
`codex mcp add raidho_memory --env RAIDHO_SCOPE=project --env RAIDHO_ROOT=/abs/project -- python3 <RAIDHODEV>/scripts/mcp_memory_server.py`

**Grok Build** (xAI) — **zero config**, verificato sul campo: ha una *Claude-compatibility*
nativa (`grok inspect` → "Harness Compatibility: claude", tutto on di default). Carica da solo:
i **plugin Claude Code** installati (skills + hooks di raidho), il **`.mcp.json` di progetto**
formato CC, `AGENTS.md` e perfino le permissions da `.claude/settings.local.json`.

Unico passo richiesto: dare il **trust al progetto** dentro la TUI (`/hooks` → *trust this
project*, oppure dal pannello `/plugins` → tab Hooks) — senza trust, MCP e hook restano
bloccati per sicurezza e l'agente ripiega sull'accesso bash-native. Diagnostica:
`grok mcp doctor` e `grok inspect`.

> **Memoria che sopravvive alla sessione (v0.22)**: senza trust, Grok *lavora* ma **non
> scrive il journal** (sessione reale 2026-08-19: zero file `*-cli-grok-*`). Con trust il
> hook SessionEnd gira; l'harness viene riconosciuto dall'env (Claude: `CLAUDECODE=1`;
> override `RAIDHO_HARNESS=grok`) e, se non riconosciuto, il journal dice `cli-unknown` e il
> payload dell'hook viene salvato in `.raidhowiki/.hook-payloads.log` — è lì che si legge il
> wire format per scrivere l'adapter. Fallback per harness senza hook: journal via
> `/raidho-session-save` (vedi *Bootstrap* nel context composto).

(Il grok-cli open-source di superagent-ai è un tool diverso: lì serve `.grok/settings.json`
con `mcpServers` stile Claude.)

**OpenCode** — `opencode.json`, chiave `mcp`, tipo `local` (`command` è un array, env in `environment`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "raidho_memory": {
      "type": "local",
      "command": ["python3", "<RAIDHODEV>/scripts/mcp_memory_server.py"],
      "enabled": true,
      "environment": { "RAIDHO_SCOPE": "project", "RAIDHO_ROOT": "/abs/project", "RAIDHO_TOOL_GROUPS": "memory,wiki,roadmap,code" }
    }
  }
}
```

**Antigravity CLI** (`agy`, ex Gemini CLI) — **esperienza piena**, validata sul campo (agy 1.1.26,
2026-09-04). Un comando scrive `.agents/mcp_config.json` (server `raidho_memory`, stesso formato
del `.mcp.json` di Claude Code) e `.agents/hooks.json` (hook nominato `raidho`, formato piatto agy):

```bash
python3 <RAIDHODEV>/scripts/install_antigravity.py --project /abs/project
agy            # dalla root del progetto; in headless: agy -p "..." --add-dir .
```

Gli hook passano da `hooks/antigravity_adapter.py`, che traduce i 5 eventi agy nel contratto
Claude Code senza toccare gli script condivisi: `PreInvocation` (prima invocazione) → output di
`session_start.py` iniettato come `ephemeralMessage` (log recenti, focus roadmap, catalogo skill,
pending steward); `Stop` (ogni turno) → `transcript_full.jsonl` normalizzato e journal upsert per
`conversationId` (`agent: cli-antigravity`), auto-summary in background via `agy -p`, consistency
check embedding; `PostToolUse` (`write_to_file`/`replace_file_content`) → re-embed pagina wiki.
Contesto statico: agy legge `AGENTS.md` e `GEMINI.md` (symlink) nativamente. Quirk noti: gli hook
girano con cwd `.agents/` (la root arriva da `workspacePaths`); in headless serve `--add-dir .`
perché agy trovi `.agents/hooks.json`; gli hook `PreToolUse`/`PostToolUse` in 1.1.26 headless non
scattano (il re-embed è coperto dal check a `Stop`). Config globale alternativa:
`~/.gemini/config/mcp_config.json` e `~/.gemini/config/hooks.json`.

### Context file generati (da `AGENTS.src.md`)

Il compose produce, oltre ad `AGENTS.md` (composed, letto nativo da Codex/Grok):
- `CLAUDE.md` = `@AGENTS.md`  (Claude Code)
- `GEMINI.md` = symlink → `AGENTS.md`  (Antigravity CLI / Gemini CLI)

Non editare i generati: il context vive in `AGENTS.src.md`.

> **Automatismi (hook).** Claude Code **e Grok CLI** hanno hook compatibili (`SessionStart/End`,
> `PreToolUse/PostToolUse`, … — JSON su stdin/stdout): gli automatismi raidho (context injection,
> journal, re-embed) reggono nativamente. **Codex**, **OpenCode** e **Antigravity** ci arrivano
> con un adapter (`hooks/codex_adapter.py`, `.opencode/plugin/raidho.js`, `hooks/antigravity_adapter.py`)
> che traduce i loro eventi nel contratto Claude Code. Un harness senza hook resta in "modo
> manuale": context statico nel file, pull dinamico e journal via tool MCP o bash-native — vedi la
> sezione *Bootstrap* nel context composto.
>
> **OpenCode (full mode)**: oltre alla config MCP `opencode.json` sopra, il plugin
> `.opencode/plugin/raidho.js` aggancia i lifecycle OpenCode agli hook Python raidho —
> `event(session.idle)`→journal, `tool.execute.after`→re-embed wiki, `chat.message`→context
> injection. Install: symlink/copia il plugin in `.opencode/plugin/` del progetto (o in
> `~/.config/opencode/plugin/`); imposta `RAIDHODEV_DIR` se vive fuori dal repo. Il plugin
> traduce i dati OpenCode nel formato JSONL che gli script CC già parsano → zero modifiche
> al Python condiviso. **Validato e2e su OpenCode 1.17.4** (loading · context injection ·
> re-embed · journal con summary). Debug opt-in: `RAIDHO_OC_DEBUG=1` → `/tmp/raidho-opencode.log`.
>
> Nota Codex: alcune versioni hanno avuto bug nel leggere `mcp_servers` da `config.toml`
> ([openai/codex#3441](https://github.com/openai/codex/issues/3441)) — verifica con la tua release.

### Install come plugin Codex (esperienza piena)

Oltre alla config MCP manuale, raidhodev è un **plugin Codex completo** — stesso core di CC
(`.codex-plugin/plugin.json` ↔ `.claude-plugin/plugin.json`, stesso `skills/` + `hooks.json`):
skills + MCP + **lifecycle hooks** (context injection a `SessionStart`, wiki re-embed a `PostToolUse`).

```bash
codex plugin marketplace add vincent-vigorito/raidhodev
# poi nel plugin browser della CLI: installa "raidho"
```

Dà gli automatismi come su CC. Nota: il journal a `SessionEnd` parsea il transcript nel formato
CC → su Codex può servire un adattamento del parser (gli altri hook funzionano a prescindere).

> ⚠️ Verificato su codex-cli 0.137: i plugin caricano le **skills**, ma gli **MCP server del
> manifest non vengono ancora avviati** dalla CLI → registra gli MCP via `config.toml`
> (project-scoped `.codex/config.toml` o `~/.codex/config.toml`, vedi sezione sopra) o
> `codex mcp add`. Il `.mcp.codex.json` del plugin resta per quando Codex li supporterà.

## Slash command

| Command | Descrizione |
|---|---|
| `/raidho-init` | Scaffolda `.raidhowiki/` (cold) o analizza codebase (analyze mode) |
| `/raidho-ingest <path\|url>` | Ingerisci fonte nel wiki strutturato |
| `/raidho-query <question>` | Interroga wiki, opzionale filing come analysis page |
| `/raidho-refresh` | Reconcile wiki ↔ codebase: diff vs last snapshot + update entity toccate |
| `/raidho-lint` | Health check: orfani, broken links, frontmatter, stale |
| `/raidho-status` | Riepilogo identità + counts + ultimo log |
| `/raidho-task add\|list\|done\|triage` | Gestione roadmap.md |
| `/raidho-config` | AskUserQuestion: provider + model embed (scrive in `.mcp.json`) |
| `/raidho-index-code` | Build/refresh vector index del codebase |
| `/raidho-steward` | Wiki steward: rivedi/applica le patch distillate dai journal, compact dei diari |
| `/raidho-upgrade` | Migra progetto/hub con wiki di versione precedente al layout corrente (triade + composed + MCP + schema-version) |
| `/raidho-evolve-skills` | Review auto-improvement delle skill (pattern Hermes): legge inbox PostToolUse, propone patch SKILL.md, applica dopo conferma |

<!-- raidho:tools:start -->
## MCP tools (59 totali via `mcp_memory_server`)

Esposti via stdio, filtrabili via env `RAIDHO_TOOL_GROUPS` (9 gruppi: `memory`, `sessions`, `soul`, `user`, `skills`, `wiki`, `roadmap`, `code`, `graph`). Sezione generata da `scripts/gen_tools_doc.py` dal registry del server: non editare a mano.

### Gruppo `memory` (3 tool)

| Tool | Descrizione |
|------|-------------|
| `memory.recall` | Cerca pagine wiki rilevanti per un topic (keyword grep+rank). |
| `memory.write` | Scrivi una nota in \<raw\>/notes/\<date\>-\<slug\>.md. |
| `memory.timeline` | 🕒 MEMORY aggregator temporale: combina log entries + sessions in una vista cronologica. |

### Gruppo `sessions` (3 tool)

| Tool | Descrizione |
|------|-------------|
| `sessions.list` | Lista sessioni recenti (chat + routine) ordinate per data desc. |
| `sessions.read` | Read full content di una specifica sessione. |
| `sessions.summarize` | 📝 Genera auto-summary on-demand per una sessione e lo scrive nella sezione `## Summary` del session file. |

### Gruppo `soul` (2 tool)

| Tool | Descrizione |
|------|-------------|
| `soul.show` | Read SOUL.md (identity + user preferences + memorable feedback + relationship facts). |
| `soul.update` | Append una entry a SOUL.md. |

### Gruppo `user` (2 tool)

| Tool | Descrizione |
|------|-------------|
| `user.read` | Read profilo utente — HOT (default, ~500 token, sempre sapevi questo già) o DETAIL on-demand. |
| `user.update` | Aggiorna profilo utente: append (default) o replace di una sezione. |

### Gruppo `skills` (11 tool)

| Tool | Descrizione |
|------|-------------|
| `skill.list` | Catalog skills disponibili (workflow plugin/hub/workspace). |
| `skill.load` | Carica body SKILL.md completo per uno skill specifico. |
| `skill.read_file` | Level 2: leggi un file di reference dentro la skill (references/, scripts/, templates/). |
| `skill.save` | Crea una nuova skill (Hermes skill_manage analog). |
| `skill.patch` | Patch mirato del SKILL.md via find/replace (preferito a edit, più sicuro). |
| `skill.history` | Lista backup disponibili di una skill (file in \<skill\>/.history/). |
| `skill.rollback` | Ripristina SKILL.md da un backup in .history/. |
| `skill.edit` | Riscrive l'intero SKILL.md. |
| `skill.delete` | Cancella una skill (rimuove la directory intera). |
| `skill.write_file` | Scrive un file di reference dentro la skill (references/, scripts/, templates/). |
| `skill.remove_file` | Rimuove un file di reference dalla skill. |

### Gruppo `wiki` (19 tool)

| Tool | Descrizione |
|------|-------------|
| `wiki.search` | 📚 Cerca nelle pagine del wiki di questo scope (project/hub/workspace). |
| `wiki.read` | 📚 Legge una pagina wiki per slug. |
| `wiki.upsert_entity` | 📝 WIKI write: crea o aggiorna una entity page (modulo, servizio, persona, prodotto, sistema esterno) in wiki/entities/\<slug\>.md. |
| `wiki.upsert_concept` | 📝 WIKI write: crea o aggiorna una concept page (pattern, idea, architettura, convenzione) in wiki/concepts/\<slug\>.md. |
| `wiki.upsert_source` | 📝 WIKI write: crea o aggiorna una source page in wiki/sources/\<slug\>.md (riassunto di una fonte ingerita: articolo, paper, doc, codebase-sn… |
| `wiki.upsert_analysis` | 📝 WIKI write: crea o aggiorna una analysis page in wiki/analysis/\<slug\>.md (query trasformata in pagina, confronti, lint report). |
| `wiki.update_overview` | 📝 WIKI write: aggiorna `wiki/overview.md` (sintesi di alto livello — 'cosa abbiamo capito'). |
| `wiki.index_update` | 📝 WIKI write: manutenzione di `wiki/index.md`. |
| `wiki.backlinks` | 🔍 WIKI nav: trova tutte le pagine che linkano allo slug via [[link]]. |
| `wiki.lint` | 🔍 WIKI health check: orfani (pagine non linkate da nessuno), broken_links ([[X]] dove X non esiste), stale (updated \> N giorni ma ancora at… |
| `wiki.verify` | Registra una verifica automatica sulla revisione corrente; conserva lo storico. |
| `wiki.rename` | ✏️ WIKI maintenance: rinomina una pagina preservando TUTTI i [[link]] cross-wiki (replace `[[old]]`, `[[old\|label]]`, `[[old#section]]` → `… |
| `wiki.replace_links` | ✏️ WIKI maintenance: replace `[[old]]` → `[[new]]` cross-wiki SENZA rinominare file. |
| `wiki.delete` | 🗑️ WIKI maintenance: cancella una pagina. |
| `wiki.tree` | 🌳 WIKI explore: struttura ad albero del wiki. |
| `wiki.stats` | 📊 WIKI explore: statistiche di salute del wiki. |
| `wiki.attach_image` | 🖼️ WIKI write: allega immagine a pagina entity/concept/source/analysis. |
| `wiki.export` | 📦 WIKI export: dump dell'intero wiki in formato md (zip), json (dump strutturato per import/training/tool esterni), o html (static site con… |
| `wiki.log_append` | 📝 WIKI write: append entry strict-format a wiki/log.md (memoria episodica). |

### Gruppo `roadmap` (6 tool)

| Tool | Descrizione |
|------|-------------|
| `roadmap.list` | 📋 ROADMAP: lista task del progetto da `wiki/roadmap.md`. |
| `roadmap.add` | 📋 ROADMAP: aggiungi nuovo task in stato open. |
| `roadmap.update` | 📋 ROADMAP: modifica metadata di un task per id. |
| `roadmap.complete` | 📋 ROADMAP: shortcut completion. |
| `roadmap.block` | 📋 ROADMAP: shortcut blocking. |
| `roadmap.archive` | 📋 ROADMAP: archivia task done più vecchi di N giorni (default 30) in `wiki/archive/roadmap-YYYY-QN.md`. |

### Gruppo `code` (5 tool)

| Tool | Descrizione |
|------|-------------|
| `code.compare_decision` | Compare a Markdown decision and selected current source files against an explicit full Git commit ID. |
| `code.inspect` | Inspect current Python source or explicit Markdown implementation declarations after code.search. |
| `code.search` | 🔎 CODE.SEARCH: ricerca nel codebase del progetto ospitante. |
| `code.reindex` | 🔎 CODE: build/refresh vector index per il codebase del progetto in `.raidhowiki/code-index.db`. |
| `code.status` | 🔎 CODE: stato del vector index del codebase. |

### Gruppo `graph` (8 tool)

| Tool | Descrizione |
|------|-------------|
| `wiki.find_duplicates` | 🔎 Trova coppie di pagine wiki semanticamente troppo simili (candidati duplicati / da fondere o contraddittorie) via embeddings condivisi. |
| `wiki.embed` | 🔗 GRAPH: embed incrementale delle pagine wiki nello stesso spazio vettoriale del code-index → abilita k-NN cross-kind (wiki ↔ code) via gra… |
| `graph.report` | 🔗 GRAPH: compute knowledge graph report (god nodes + clusters + surprise edges + wiki↔code anchors + orphans). |
| `graph.search_text` | 🔗 GRAPH: semantic search cross-kind via query libera. |
| `wiki.search_semantic` | 📚 WIKI: semantic search del wiki (sessions escluse di default). |
| `sessions.search_semantic` | 🧠 SESSIONS: semantic search nelle session journal. |
| `graph.html` | 🔗 GRAPH: genera `\<wiki\>/graph.html` standalone visualizer (Cytoscape). |
| `graph.semantic_neighbors` | 🔗 GRAPH: k-NN nello spazio embedding unificato wiki+code. |

<!-- raidho:tools:end -->

> **Nomi sul wire (v0.24+)**: i nomi canonici nelle tabelle sopra sono puntati (`wiki.read`), ma `tools/list`
> li emette **flat** (`wiki_read`) — Grok Build e i client OpenAI-style scartano i nomi col punto,
> Claude Code li mostrava già così (`mcp__raidho_memory__wiki_read`). `tools/call` accetta entrambe le forme.

> **Wiki steward (v0.23).** `scripts/steward.py --root <proj>` — triage senza LLM delle
> sessioni *worth* della settimana (cluster per giorno), **una** call LLM per cluster (max 5)
> che propone 0–3 patch in JSON, scrittura **fail-closed**: pagine esistenti solo in
> **append** (stamp `steward, data, cluster`), pagina nuova solo se la rationale cita ≥2
> session, overview mai riscritto (solo `## Recent` ≤80 parole; overview > 60 gg →
> `stale_after` +90d), `log_append` sempre; niente analysis/delete/rename/SOUL. Le session
> dei cluster diventano `distilled: true` e il compact le archivia dopo 14 gg. Modalità:
> dry-run (default) · `--propose` (scrive `.raidhowiki/.steward-pending.json`, nessuna
> scrittura wiki — è quello che fa il **lazy SessionStart ogni 24h**) · `--apply` (routine
> notturna) · `--apply-pending [id…]` (dopo la revisione con `/raidho-steward`). Lock 30 min,
> opt-out `RAIDHO_STEWARD=0`. Lo steward cura "cosa sa il repo", non l'identità
> dell'utente.
>
> **Sessioni ≠ wiki (v0.22).** I journal in `sessions/` sono diari, non conoscenza: `wiki.search`,
> `wiki.search_semantic`, `graph.search_text` (filter `all`/`wiki`) e l'embedding li escludono
> di default (`include_sessions=true` per includerli). Il hook SessionEnd **non journala** le
> sessioni-macchina (Agent SDK / `claude -p` / `RAIDHO_JOURNAL=0`, entrypoint `sdk-*`, 0 messaggi)
> e l'auto-summary parte solo se la sessione *vale* (≥3 messaggi, ≥5 min, e volume o tool di
> scrittura o parole di segnale). I diari si compattano senza LLM
> (`scripts/compact_sessions.py`, lazy a SessionStart ogni 24 h e dentro lo steward), con una
> **politica di ritenzione esplicita** (v0.30, `.raidhowiki/config.json` → `sessions`):
>
> | Sessione | Dopo | Cosa succede |
> |---|---|---|
> | short (< 3 msg o < 5 min) | 14 gg | archiviata come stub (`sessions/archive/`, Summary e transcript conservati) |
> | distilled dallo steward | 14 gg | archiviata |
> | worth mai distillata | 30 gg | archiviata col summary (prima restava attiva per sempre) |
> | stub archiviato senza summary | 180 gg | cancellato |
> | archivio oltre 500 stub | subito | via i più vecchi senza summary (cap soft: uno stub con summary non si cancella mai) |
>
> Lo steward guarda una finestra automatica dall'ultimo run (7–30 gg, non più 7 fissi), così
> nulla resta indietro se il progetto è usato a intermittenza. `last_compact` in `meta.yaml`
> e `/raidho-status` dicono quando è avvenuta l'ultima ottimizzazione.

> Dal **v0.21** questo server espone SOLO i gruppi core del plugin CLI. Se un
> `.mcp.json` vecchio elenca in `RAIDHO_TOOL_GROUPS` gruppi che non esistono piu'
> (`agents`, `tasks`, `workspace`, `kanban`, `goals`, `pp`), il server parte comunque e
> stampa un warning su stderr.

## Architettura

```
raidho/
├── .claude-plugin/plugin.json   # manifest plugin (versione — allineata da bump.sh)
├── .codex-plugin/plugin.json    # manifest plugin Codex
├── bump.sh                      # release: allinea le versioni nei 3 manifest in un colpo
├── commands/                    # 12 slash command (.md)
├── hooks/
│   ├── session_start.py         # carica focus roadmap + ultime 5 log
│   ├── session_end.py           # write session file + spawn auto-summary bg
│   ├── codex_adapter.py + antigravity_adapter.py   # traducono gli eventi Codex/agy nel contratto CC
│   └── journal_policy.py        # harness detection, sessioni-macchina, worth
├── agents/                      # subagent (wiki-maintainer)
├── scripts/
│   ├── mcp_memory_server.py     # entry point MCP server stdio (59 tool, 9 gruppi) → package raidho/
│   ├── raidho/                    # server.py (registry+dispatch), config.py, common.py, un modulo per dominio
│   │                            #   memory, sessions, soul, user, skills, wiki(+_maint,+_io), roadmap, code, graph
│   ├── mcp_code_server.py       # raidho_code: execute_python (opt-in RAIDHO_CODE_EXEC=1)
│   ├── steward.py + compact_sessions.py   # distill notturno dei journal + archivio
│   ├── code_db.py + code_index.py + code_search.py + embed_providers.py
│   ├── roadmap_io.py
│   ├── summarize_session_bg.py  # detached process per auto-summary
│   ├── init_project.py          # scaffolding /raidho-init
│   ├── gen_tools_doc.py         # sezione MCP tools del README generata dal registry (--check in CI)
│   ├── release_check.py         # coerenza versioni/conteggi/test prima di una release
│   └── ... (lint_checks, slugify, compose_claude_md, status, ecc.)
├── templates/
│   ├── project-skeleton/        # struttura .raidhowiki/ scaffoldata da /raidho-init
│   ├── soul-baselines/          # personality presets per type (dev/research/...)
│   └── triade-skeleton/         # AGENTS/SOUL/TOOLS scaffolding
├── skills/                      # skill descrittive workflow (ingest, query, lint, refresh, init-analyze)
├── tests/                       # pytest: registry, smoke su tutti i tool, steward, adapter Codex/OpenCode/Antigravity
├── SCHEMA.md                    # wire format pubblico .raidhowiki/
├── SECURITY.md                  # garanzie, assunzioni, cosa NON è garantito (raidho_code)
├── pyproject.toml               # config pytest / ruff / coverage (nessuna dipendenza runtime)
├── .github/workflows/ci.yml     # test matrix + lint + coerenza + coverage
└── README.md                    # questo file
```

### Classificazione componenti

| Classe | Cosa | Garanzia |
|--------|------|----------|
| **core** | `scripts/raidho/` (server MCP), `mcp_memory_server.py`, `hooks/`, `commands/`, `skills/`, `steward.py`, `compact_sessions.py`, `roadmap_io.py`, `wiki_embed.py`, `code_*.py`, `embed_providers.py`, `init_project.py`, `upgrade_triade.py`, `summarize_session_bg.py`, `lint_checks.py`, `context_loader.py`, `secrets_loader.py`, `slugify.py`, `skill_parser.py`, `tools_md.py`, `compose_claude_md.py`, `status.py` | wire e schema stabili entro la MAJOR, coperti dai test, in CI |
| **adapter** | `.codex-plugin/`, `.mcp.codex.json`, `hooks/codex_adapter.py`, `install_codex_hooks.py`, `.opencode/`, `.agents/plugins/` (Codex marketplace), `hooks/antigravity_adapter.py`, `install_antigravity.py` | best-effort, validati sul campo e con test di traduzione |
| **sperimentale** | `mcp_code_server.py` (`raidho_code`, opt-in), `graph_html.py` / `graph_report.py`, provider `local` | possono cambiare senza MAJOR; `raidho_code` non è un confine di sicurezza (SECURITY.md) |
| **legacy** | `migrate_cc_memory.py`, `cc_memory_to_soul.py`, `cc_memory_sync.py` (import della memoria nativa di Claude Code) | mantenuti finché servono alle migrazioni, esclusi dalla coverage |
| **dev** | `gen_tools_doc.py`, `release_check.py`, `bump.sh`, `tests/`, `pyproject.toml`, `.github/` | strumenti del repo, non distribuiti come funzionalità |

### Wire format pubblico

Il layout `.raidhowiki/` è un **contratto pubblico** descritto in [`SCHEMA.md`](./SCHEMA.md). Consumatori esterni (IDE plugin, tool di sync, script) possono assumere il layout, frontmatter required, formato log e wikilinks come stabili entro la stessa MAJOR version. Vedi anche `.raidhowiki/.schema-version` scritto da `/raidho-init`.

## Env vars

| Var | Default | Descrizione |
|---|---|---|
| `RAIDHO_SCOPE` | `project` | `project` \| `hub` \| `agent` — determina path resolution |
| `RAIDHO_ROOT` | — | Path del root scope (set da `.mcp.json` per ogni progetto) |
| `RAIDHO_TOOL_GROUPS` | tutti | CSV: `memory,sessions,soul,user,skills,wiki,roadmap,code,graph` — filtra tool MCP |
| `RAIDHO_EMBED_PROVIDER` | `openrouter` | `openrouter` \| `voyage` \| `openai` \| `local` \| `mock` (solo test, nessuna rete) \| `none` |
| `RAIDHO_CODE_EXEC` | — | `1` abilita `execute_python` in `raidho_code` (default: server attivo ma senza tool, vedi `SECURITY.md`) |
| `RAIDHO_EMBED_MODEL` | provider-default | es. `qwen/qwen3-embedding-8b` per openrouter |
| `RAIDHO_AUTO_SUMMARY` | `1` | `0` per disabilitare auto-summary background |
| `RAIDHO_SUMMARY_BIN` | harness → PATH | CLI per i summary: `claude` \| `grok` \| `codex` \| path \| `none` |
| `RAIDHO_SUMMARY_MODEL` | `haiku` | modello per `claude -p` |
| `RAIDHO_JOURNAL` | `1` | `0` = questa sessione non è un journal (lo settano gli spawner programmatici) |
| `RAIDHO_HARNESS` | auto | forza il harness nel journal (`claude` \| `grok` \| `codex` \| `opencode`) |
| `RAIDHO_STEWARD` | `1` | `0` = niente steward (lazy start, slash lo spiega) |
| `RAIDHO_STEWARD_BIN` | `RAIDHO_SUMMARY_BIN` → harness → PATH | CLI per il distill (`claude` \| `grok` \| `codex` \| path \| `none`) |
| `RAIDHO_STEWARD_MODEL` | `haiku` | modello per `claude -p` nel distill |
| `RAIDHO_STEWARD_EVERY_H` | `24` | ore fra due lazy start (`--propose`) |
| `RAIDHO_STEWARD_ARCHIVE_AFTER` | `14` | legacy: giorni per short/distilled (la policy completa è in `.raidhowiki/config.json` → `sessions`) |
| `RAIDHO_COMPACT` | `1` | `0` = niente compact lazy a SessionStart |
| `RAIDHO_COMPACT_EVERY_H` | `24` | ore fra due compact lazy |
| `RAIDHO_COMPACT_BUDGET` | `20` | max azioni (archive/purge) per compact lazy |
| `RAIDHO_HUB` | — | Override path hub (per scope=project che vuole user-global) |
| `RAIDHO_LOG` | — | `debug`: ogni eccezione gestita best-effort nel server viene tracciata su stderr (stdout resta solo JSON-RPC) |

## Filosofia

- **Stdlib first**: nessuna dipendenza esterna obbligatoria per il core (sqlite-vec + httpx opzionali per code search).
- **MCP-first**: ogni capability via tool stdio, token-controlled via `RAIDHO_TOOL_GROUPS`.
- **Edit minimali**: tre righe simili > astrazione prematura.
- **Niente commenti ovvi**: solo "perché" non ovvi.
- **Wiki self-maintained**: l'agent è responsabile dell'igiene (lint, rename, dedup) come prima cittadina.

## Dev setup (per contributor)

```bash
git clone git@github.com:vincent-vigorito/raidhodev.git ~/Documents/raidhodev
cd ~/Documents/raidhodev

# Il repo È il plugin (root = plugin). Editing diretto sui file. Nessun build step.
# Per testare in un progetto reale:
cd ~/Documents/my-project
/plugin marketplace add ~/Documents/raidhodev
/plugin install raidho@raidhodev
/raidho-init --type dev
```

> **Release**: dopo ogni modifica da distribuire, `./bump.sh <major.minor.patch>` allinea
> la versione nei 3 manifest, nel README (riga Stato) e in `SERVER_VERSION` dei due server, poi
> esegue `scripts/release_check.py`; poi commit + `git tag vX.Y.Z`. Senza bump, CC vede "already at
> latest" e continua a caricare la cache pre-modifica (versiona per numero, non per git SHA).

### Workflow dev tipico

| Modifica | Come ricaricare |
|---|---|
| MCP server (`scripts/mcp_*.py`) | Nuova chat in CC (subprocess MCP rispawna) |
| Slash command (`commands/*.md`) | Nuova chat |
| Hook (`hooks/*.py`) | Nuova chat (hook caricato a `SessionStart`) |
| Template (`templates/`) | Nessun reload; effetto su prossimo `/raidho-init` |

### Test, lint, coverage

```bash
python3 -m pytest                      # tutta la suite (ogni file gira anche standalone: python3 tests/test_x.py)
RAIDHO_TEST_PYTHON=/opt/homebrew/opt/python@3.12/bin/python3.12 python3 -m pytest   # sottoprocessi con un altro interprete
python3 -m ruff check .                # lint (config in pyproject.toml)
RAIDHO_COV_ROOT=$PWD COVERAGE_FILE=$PWD/.coverage COVERAGE_PROCESS_START=$PWD/pyproject.toml \
  PYTHONPATH=$PWD/tests/_coverage_hook python3 -m coverage run -m pytest && python3 -m coverage combine && python3 -m coverage report
python3 scripts/gen_tools_doc.py --check    # sezione MCP tools del README == registry
python3 scripts/release_check.py            # versioni, conteggi, test raccoglibili
```

Suite: `test_registry` (TOOLS ↔ gruppi ↔ handler ↔ nomi wire), `test_mcp_smoke` (ogni tool del
registry chiamato sul wire, copertura obbligatoria), `test_embed_mock` (pipeline embedding con
`RAIDHO_EMBED_PROVIDER=mock`; skip se sqlite-vec non è caricabile), `test_code_server` (sandbox di
`execute_python`), `test_steward`, `test_journal_policy`, `test_compact_sessions`, `test_core_split`,
adapter Codex/OpenCode. La CI (`.github/workflows/ci.yml`) esegue tutto su 3.9/3.10/3.12 ×
ubuntu/macos, più ruff, i check di coerenza e la coverage (sottoprocessi inclusi) con soglia.

### Convenzioni codice

- Python 3.9+: typing moderno (`X | None`, `list[T]`) va bene grazie a `from __future__ import annotations` in testa a ogni file
- Solo stdlib nel core. Eccezioni motivate: `sqlite-vec`, `httpx` (opt-in per code search)
- File <1000 LOC: il server è un package (`scripts/raidho/`), un modulo per dominio; `TOOLS` di ogni modulo porta gli schemi dei suoi tool e `raidho.server` li aggrega in `MODULE_ORDER` (= ordine sul wire)
- Niente `except Exception: pass` muto: usa `log_exc("modulo.funzione", exc)` da `raidho.config` (visibile con `RAIDHO_LOG=debug`)
- Tool MCP: handler `def tool_<group>_<name>(args: dict) -> dict`, return JSON-serializable, errors come `{"error": "msg", "hint": "..."}`

## Licenza

[MIT](./LICENSE) © 2026 Vincent Vigorito
