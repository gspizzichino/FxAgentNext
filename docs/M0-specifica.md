# FxAgent Next — M0: specifica e design

Stato: **proposta, in attesa di approvazione** · Data: 2026-10-20 · Milestone: M0

Questo documento è la specifica M0. Non contiene codice applicativo. Contratti
dettagliati in [contracts.md](contracts.md), decisioni in [adr/](adr/README.md).

---

## 0. Sintesi

FxAgent Next è un **modular monolith Python 3.12** con un **core deterministico
puro** (`domain` + `strategy`) che non conosce broker, database, UI o LLM. Lo
stesso `evaluate()` viene chiamato da replay, backtest, paper e runtime MT5:
cambia solo chi costruisce lo snapshot e chi consuma il risultato.

Tre idee portanti:

1. **Valutazione guidata dall'orologio, non dai dati.** Il replay pianifica una
   valutazione per ogni chiusura attesa di barra secondo il calendario FX. Se la
   barra manca, la valutazione esiste comunque e termina in astensione
   `DATA_GAP`. Così un buco nei dati non può mai sparire silenziosamente.
2. **Disponibilità esplicita.** Ogni barra ha `open_time`, `close_time` e
   `available_at`. Uno snapshot al tempo `t` contiene solo barre con
   `available_at <= t`. Le barre incomplete non esistono per il core.
3. **Identità e hash ovunque.** Dataset, configurazione, strategia, snapshot e
   valutazione hanno un identificativo stabile derivato dal contenuto. Due run
   con gli stessi input producono gli stessi hash, verificabili da un test.

M1 consegna soltanto: import e validazione dati storici, dataset immutabili,
calendario FX, snapshot point-in-time, una baseline Donchian breakout senza
filtri discrezionali, audit strutturato e replay deterministico riprendibile.
Niente simulatore, niente MT5, niente risk/execution, niente AI, niente dashboard.

---

## 1. Cosa insegna il vecchio FxAgent (lettura in sola lettura)

Ho letto `gspizzichino/FxAgent` (33 commit tra 2026-09-16 e 2026-10-12, circa
91.000 righe Python, 34 tabelle SQLite in un unico DB). Non riuso codice né
architettura; ne ricavo requisiti e failure mode concreti.

| # | Evidenza nel vecchio repo | Requisito che ne deriva per Next |
|---|---|---|
| L1 | `execution/manager.py::_place_with_retry` ritenta fino a `BROKER_MAX_RETRIES=3` anche su `BrokerConnectionError`, che `_send_order` solleva quando `order_send` restituisce `None` (esito ignoto). Solo il percorso Strategy Lab disattiva il retry. | Nessun retry di un invio con esito ignoto, in nessun percorso. Stato `UNKNOWN` + riconciliazione obbligatoria (ADR-0007). |
| L2 | `MT5_SERVER_TIMEZONE` statico (default `UTC`) usato per convertire i timestamp server. I server MT5 tipicamente usano UTC+2/+3 con DST. | Offset server rilevato e verificato a runtime contro il tick, mai solo configurato; in caso di discordanza le entrate si sospendono (ADR-0002). |
| L3 | Research Bull/Bear su LLM nel percorso che seleziona o astiene il setup, con circuit breaker e fallback `HOLD`. | Nessun LLM nel percorso decisionale. L'AI produce solo proposte offline (ADR-0008). |
| L4 | Logiche separate per detector legacy, Strategy Lab e backtest (`_exit_on_bar` ha la propria logica di uscita, spread costante attorno a un midpoint sintetico). | Una sola implementazione della strategia; il simulatore è un modulo distinto, versionato, che dichiara le sue assunzioni (ADR-0003, ADR-0005). |
| L5 | Etichette di outcome v1 che "attraversavano" buchi nei dati, sostituite da v2 contigua; righe v1 non confrontabili. | Gap-awareness dal giorno uno, contratti di outcome versionati prima di produrre dati. |
| L6 | Cambio account del 7 ottobre 2026 16:22 UTC che ha reso osservazioni non attribuibili. | Ogni record operativo è legato a un'identità account autoritativa; un cambio account blocca le entrate. |
| L7 | Metriche di funnel ambigue (`qualified` significava `detected` nella v1). | Definizioni di metriche e denominatori scritte e versionate prima degli esperimenti. |
| L8 | Score fisso 0.75 usato come proxy di ammissione, facilmente confuso con una probabilità. | Nessuno score senza semantica statistica dichiarata; filtri come predicati con misura e soglia. |
| L9 | La dashboard Streamlit è il punto d'ingresso che avvia il worker; IPC ZeroMQ volatile tra worker e Position Manager. | Motore avviabile da CLI senza UI; il DB/broker è la verità, non i messaggi in volo. Dashboard read-only e opzionale. |
| L10 | Complessità di governance (PolicyCandidate con 7 commit di "hardening", shadow, challenger, deployment contract) cresciuta prima di avere evidenza di edge. | Principio 9: nessuna funzionalità senza requisito misurabile. Prima la qualità della baseline e del motore di ricerca. |
| L11 | Livelli SL/TP arrotondati dal broker e distanze minime di stop; il Lab rifiuta arrotondamenti verso l'esterno. | Arrotondamento solo verso l'interno o rifiuto; preflight esplicito (M3). |
| L12 | Ritardo operativo ≥ ciclo → entry scaduta. | Ogni candidato ha `expires_at`; nessuna entrata tardiva. |
| L13 | Barre piatte di weekend/fill mancanti negli export Dukascopy e deal broker mancanti negli export. | Validazione dati con report di qualità; nulla è scartato senza conteggio e motivo. |

Cosa **non** porto: i quattro detector legacy, S1/S2/S3, Bull/Bear, sentiment,
macro, PolicyCandidate/shadow governance, ZeroMQ, Streamlit come control plane.

---

## 2. Requisiti

### 2.1 Funzionali

| ID | Requisito | Milestone |
|---|---|---|
| FR-D1 | Importare dati OHLC storici da file (formato Dukascopy CSV e formato generico) per BID e, se disponibile, ASK. | M1 |
| FR-D2 | Validare ogni dataset: ordinamento, duplicati identici/conflittuali, timestamp fuori griglia, coerenza OHLC, prezzi non positivi/non finiti, ask < bid, barre piatte a volume zero, gap. Produrre un report di qualità persistito. | M1 |
| FR-D3 | Archiviare dataset immutabili e versionati (Parquet + manifest con hash del contenuto e della sorgente). | M1 |
| FR-D4 | Derivare H1/H4 da M15 solo con copertura completa; barre parziali assenti e marcate. | M1 |
| FR-D5 | Calendario FX: sessione settimanale ancorata alle 17:00 America/New_York, DST, chiusure attese configurabili e versionate. | M1 |
| FR-D6 | Costruire snapshot point-in-time con solo barre disponibili e chiuse. | M1 |
| FR-S1 | Interfaccia `evaluate(market_snapshot, strategy_config, strategy_state) -> StrategyEvaluation`. | M1 |
| FR-S2 | Ogni valutazione termina con esattamente un candidato oppure un'astensione con codice strutturato. | M1 |
| FR-S3 | Ogni filtro o gate registra nome, misura, soglia, comparatore, esito e codice motivo. | M1 |
| FR-S4 | Una baseline deterministica (Donchian breakout H1). | M1 |
| FR-A1 | Audit strutturato di ogni valutazione, interrogabile ("perché EURUSD non ha generato un trade alle 10:15?"). | M1 |
| FR-R1 | Replay cronologico che usa lo stesso Strategy Engine, riprendibile dopo interruzione con risultato identico. | M1 |
| FR-R2 | Simulatore con modello costi (spread, commissioni, slippage, latenza, fill, ambiguità intrabar) versionato. | M2 |
| FR-R3 | Metriche in R, walk-forward, OOS, holdout, sensitivity/ablation, benchmark nulli. | M2 |
| FR-K1 | Risk deterministico: sizing, esposizione per valuta, leva, rischio aggregato, stop giornaliero. | M3 |
| FR-E1 | Execution MT5 con outbox degli intenti, preflight, gestione esito ignoto, riconciliazione. | M3 |
| FR-P1 | Position management riconciliato con il broker; SL/TP broker-side; protezione indipendente dalla generazione segnali. | M3 |
| FR-K2 | Kill switch persistente che blocca nuove entrate senza toccare la protezione delle posizioni. | M3 |
| FR-AI1 | Agente di ricerca asincrono che produce proposte e specifiche di test, mai configurazione attiva. | M4 |
| FR-UI1 | Dashboard read-only. | M4 |

### 2.2 Non funzionali

| ID | Requisito | Verifica |
|---|---|---|
| NFR-1 Determinismo | Stessi dataset + config + versione codice → stesso hash di output del run, su Linux e Windows. | Test di regressione con hash attesi, CI su entrambe le piattaforme. |
| NFR-2 No lookahead | Modificare o aggiungere dati dopo `as_of` non cambia la valutazione a `as_of`. | Property test "perturbazione del futuro". |
| NFR-3 Offline | Tutta M1/M2 gira senza rete e senza MT5, su Windows. | CI senza rete; MT5 non è una dipendenza di installazione base. |
| NFR-4 Isolamento | `domain` e `strategy` non importano I/O, DB, broker, LLM, UI. | Test di architettura (import-linter) in CI. |
| NFR-5 Tracciabilità | Ogni output referenzia dataset_id, config_hash, strategy_version, engine_version, git sha. | Schema del manifest di run validato. |
| NFR-6 Sicurezza | Trading reale disattivato per default; credenziali mai nel repo; dipendenze bloccate da lockfile. | Test di config + scansione segreti. |
| NFR-7 Semplicità | Nessun servizio esterno (Redis, code, K8s, orchestratori AI). Un comando per installare, uno per testare. | Review. |
| NFR-8 Prestazioni (soglia minima, non obiettivo) | Replay della baseline su 10 anni H1 di una coppia in meno di 5 minuti su un portatile. | Benchmark informativo in M1, non bloccante salvo regressioni ×2. |

---

## 3. Architettura proposta

### 3.1 Forma

Un solo package Python `fxnext`, un processo per comando (CLI). Nessun servizio.
Moduli con regola di dipendenza verso l'interno: il core non dipende da nulla
di esterno, gli adapter dipendono dal core.

```
                 ┌────────────── apps (CLI, poi runtime e dashboard) ──────────────┐
                 │                                                                 │
   adapters ─────┤  data.sources   research.sim   execution.mt5   ai.research     │
   (I/O)         │  data.store     audit.store    ops.sqlite      dashboard        │
                 │                                                                 │
   services ─────┤  data.validate  data.snapshot  replay   risk   positions        │
                 │                                                                 │
   core puro ────┤               domain (tipi, tempo, calendario)                  │
                 │               strategy (evaluate, baseline, registry)          │
                 └─────────────────────────────────────────────────────────────────┘
          Le frecce vanno solo verso il basso: nessun modulo del core importa i livelli sopra.
```

### 3.2 Moduli e responsabilità

| Modulo | Responsabilità | Dipende da | Milestone |
|---|---|---|---|
| `fxnext.domain` | Tipi immutabili (Instrument, Timeframe, Bar, BarSeries, Quote, MarketSnapshot, StrategyEvaluation, FilterResult, ReasonCode), modello del tempo, calendario FX, hashing canonico. | stdlib, pydantic, numpy | M1 |
| `fxnext.strategy` | Protocollo `Strategy`, registry versionato, baseline, indicatori puri (ATR, canali). | domain | M1 |
| `fxnext.data.sources` | Adapter di lettura: Dukascopy CSV, CSV generico. In M3 adapter MT5 storico. | domain | M1 |
| `fxnext.data.validate` | Regole di validazione e report di qualità. | domain | M1 |
| `fxnext.data.store` | Dataset Parquet immutabili + manifest; lettura per intervallo. | domain, pyarrow | M1 |
| `fxnext.data.snapshot` | Costruzione snapshot point-in-time e aggregazione HTF. | domain | M1 |
| `fxnext.replay` | Scheduler guidato dal calendario, loop cronologico, checkpoint e ripresa. | domain, strategy, data, audit | M1 |
| `fxnext.audit` | Record di valutazione append-only, manifest di run, query `explain`. | domain, pyarrow, duckdb | M1 |
| `fxnext.research` | Simulatore, outcome, metriche, walk-forward, report. | domain, strategy, replay | M2 |
| `fxnext.risk` | Sizing e limiti deterministici. | domain | M3 |
| `fxnext.execution` | Outbox intenti, preflight, porta `Broker`, adapter MT5, broker simulato. | domain, risk, ops | M3 |
| `fxnext.positions` | Riconciliazione, protezione, sospensione entrate. | domain, execution, ops | M3 |
| `fxnext.ops` | Stato operativo SQLite minimo (intenti, posizioni osservate, kill switch, binding account). | domain, sqlite3 | M3 |
| `fxnext.ai` | Agente di ricerca: legge risultati, produce proposte in un formato deterministico. | research (sola lettura) | M4 |
| `fxnext.dashboard` | UI read-only. | audit, research, ops (sola lettura) | M4 |

### 3.3 Interfacce principali (porte)

Interfacce piccole e tipizzate (`typing.Protocol`). Dettaglio in [contracts.md](contracts.md).

- `Strategy.evaluate(snapshot, config, state) -> StrategyEvaluation` (puro).
- `BarSource.read(request) -> RawBars` (adapter dati).
- `DatasetStore.put(bars, manifest) -> DatasetRef` / `get(ref, range) -> BarSeries`.
- `SnapshotBuilder.build(symbol, as_of, requirements) -> MarketSnapshot`.
- `AuditSink.write(evaluations)` idempotente per `evaluation_id`.
- M2: `ExecutionModel.simulate(candidate, future_path) -> SimulatedFill/Outcome`.
- M3: `Broker` (account_identity, symbol_spec, tick, positions, orders/deals history, send, modify, close) con implementazioni `MT5Broker` e `SimulatedBroker`.

### 3.4 Flusso di una valutazione (identico in replay e runtime)

```
clock/calendario ──> as_of ──> SnapshotBuilder (solo available_at <= as_of)
                                   │
                                   ▼
                     Strategy.evaluate (puro) ──> StrategyEvaluation
                                   │
             ┌─────────────────────┼──────────────────────┐
             ▼                     ▼                      ▼
         AuditSink         (M2) Simulatore          (M3) Risk → Execution
```

In runtime (M3) cambia solo l'origine dello snapshot (feed MT5 + tick fresco) e
il consumatore (Risk/Execution). M2 include un test di parità: stessi snapshot
→ stesse valutazioni in replay e nel runtime con broker simulato.

---

## 4. Modello del tempo e dei dati

### 4.1 Tempo

- Unica rappresentazione interna: `datetime` timezone-aware in UTC (validato da
  Pydantic, naive rifiutati). Serializzazione: interi in nanosecondi UTC in
  Parquet, ISO-8601 `Z` in JSON.
- **Barra**: `open_time` (inclusivo), `close_time = open_time + timeframe`
  (esclusivo), `available_at >= close_time`. Per dati storici `available_at =
  close_time + publication_delay` (default 0, configurabile e registrato). In
  runtime `available_at` è l'istante in cui la barra chiusa è stata osservata.
- **Snapshot** a `as_of`: contiene solo barre con `available_at <= as_of`.
  Le barre in formazione non sono mai visibili al core.
- **Candidato**: `available_at` (quando la decisione è nota) e `expires_at`
  (oltre il quale non è più eseguibile). Nella baseline `expires_at = as_of + 1
  barra del timeframe di segnale`.
- **Ex post**: outcome, fill e costi stanno in tabelle separate (M2/M3) e non
  sono mai input di `evaluate`.

### 4.2 Calendario FX

- Settimana di trading: dalla domenica 17:00 America/New_York al venerdì 17:00
  America/New_York. Usare il fuso di New York gestisce automaticamente il DST
  (21:00 UTC in estate USA, 22:00 UTC in inverno). Le settimane in cui USA ed
  Europa cambiano ora in date diverse sono coperte da test dedicati.
- Chiusure attese (25 dicembre, 1 gennaio, eventuali altre) in un file di
  calendario versionato; il calendario ha un `calendar_version` incluso negli hash.
- Una barra attesa e mancante fuori dalle chiusure è un **gap inatteso**; una
  barra mancante in una chiusura attesa è **assenza attesa**. Entrambe sono
  conteggiate; solo la prima causa astensione `DATA_GAP`.

### 4.3 Timeframe e allineamento

- Base: M15 (BID e, se presente, ASK). H1 e H4 derivati da M15 con copertura
  completa (4 e 16 barre). Barre derivate parziali non vengono create.
- H4 allineato alla chiusura di New York (confini 17, 21, 01, 05, 09, 13 ora di
  NY), coerente con i broker MT5 a UTC+2/+3 (ADR-0009). Alternativa in §9.
- Ogni barra derivata porta `source_dataset_id` e il conteggio delle barre sorgente.

### 4.4 Base di prezzo e quote

- Ogni serie dichiara `price_basis`: `BID`, `ASK` o `MID`. Il core non mescola basi.
- Se il dataset ha solo BID, lo spread **non** viene inventato come dato: il
  simulatore (M2) applica un modello di spread esplicito e marca il risultato
  come `MODELLED`. Una `Quote` ha sempre `provenance ∈ {OBSERVED, MODELLED}`.
- Barre piatte a volume zero (tipiche dei riempimenti di weekend nei dati
  Dukascopy) sono marcate `NO_TRADE` ed escluse dagli indicatori, con conteggio.

### 4.5 Dataset

- Parquet, una partizione per `(symbol, timeframe, price_basis, anno)`.
- `dataset_id = sha256` del contenuto canonico (colonne ordinate, tipi fissati,
  righe ordinate per `open_time`). Il manifest JSON contiene sorgente, hash dei
  file grezzi, intervallo, conteggi, report di qualità, versione dello schema,
  versione del validatore.
- Immutabile: una correzione produce un nuovo `dataset_id`. Nessuna modifica in
  place. Il DB operativo del vecchio FxAgent non viene mai letto.

### 4.6 Identità

| Oggetto | Identificativo |
|---|---|
| Configurazione strategia | `config_hash = sha256(JSON canonico dei parametri + strategy_id + strategy_version)` |
| Snapshot | `snapshot_hash` sulle barre visibili (solo finestra richiesta) + quote + calendar_version |
| Evento strutturale | `event_id = sha256(strategy_id, strategy_version, symbol, timeframe, open_time barra evento, direzione)` |
| Valutazione | `evaluation_id = sha256(strategy_id, strategy_version, config_hash, symbol, as_of)` |
| Run di replay | `run_id` casuale + `run_fingerprint = sha256(dataset_ids, config_hash, engine_version, calendar_version, intervallo)` |
| Output del run | `output_hash` sull'elenco ordinato delle valutazioni canoniche |

`evaluation_id` non include lo snapshot_hash di proposito: la stessa domanda
(strategia, config, simbolo, istante) ha un solo identificativo, e se due run
producono snapshot diversi per lo stesso `evaluation_id` è un errore rilevabile.

---

## 5. Contratto del Strategy Engine (sintesi)

`StrategyEvaluation` contiene: identificativi e versioni; `as_of`;
`snapshot_hash`; `event` (evento strutturale o `None`); `outcome` che è
**esattamente uno** tra `Candidate` e `Abstention`; la lista ordinata dei
`FilterResult` (gate di qualità dati e filtri strategici, ciascuno con misura,
soglia, comparatore, esito, `reason_code`); le misure usate; `next_state`.

Codici di astensione iniziali (enum chiuso, estendibile solo con nuova versione):
`INSUFFICIENT_HISTORY`, `DATA_GAP`, `INVALID_DATA`, `STALE_SNAPSHOT`,
`MARKET_CLOSED`, `NO_EVENT`, `FILTER_REJECTED`, `INVALID_GEOMETRY`.

Regola: la valutazione si ferma al primo gate fallito ma registra comunque tutti
i gate e filtri valutati fino a quel punto; quelli non valutati compaiono con
esito `NOT_EVALUATED`. Così l'audit distingue "fallito" da "non raggiunto".

Il core non può: leggere orologi di sistema, generare numeri casuali senza seed
passato in config, fare I/O, chiamare LLM, mutare input. Lo stato è un valore
immutabile restituito come `next_state`.

---

## 6. Strategia baseline

**`baseline_donchian_breakout` v1** su H1 (derivato da M15).

- **Evento**: long se `close_t > max(high_{t-20..t-1})` e la barra precedente non
  era già in breakout (evento "fresco"); short simmetrico su `min(low)`. Long e
  short simultanei sono impossibili per costruzione.
- **Gate dati** (non filtri strategici): storia contigua sufficiente (20 + 15
  barre), nessun gap inatteso nella finestra, barre valide, ATR > 0, ultima barra
  chiusa = barra attesa per `as_of`.
- **Livelli**: `entry_ref = close_t` (riferimento, non prezzo eseguibile),
  `stop = entry_ref ∓ 1.5 × ATR14(H1)`, `target = entry_ref ± 2 × |entry_ref − stop|`.
- **Scadenza**: una barra H1.
- **Nessun filtro discrezionale**: niente sessione, trend, volatilità, spread.

Perché questa: è uno dei sistemi più semplici e documentati; l'evento è discreto
e ha identità naturale; non ha stato; esercita tutto il motore (finestre
multi-barra, ATR, gate di qualità, livelli, scadenza, astensioni). I parametri
(20, 14, 1.5, 2) sono scelte ingegneristiche convenzionali, **fissate prima di
vedere qualunque risultato e non ottimizzate**. Non è presentata come
profittevole e non è abilitabile al trading da configurazione (ADR-0006).

Benchmark previsti in M2, nessuno in M1: entrata casuale con le stesse regole
di stop/target e stessa frequenza; entrata sempre long; entrata sempre short.

---

## 7. Stato operativo, execution e riconciliazione (design per M3, non in M1)

Fissato ora perché influenza i contratti; implementato solo in M3 dopo approvazione.

- **Modalità**: `trading_mode ∈ {off, paper, demo, live}`, default `off`. `live`
  richiede: flag esplicito, allowlist di `(server, login)` e `trade_mode`
  confermato dal terminale. In questo progetto `live` resta non supportato fino a
  decisione esplicita successiva a M3.
- **Outbox degli intenti (SQLite WAL)**: ogni ordine nasce come `OrderIntent`
  con `client_order_id` corto (≤ 31 caratteri, va nel campo `comment` MT5) e
  `magic` per strategia. L'intento viene **persistito prima** dell'invio.
- **Macchina a stati**: `PENDING → SUBMITTING → {FILLED, PARTIAL, REJECTED,
  UNKNOWN}`; `UNKNOWN → RECONCILED_{FILLED|ABSENT}`. Un intento `UNKNOWN` blocca
  nuove entrate sul simbolo finché la riconciliazione (positions, orders, deals
  filtrati per magic e comment in una finestra temporale) non decide. Se l'ordine
  risulta assente, l'intento è chiuso come non eseguito e **non viene reinviato**:
  serve una nuova valutazione con un nuovo intento, se ancora valida.
- **Crash tra persistenza e invio**: al riavvio ogni intento `SUBMITTING` viene
  trattato come `UNKNOWN`.
- **Riconciliazione all'avvio e periodica**: il broker è la verità sulle
  posizioni. Posizione con nostro magic assente nel DB → adottata e protetta;
  posizione senza nostro magic → "estranea", non toccata ma conteggiata
  nell'esposizione; discordanza → entrate sospese con motivo strutturato.
- **Protezione**: SL/TP inviati nella richiesta stessa (broker-side). La
  protezione delle posizioni gira come loop indipendente dalla generazione di
  segnali, con heartbeat proprio; il kill switch ferma solo le entrate.
- **Cambio account o offset server incoerente**: entrate sospese.
- **Preflight**: identità account, simbolo e proprietà (digits, tick size/value,
  volume min/step/max, stops level, freeze level), tick fresco con età massima,
  mercato aperto, spread massimo, margine, geometria SL/TP con arrotondamento solo
  verso l'interno, rischio approvato dal modulo Risk.

---

## 8. Minacce e failure mode

| Failure mode | Mitigazione | Test | Milestone |
|---|---|---|---|
| Lookahead (barra futura o incompleta nello snapshot) | Filtro `available_at <= as_of`, nessuna barra parziale | Property test perturbazione del futuro; test barra in formazione | M1 |
| Doppia elaborazione della stessa candela | `evaluation_id` deterministico; sink idempotente; in M3 intento unico per `(event_id, account)` | Elaborare due volte la stessa barra → un solo record e un solo intento | M1/M3 |
| Gap M15/H1/H4 | Valutazione guidata dal calendario; HTF solo con copertura completa | Dataset sintetici con gap in punti noti | M1 |
| Timestamp incoerenti (fuori griglia, non monotoni, naive, DST) | Validatore; tipi UTC-aware; test transizioni DST USA/EU | Fixture dedicate | M1 |
| Duplicati conflittuali | Rifiuto dell'import o esclusione marcata, mai scelta arbitraria | Fixture | M1 |
| Prezzi invalidi, quote stale | Validatore; gate `INVALID_DATA`/`STALE_SNAPSHOT`; in M3 età massima tick | Fixture; broker simulato | M1/M3 |
| Weekend/barre piatte trattate come mercato | Marcatura `NO_TRADE` e calendario | Fixture weekend Dukascopy | M1 |
| Replay non riproducibile dopo restart | Checkpoint per `as_of`, scrittura idempotente | Kill a metà run + ripresa = stesso `output_hash` | M1 |
| Non determinismo tra piattaforme | Niente parallelismo nel core, ordine di riduzione fisso, hash su valori canonici arrotondati alla precisione dello strumento | CI Linux + Windows con hash attesi | M1 |
| Ordine con risposta persa | Stato `UNKNOWN`, riconciliazione, nessun reinvio | Broker simulato che perde la risposta | M3 |
| Broker disconnesso | Entrate sospese, protezione broker-side già presente | Failure injection | M3 |
| Cambio account | Binding account, blocco entrate | Failure injection | M3 |
| Crash tra persistenza e invio | `SUBMITTING` → `UNKNOWN` al riavvio | Crash simulato | M3 |
| Posizione su MT5 assente nel DB | Adozione o "estranea" con esposizione | Broker simulato | M3 |
| Broker e DB discordanti temporaneamente | Broker come verità, finestra di tolleranza, entrate sospese | Broker simulato | M3 |
| Arrotondamento SL/TP verso l'esterno | Solo verso l'interno o rifiuto | Unit test | M3 |
| LLM che altera soglie o rischio | L'AI non ha accesso in scrittura a config attive né al DB operativo | Test di architettura; review | M4 |
| Prompt injection da letteratura web | Output AI solo come proposta in schema chiuso, validata da umani | Review | M4 |
| Data snooping / overfitting | Pre-registrazione, holdout sigillato, ablation su dati non usati per scegliere | Protocollo §14 | M2 |
| Credenziali MT5 esposte | Variabili d'ambiente/keyring, mai nel repo, scansione segreti | CI | M3 |
| Supply chain | Lockfile, poche dipendenze | Review | M1 |

---

## 9. Decisioni da prendere prima di codificare

Per ognuna indico la mia raccomandazione. Approvando M0 senza commenti si
intendono accettate le raccomandazioni.

| # | Decisione | Opzioni | Raccomandazione |
|---|---|---|---|
| D1 | Simboli M1 | solo EURUSD; EURUSD+GBPUSD+USDJPY | **EURUSD, GBPUSD, USDJPY** (USDJPY esercita una precisione a 3 decimali diversa). |
| D2 | Fonte dati storici M1 | file Dukascopy importati; downloader integrato; export MT5 | **Import da file Dukascopy (BID + ASK M15) e CSV generico.** Niente downloader in M1. Dati reali non committati nel repo (licenza e dimensione); test su dati sintetici + piccola fixture. Mi serve sapere se puoi fornire i file o se preferisci che in M1 aggiunga un downloader opzionale. |
| D3 | Allineamento H4/D1 | chiusura NY 17:00; mezzanotte UTC | **Chiusura NY**, coerente con la maggior parte dei server MT5 UTC+2/+3. |
| D4 | Baseline | Donchian breakout; incrocio medie; solo benchmark casuale | **Donchian breakout 20 su H1**, parametri §6. |
| D5 | Libreria dati | pandas; polars; pyarrow + numpy | **pyarrow + numpy** nel codice applicativo, **DuckDB** per le query di audit/ricerca. Niente pandas nel core. |
| D6 | Formato audit M1 | Parquet; SQLite; JSONL | **Parquet append-only per run** + manifest JSON, interrogato con DuckDB. SQLite solo per lo stato operativo da M3. |
| D7 | Tooling | pip/venv; uv | **uv** (lockfile, Python 3.12 gestito, funziona su Windows), ruff, mypy `--strict`, pytest, hypothesis, import-linter. |
| D8 | CI | nessuna; GitHub Actions Linux; Linux + Windows | **GitHub Actions su Linux e Windows** (il determinismo cross-platform è un requisito). |
| D9 | Lingua | tutto italiano; codice inglese e documenti italiani | **Codice e identificativi in inglese, documentazione e ADR in italiano.** |
| D10 | Periodo storico (serve per M2, utile saperlo ora) | p.es. 2012–2025 | Proporre in M2 con split dev/validation/holdout prima di guardare i risultati. |

---

## 10. Compromessi tecnici

| Scelta | Vantaggio | Costo accettato |
|---|---|---|
| Modular monolith | Un processo, un deploy, test semplici, nessuna IPC da perdere | Isolamento solo logico; va difeso con test di architettura |
| Replay evento per evento con lo stesso `evaluate` (non vettorizzato) | Parità garantita con il runtime, lookahead strutturalmente difficile | Più lento di un backtest vettoriale; mitigabile con indicatori incrementali solo se misurato necessario |
| Strategie senza stato (stato derivabile dallo snapshot) | Ripresa dopo restart banale, niente stato da migrare | Finestre di lookback ricalcolate a ogni passo |
| Pydantic per i contratti, numpy per le serie | Validazione forte ai confini, velocità nei calcoli | Due rappresentazioni da tenere coerenti |
| HTF derivati da M15 | Una sola sorgente di verità, gap tracciabili | Possibili differenze rispetto alle barre H1/H4 native del broker; misurate in M3 |
| float64 per i calcoli, interi in punti all'execution | Semplicità e velocità | Hash su valori arrotondati alla precisione dello strumento per evitare differenze di ultimo bit |
| Parquet per audit e dataset, SQLite per lo stato operativo | Ognuno nel suo uso: analitico colonnare vs transazionale piccolo | Due tecnologie di storage |
| Valutazione guidata dal calendario | Gap mai silenziosi | Calendario e chiusure da mantenere e versionare |
| BID-only consentito con spread modellato | Molti più dati storici utilizzabili | Risultati marcati `MODELLED`, non confrontabili con quelli a spread osservato |
| Nessun retry su esito ignoto | Niente ordini duplicati | Qualche entrata persa; accettabile per costruzione |

---

## 11. Confini esatti di M1

### Dentro M1

1. Skeleton del progetto: `pyproject.toml` con uv, Python 3.12, ruff, mypy
   strict, pytest, hypothesis, import-linter, CI Linux + Windows.
2. `domain`: Timeframe (M15, H1, H4), Instrument (EURUSD, GBPUSD, USDJPY con
   digits e point), Bar, BarSeries, Quote (solo tipo), MarketSnapshot,
   StrategyConfig, StrategyState, FilterResult, StructuralEvent, Candidate,
   Abstention, ReasonCode, StrategyEvaluation; hashing canonico.
3. Calendario FX con DST e chiusure attese versionate.
4. `data.sources`: lettura Dukascopy CSV (BID, ASK) e CSV generico documentato.
5. `data.validate`: regole FR-D2 e report di qualità.
6. `data.store`: dataset Parquet immutabili con manifest e `dataset_id`.
7. `data.snapshot`: aggregazione M15 → H1/H4 con copertura completa; snapshot
   point-in-time.
8. `strategy`: protocollo, registry, indicatori puri ATR14 (Wilder) e canale
   Donchian, baseline v1.
9. `replay`: scheduler da calendario, loop, checkpoint e ripresa idempotente,
   manifest di run con `run_fingerprint` e `output_hash`.
10. `audit`: scrittura Parquet delle valutazioni; comando `explain` (simbolo,
    istante) che risponde con evento, gate/filtri, misure, soglie, motivo.
11. Generatore deterministico di dataset sintetici (con seed) con gap, weekend,
    DST, duplicati e prezzi invalidi in posizioni note.
12. CLI: `fxnext data import`, `fxnext data inspect`, `fxnext replay run`,
    `fxnext replay verify`, `fxnext explain`.
13. Documentazione: contratti aggiornati, ADR, esempio riproducibile.

### Fuori da M1 (esplicitamente)

Simulatore e modello costi, outcome, metriche, walk-forward, benchmark; qualsiasi
codice MT5; Risk, Execution, Position Management, SQLite operativo, kill switch;
runtime live o paper; downloader di rete; strategie oltre la baseline; filtri
discrezionali; AI; dashboard; ottimizzazione parametri; D1 e timeframe sotto M15.

Volume atteso: dell'ordine di 25–35 file di codice sorgente più test, in 4–6
incrementi verificabili, ciascuno con test verdi prima del successivo.

---

## 12. Criteri di accettazione

### M1 (verificati da test automatici, salvo dove indicato)

| # | Criterio | Come si verifica |
|---|---|---|
| A1 | Stessi input e configurazione producono gli stessi eventi e lo stesso `output_hash`. | Due run consecutivi; run su Linux e Windows in CI con hash di regressione committati. |
| A2 | Nessun uso di candele future o incomplete. | Property test: per ogni `as_of` campionato, perturbare o aggiungere barre con `available_at > as_of` non cambia la valutazione. Test esplicito della barra in formazione. |
| A3 | Gap e dati invalidi non ignorati. | Dataset sintetico con gap inattesi in posizioni note: il numero di astensioni `DATA_GAP` coincide esattamente con quello calcolato dalle posizioni dei gap e dalla lunghezza della finestra. Ogni riga invalida è conteggiata nel report di qualità con il suo motivo, e righe lette = righe accettate + righe escluse per motivo. |
| A4 | Ogni valutazione termina con candidato o astensione motivata. | Invariante del tipo (validatore Pydantic) + test su tutte le valutazioni di un run: zero casi senza esito, zero astensioni senza `reason_code`. |
| A5 | Replay riproducibile dopo restart. | Interruzione forzata a metà run + `replay run --resume` → `output_hash` identico al run ininterrotto, nessun record duplicato. |
| A6 | Audit interrogabile. | `fxnext explain EURUSD 2024-03-12T10:15Z` restituisce la valutazione con tutti i gate/filtri, misure e soglie; per un istante senza valutazione prevista (weekend) risponde con il motivo di calendario. |
| A7 | Suite automatica e regressione. | CI verde su Linux e Windows; test di architettura che falliscono se `domain`/`strategy` importano I/O; hash di regressione su dataset sintetico e sulla fixture reale. |
| A8 | Esempio riproducibile. | Un comando documentato genera il dataset sintetico, esegue il replay e stampa `run_fingerprint` e `output_hash` attesi. (Revisione manuale.) |
| A9 | Limiti dichiarati. | Sezione "limiti" nel report di M1. (Revisione manuale.) |

Il numero di test passati non è un criterio: contano i criteri sopra.

### Milestone successive (sintesi, da dettagliare all'inizio di ciascuna)

- **M2**: simulatore con assunzioni versionate e risultati marcati `MODELLED`;
  quattro rendimenti separati (segnale, controfattuale, esecuzione simulata,
  broker osservato, quest'ultimo vuoto fino a M3); parità replay/runtime con
  broker simulato su snapshot identici; walk-forward e holdout secondo protocollo
  approvato; baseline confrontata con benchmark nulli; risultati inconcludenti
  riportati come tali.
- **M3**: tutti i test di failure injection di §8 verdi contro il broker
  simulato; poi test su conto demo solo dopo tua approvazione esplicita; kill
  switch verificato; nessuna entrata reale possibile da configurazione.
- **M4**: l'AI produce proposte in schema chiuso che passano dalla stessa
  pipeline di validazione; nessun percorso di scrittura verso config attive;
  dashboard read-only funzionante con il motore spento.

---

## 13. Piano di test

| Livello | Contenuto | Da |
|---|---|---|
| Unit | Tipi, calendario (DST USA/EU, weekend, chiusure), indicatori contro valori calcolati a mano, regole di validazione, hashing canonico | M1 |
| Property-based (hypothesis) | Perturbazione del futuro; idempotenza della doppia elaborazione; invarianti di aggregazione HTF (high = max, low = min, open primo, close ultimo); ogni valutazione ha un esito | M1 |
| Golden/regressione | Hash di output su dataset sintetico e fixture reale | M1 |
| Replay e restart | Interruzione e ripresa, run concorrenti sullo stesso output rifiutati | M1 |
| Architettura | import-linter: regole di dipendenza | M1 |
| Parità | Replay vs runtime con broker simulato su snapshot identici | M2 |
| Simulatore | Casi intrabar ambigui, gap all'apertura, costi, latenza | M2 |
| Integrazione broker simulato | Risposta persa, disconnessione, cambio account, crash tra persistenza e invio, posizione orfana, discordanza temporanea, fill parziale, quote stale | M3 |
| Demo MT5 | Solo dopo approvazione, con checklist | M3 |

---

## 14. Protocollo quantitativo (bozza, da approvare all'avvio di M2)

Riportato ora perché vincola i contratti di M1 (identità, versioni, dataset).

1. **Pre-registrazione**: prima di ogni esperimento si congelano strategia,
   config, dataset_id, intervalli, modello costi e criteri. Il documento
   pre-registrato ha un hash citato nel report.
2. **Split temporale**: sviluppo, validazione, holdout finale. L'holdout si apre
   una sola volta per ipotesi; ogni apertura è registrata.
3. **Indipendenza**: eventi sovrapposti (posizioni aperte contemporaneamente o a
   distanza inferiore all'orizzonte) non contano come osservazioni indipendenti;
   si riportano sia il conteggio grezzo sia quello degli eventi non sovrapposti.
4. **Metriche**: expectancy netta in R con intervallo di confidenza da block
   bootstrap per settimana, numero di osservazioni indipendenti, frequenza,
   drawdown in R, profit factor, stabilità per anno e per coppia, copertura dati,
   sensibilità ai costi (×1, ×1.5, ×2).
5. **Confronti**: contro benchmark nulli e contro la versione senza ogni filtro
   aggiunto (ablation), su dati non usati per sceglierlo.
6. **Esiti possibili**: `REJECTED`, `INCONCLUSIVE`, `CANDIDATE_FOR_DEMO`. Le
   soglie numeriche per `CANDIDATE_FOR_DEMO` vengono proposte e approvate da te
   prima di vedere i risultati; non sono prova statistica di per sé.
7. Il numero di trade non è mai un obiettivo di ottimizzazione.

---

## 15. Limiti di questa specifica

- Nessun risultato empirico: nulla in questo documento dice che la baseline o
  qualunque strategia abbia un edge.
- Il comportamento esatto dell'offset server MT5, dei filling mode e dei
  retcode del broker che userai va verificato in M3 sul terminale reale.
- I tempi di replay (NFR-8) sono una stima da misurare in M1.
- La fixture reale dipende da D2.
