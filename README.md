# Hassgram

Bot Telegram per controllare Home Assistant tramite le sue REST API.
**Bilingue: parla italiano e inglese**, riconosce la lingua da come gli scrivi e
risponde nella stessa — vocali compresi.

*A Telegram bot to control Home Assistant. It speaks both Italian and English:
write in either language and it follows you. See [docs/](docs/).*

## Configurazione

Le credenziali sono lette da `.env` (già presente):

| variabile | uso |
|---|---|
| `HOME_ASSISTANT_API_URL` | endpoint API, es. `http://homeassistant.local:8123/api/` |
| `HOME_ASSISTANT_API_ACCESS_TOKEN` | long-lived access token di Home Assistant |
| `TELEGRAM_BOT_TOKEN` | token del bot (@BotFather) |
| `TELEGRAM_CHAT_ID` | chat autorizzate, separate da virgola. Se vuoto, il bot risponde a chiunque |
| `HA_STT_ENTITY` | *(opzionale)* entità speech-to-text, es. `stt.google_ai_stt`. Se assente ne viene scelta una automaticamente |
| `BOT_LANGUAGE` | *(opzionale)* lingua iniziale di una chat nuova, `it` o `en`. Default `it` |
| `STT_LANGUAGE_IT` | *(opzionale)* lingua dei vocali italiani, default `it-IT`. `STT_LANGUAGE` resta accettato come sinonimo |
| `STT_LANGUAGE_EN` | *(opzionale)* lingua dei vocali inglesi, default `en-US` |
| `RUNNABLES_REFRESH_SECONDS` | *(opzionale)* ogni quanto rileggere scene, script e automazioni. Default `300`; `0` disattiva il ciclo e legge solo all'avvio |

## Avvio

```bash
pip install -r requirements.txt
python3 bot.py
```

## Test

```bash
python3 -m unittest discover
```

L'intera suite, senza dipendenze aggiuntive: niente rete, niente Telegram,
niente Home Assistant. Dettagli in [docs/development.md](docs/development.md#testing).

## Comandi

| comando | cosa fa |
|---|---|
| `/luci` | riepilogo per stanza + tastiera; toccando una stanza si vedono le sue luci con toggle |
| `/luci salone` | solo le luci che corrispondono a «salone» |
| `/accese` | tutte le luci accese in questo momento, raggruppate per stanza |
| `/accendi studio` | accende una luce, un'intera stanza, o tutta la casa con `/accendi casa` |
| `/spegni luciCucina` | spegne; se il nome è ambiguo propone una scelta a bottoni |
| `/temperatura` | temperature e umidità di tutte le stanze (come `/temperatura casa`) |
| `/temperatura bagno` | solo quella stanza |
| `/stato <nome>` | stato di una qualsiasi entità (anche sensori, prese, climate) |
| `/esegui` | elenca scene, script e automazioni, con un bottone per ciascuno |
| `/esegui cinema` | esegue quella scena, quello script o quell'automazione |
| `/lingua it\|en` | fissa la lingua della chat (`/language` è lo stesso comando) |

Ogni comando ha un alias inglese: `/lights`, `/whatson`, `/on`, `/off`,
`/temperature`, `/state`, `/run`, `/language`. All'avvio il bot pubblica il
**menu comandi di Telegram** (quello che compare digitando `/`) in entrambe le
lingue: un client impostato in italiano vede `/luci`, uno in inglese `/lights`. **Il nome che usi è già un segnale di
lingua**: `/luci` risponde in italiano, `/lights` in inglese.

**«casa» vale come tutte le stanze insieme** — valgono anche *tutto*, *tutta la casa*, *tutte le stanze*,
*ovunque*, *appartamento*. Nelle azioni in blocco (casa o stanza intera) le luci `unavailable` vengono escluse,
così il conteggio nella risposta è quello reale.

Funziona anche in linguaggio naturale: *«accendi la luce dello studio»*, *«spegni le luci del salone»*,
*«che temperatura c'è in camera da letto?»*, *«quanti gradi in salone»*, *«accendi tutto»*, *«spegni tutte le luci»*,
*«esegui la scena cinema»*, *«lancia lo script buonanotte»*.

### Scene, script e automazioni

`/esegui` è l'unico comando che **avvia** qualcosa invece di accenderlo, e sceglie il servizio giusto per
ogni dominio: `scene.turn_on` per una scena, `script.turn_on` per uno script e `automation.trigger` per
un'automazione — non `automation.turn_on`, che si limiterebbe ad *abilitarla* senza eseguirla.
Un'automazione disattivata resta eseguibile a mano ed è segnalata come tale nell'elenco.
L'elenco viene **letto all'avvio e poi rinfrescato a ciclo** (`RUNNABLES_REFRESH_SECONDS`, default 300 s):
`/esegui` risponde dalla memoria, quindi il menu resta disponibile anche se Home Assistant è
momentaneamente irraggiungibile — è l'esecuzione vera e propria a fallire, non l'elenco.
Il rovescio della medaglia: se abiliti o disabiliti un'automazione da Home Assistant, l'indicazione
nell'elenco si aggiorna al giro successivo.
Scene, script e automazioni non fanno mai parte di `/accendi`, `/spegni` o `/luci`: «spegni casa» non può
raggiungerle.

## Bilingue 🇮🇹 🇬🇧

Scrivi in inglese e il bot passa all'inglese, senza configurare niente:
*«turn on the light in the study»*, *«turn everything off»*, *«how warm is it in
the bedroom?»*, *«which lights are on»*, *«run the cinema scene»*. La lingua riconosciuta diventa quella
della chat, quindi valgono anche per i bottoni e per i vocali; `/lingua it` o
`/language en` la fissano a mano.

Il riconoscimento guarda parole caratteristiche di ciascuna lingua: se il
messaggio è troppo corto per decidere (*«salone»*) la chat resta dov'era, invece
di cambiare lingua su un indizio debole.

## Comandi vocali 🎙

Manda un **messaggio vocale** (o un audio, o un video-messaggio) con lo stesso comando che scriveresti:
il bot lo trascrive e lo esegue, rispondendo prima con il testo riconosciuto così vedi cosa ha capito.

I vocali vengono trascritti nella **lingua corrente della chat**: se la chat è in
inglese il bot chiede a Home Assistant una trascrizione `en-US`, altrimenti
`it-IT`. Per dettare nell'altra lingua basta scrivere un messaggio in quella
lingua, o usare `/language`, prima di registrare.

La trascrizione usa lo **speech-to-text già presente in Home Assistant** (`POST /api/stt/<entity_id>`),
quindi nessun servizio esterno in più e nessuna chiave aggiuntiva. Sul tuo impianto viene rilevato
`stt.google_ai_stt`, che accetta ogg/opus — lo stesso formato dei vocali Telegram — quindi **non serve
ffmpeg né alcuna conversione**. I metadati vanno nell'header `X-Speech-Content`; Home Assistant accetta
solo `sample_rate=16000`, ma il contenitore ogg porta con sé il proprio sample rate (48 kHz per Telegram)
e il provider lo decodifica correttamente.

Se in Home Assistant non c'è nessuna entità `stt.`, i comandi scritti continuano a funzionare e ai vocali
il bot risponde spiegando che manca il motore di trascrizione.

## Documentazione

La documentazione completa è in [docs/](docs/) (in inglese, come il codice):

| documento | contenuto |
|---|---|
| [docs/configuration.md](docs/configuration.md) | installazione, variabili d'ambiente, come ottenere token e chat id |
| [docs/usage.md](docs/usage.md) | comandi, linguaggio naturale, bottoni, ricerca fuzzy |
| [docs/architecture.md](docs/architecture.md) | moduli, flusso di una richiesta, cache, token dei callback, limiti di Telegram |
| [docs/voice.md](docs/voice.md) | pipeline dei vocali, la questione del sample rate, diagnostica |
| [docs/api-reference.md](docs/api-reference.md) | firme e sommari di ogni funzione |
| [docs/operations.md](docs/operations.md) | systemd, log, runbook dei guasti, note di sicurezza |
| [docs/development.md](docs/development.md) | convenzioni, test, come aggiungere comandi e frasi |

## Come è fatto

- [ha_client.py](ha_client.py) — client async su `httpx`: `/api/states`, `/api/services/<domain>/<service>`,
  `/api/template`. Le aree non sono esposte dalla REST API, quindi la mappa `entity_id → stanza`
  viene renderizzata con un template Jinja lato Home Assistant e messa in cache.
  Gli stati hanno una cache di 5 secondi, invalidata a ogni chiamata di servizio.
- [entities.py](entities.py) — ricerca fuzzy (nome, entity_id, stanza) e formattazione.
- [i18n.py](i18n.py) — catalogo dei messaggi, rilevatore di lingua e le due grammatiche.
  Nessuna stringa rivolta all'utente vive fuori da qui.
- [bot.py](bot.py) — comandi, tastiere inline, vocali e parsing del linguaggio naturale.
  Testo e vocali confluiscono nello stesso interprete (`_dispatch_text`).
  I `callback_data` sono token brevi (limite Telegram: 64 byte) risolti in una mappa LRU in memoria:
  oltre le ultime 2000 voci i token più vecchi decadono e il bot risponde «sessione scaduta».
  Ogni risposta passa da un unico helper che tronca su un confine di riga, così non si supera
  il limite di 4096 caratteri di Telegram; un error handler globale trasforma qualunque
  eccezione (Home Assistant irraggiungibile compreso) in un messaggio all'utente.

## Docker

L'immagine ufficiale è [`lordraw/hassgram`](https://hub.docker.com/r/lordraw/hassgram)
(`linux/amd64`, `arm64`, `arm/v7`). Non espone porte e non usa volumi: il bot fa
solo connessioni in uscita e non tiene niente su disco.

```bash
docker run -d --name hassgram --restart unless-stopped \
  --env-file .env lordraw/hassgram:latest
```

Oppure con compose, partendo da [docker-compose.yml](docker-compose.yml):

```bash
cp .env.example .env   # e riempilo
docker compose up -d
```

### Build e pubblicazione

Il [Makefile](Makefile) prende la versione da git, quindi un tag `:X.Y.Z` su
Docker Hub corrisponde sempre a un tag git:

| comando | cosa fa |
|---|---|
| `make build` | costruisce l'immagine locale, taggata `:<versione>` e `:latest` |
| `make run` | la costruisce e la avvia con il `.env` locale |
| `make push` | pubblica `:latest` multi-arch |
| `make release` | pubblica `:<tag git>` e `:latest`; fallisce se HEAD non è su un tag |
| `make tag V=1.2.3` | crea e pusha il tag git, poi fa `release` |
| `make version` | mostra cosa pubblicherebbe questo checkout |

`make push` e `make release` usano `docker buildx` (il builder viene creato al
volo) e richiedono un `docker login`, disponibile anche come `make login`.

La descrizione da incollare su Docker Hub — overview lunga e short description —
sta in [DOCKERHUB.md](DOCKERHUB.md).

## Esecuzione come servizio

```bash
cp hassgram.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now hassgram
```
