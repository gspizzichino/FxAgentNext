# FxAgent Next — M0: specifica e design

Stato: **proposta, in attesa di approvazione** · Revisione: v0.1 (2026-10-23) · Milestone: M0
Contratti: `contracts-v0.2`

Questo documento è la specifica M0. Non contiene codice applicativo. Contratti
dettagliati in [contracts.md](contracts.md), decisioni in [adr/](adr/README.md).

**Revisione v0.1 (2026-10-23).** Architettura invariata. Precisati: disponibilità
per ogni osservazione anche nelle serie colonnari e nelle barre derivate (§4.1);
scheduling con chiusura attesa, disponibilità, `as_of` nominale e ritardo
massimo (§4.2); stati di barra distinti e base di prezzo dei livelli (§4.5);
immutabilità profonda (§4.7); identità separate e hashing senza arrotondamenti
(§4.8); protocollo audit/checkpoint crash-safe (contracts §10); baseline
formalizzata con ATR a media semplice (§6). Nuovi ADR 0012–0014; ADR-0010
sostituito da 0013. Test di accettazione per ciascun punto in §12.

**Decisione D2 (2026-11-02).** Giacomo ha scelto il downloader Dukascopy in M1.
Aggiunti: FR-D7, ADR-0015, contracts §13 (`contracts-v0.2`), test T8.x. Il
download è l'unico comando che usa la rete; import e replay restano offline.

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
   (`BAR_NOT_AVAILABLE` o `DATA_GAP`). Così un buco nei dati non può mai sparire silenziosamente.
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
| FR-D1 | Importare dati OHLC storici dalla cache grezza Dukascopy (candele M1 BID e ASK) e da CSV generico. | M1 |
| FR-D7 | Scaricare dal datafeed pubblico Dukascopy le candele M1 giornaliere BID e ASK in una cache grezza immutabile, riprendibile e verificata da hash. | M1 |
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
| NFR-3 Offline | Tutta M1/M2 gira senza rete e senza MT5, su Windows, tranne `fxnext data download`. | CI senza rete (downloader testato con server HTTP locale); MT5 non è una dipendenza di installazione base. |
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
| `fxnext.data.sources` | Downloader Dukascopy verso cache grezza; parser `.bi5`; CSV generico. In M3 adapter MT5 storico. | domain, stdlib (`urllib`, `lzma`) | M1 |
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
- `AuditSink.write(evaluations)` con commit atomico per segmento; una `request_id` già scritta con contenuto diverso è un errore di integrità (contracts §10).
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

Le definizioni normative sono in [contracts.md](contracts.md) (`contracts-v0.2`);
qui la sintesi e le motivazioni.

### 4.1 Tempo e disponibilità

- Unica rappresentazione interna: datetime UTC timezone-aware (naive rifiutati);
  nanosecondi UTC interi in Parquet e negli hash.
- **Barra**: `open_time` (inclusivo), `close_time = open_time + timeframe`
  (esclusivo), `available_at >= close_time`, più `availability`:
  - `MODELLED` per lo storico: `available_at = close_time + publication_delay`
    (parametro del run, default proposto 0 s, nel fingerprint);
  - `OBSERVED` quando l'istante di ricezione è stato registrato (runtime, o CSV
    con colonna `available_at`).
- **Forma colonnare**: `BarSeries` conserva `available_at` e `availability` per
  ogni riga, come `Bar` (contracts §4).
- **Barre derivate H1/H4**: `VALID` solo con tutte le sorgenti M15 `VALID` e
  disponibili; `available_at = max` delle sorgenti; `OBSERVED` solo se tutte le
  sorgenti lo sono.
- **Snapshot** a `as_of`: contiene solo la finestra richiesta dalla strategia e
  solo barre con `available_at <= as_of`. Il costruttore di `MarketSnapshot`
  riverifica l'invariante e solleva un'eccezione se violata, così un bug del
  builder non raggiunge il core.
- **Ex post** (outcome, fill, costi, causa di un dato mancante) non è mai input
  di `evaluate`.

### 4.2 Scheduling

Per ogni slot k del timeframe di segnale: chiusura attesa `C_k` (calendario),
disponibilità della barra `A_k`, istante di valutazione `T_k = C_k +
evaluation_delay` (sempre nominale), istante reale `W_k` (solo runtime).

- Una sola richiesta di valutazione per `(strategia, config, simbolo, C_k)`,
  nessun retry.
- **Dato non ancora arrivato** (`A_k > T_k`) e **dato mancante** sono, a `T_k`,
  la stessa situazione: slot `UNAVAILABLE` → astensione `BAR_NOT_AVAILABLE` (slot
  di segnale) o `DATA_GAP` (finestra). La distinzione `LATE_ARRIVAL` / `MISSING` /
  `INVALID_ROW` è un'annotazione ex post di `explain`, fuori dall'hash.
- **Valutazione troppo tardiva** (solo runtime, `W_k − T_k >
  max_dispatch_lateness`): la valutazione è calcolata e registrata ma marcata
  `NOT_DISPATCHED_LATE`; slot persi per downtime `MISSED_DOWNTIME`. In replay
  `W_k = T_k`.
- Questo rende identici replay e runtime a parità di osservazioni registrate:
  il runtime non usa mai l'orologio reale come `as_of`.

### 4.3 Calendario FX

- Settimana di trading dalla domenica 17:00 al venerdì 17:00 America/New_York
  (DST gestito dal fuso di New York). Chiusure attese in file versionato
  (`calendar_version` negli hash). Fuori sessione non esistono slot.
- Gli slot sono consecutivi in tempo di sessione: il weekend non è un gap.

### 4.4 Timeframe e allineamento

H1 e H4 derivati da M15 con copertura completa; H4 allineato ai confini
17/21/01/05/09/13 di New York (ADR-0009).

### 4.5 Stato delle barre e base di prezzo

- Stati distinti e mai equivalenti: `VALID`, `FLAT_ZERO_VOLUME`, `INVALID`,
  `UNAVAILABLE`, `BEFORE_COVERAGE`; mercato chiuso = nessuno slot; dataset
  sintetico = provenienza a livello di dataset, mai mescolata con dati reali.
- Una barra piatta a volume zero in sessione non è né valida né ignorata: resta
  nel dataset come `FLAT_ZERO_VOLUME`, invalida la barra derivata che la contiene
  e produce l'astensione `FLAT_BAR_IN_WINDOW`. Fuori sessione è un riempitivo
  (`CLOSED_FILLER`) contato nel report di qualità.
- Le righe invalide vanno nel file `rejected` del dataset con il motivo; lo slot
  corrispondente è `INVALID`.
- Ogni serie dichiara `price_basis`. Segnale, `entry_ref`, `stop` e `target` della
  baseline sono su BID (`level_basis = BID`). I prezzi eseguibili e la traduzione
  in trigger broker appartengono al modello di esecuzione (M2) o al broker (M3),
  registrati a parte con `Provenance`; i livelli del candidato non si
  sovrascrivono. Lo spread non viene mai inventato come dato osservato.

### 4.6 Dataset

- Parquet partizionato per `(symbol, timeframe, price_basis, anno)` + file
  `rejected` + manifest JSON (sorgente, hash dei file grezzi, copertura,
  conteggi per motivo, provenienza, versioni di schema e validatore).
- `dataset_id` = content hash delle righe canoniche, indipendente dal writer
  Parquet. Immutabile: una correzione è un nuovo `dataset_id`.
- Il DB operativo del vecchio FxAgent non viene mai letto.

### 4.7 Immutabilità

Snapshot, serie, parametri e JSON congelati sono immutabili in profondità
(ADR-0012): colonne NumPy su buffer `bytes` propri (read-only non riattivabile),
`FrozenMap` al posto dei dict, `FrozenJson` per i dati liberi, test di non
mutazione basati su hash prima/dopo `evaluate`.

### 4.8 Identità

Sei identità distinte con schema `fxnext-hash-v1` (ADR-0013, contracts §9):

| Identità | Risponde a |
|---|---|
| `request_id` | quale domanda: strategia, config, simbolo, `C_k`, `as_of` |
| `event_id` | quale evento strutturale, con discriminante per più eventi sulla stessa barra e direzione |
| `candidate_id` | quale proposta per quell'evento con quella config |
| `snapshot_hash`, `evaluation_hash`, `dataset_id` | quale contenuto |
| `run_fingerprint` | quali input completi del run |
| `output_hash` | quale output, come catena hash incrementale |

Float codificati con i loro 64 bit esatti (`float.hex`), nessun arrotondamento.
Il determinismo cross-platform viene dal vincolare il calcolo (solo operazioni
IEEE correttamente arrotondate, in ordine fissato), non dal perdere precisione
nell'hash. Stesso identificativo con contenuto diverso è un errore di integrità
che interrompe il run.

---

## 5. Contratto del Strategy Engine (sintesi)

`evaluate(market_snapshot, strategy_config, strategy_state) -> StrategyEvaluation`.
L'output contiene: la `EvaluationRequest`; `snapshot_hash`; gli eventi
strutturali (con `event_id` unici); `outcome` che è **esattamente uno** tra
`Candidate` e `Abstention`; tutti i filtri dichiarati in ordine, ciascuno con
misura, soglia, comparatore, stato (`PASSED`, `FAILED`, `NOT_EVALUATED`) e
`reason_code` se fallito; le misure; `next_state`; `evaluation_hash`.

Codici di astensione `contracts-v0.1`: `BAR_NOT_AVAILABLE`,
`INSUFFICIENT_HISTORY`, `DATA_GAP`, `INVALID_DATA`, `FLAT_BAR_IN_WINDOW`,
`MARKET_CLOSED`, `NO_EVENT`, `FILTER_REJECTED` (riservato), `INVALID_GEOMETRY`.
`STALE_SNAPSHOT` è stato rimosso: la freschezza è ora espressa da
`BAR_NOT_AVAILABLE`; la freschezza delle quote sarà un controllo di Execution (M3).

Il core non può leggere orologi, usare casualità non seminata, fare I/O,
chiamare LLM o mutare input. Lo stato è un valore immutabile restituito come
`next_state`.

---

## 6. Strategia baseline

**`baseline_donchian_breakout` v1** su H1 BID derivato da M15, senza stato,
`research_only`. Definizione normativa completa in contracts §11; sintesi:

- Finestra: 22 slot in sessione `t−21 … t`, dove `t` è lo slot che chiude a `C_k`
  e gli indici contano solo slot del calendario (weekend e chiusure attese
  saltati, non sono gap).
- Canale: `U_t = max(high[t−20 … t−1])`, `L_t = min(low[t−20 … t−1])`, barra
  corrente esclusa. Canale precedente su `t−21 … t−2`.
- Evento fresco: LONG se `close_t > U_t` e `close_{t−1} <= U_{t−1}`; SHORT simmetrico
  con disuguaglianze opposte. Disuguaglianza stretta sul breakout.
- ATR14: media semplice di 14 True Range su `t−13 … t` (warm-up 15 slot), somma
  sequenziale.
- Livelli: `d = 1.5 × ATR`, stop a `d`, target a `2d` da `entry_ref = close_t`.
- Sette gate in ordine fisso (barra di segnale valida, copertura, disponibilità,
  validità, assenza di barre piatte, ATR > 0, slot di entrata in sessione), poi
  evento, poi geometria. Qualunque slot non `VALID` nella finestra produce
  astensione; nessun valore imputato. L'ultimo slot prima di una chiusura produce
  `MARKET_CLOSED`.
- Scadenza: fine dello slot successivo.

**Unica modifica di sostanza rispetto a M0 v0**: ATR14 come media semplice
invece della ricorsione di Wilder. Motivo concreto: la ricorsione di Wilder ha
memoria infinita, quindi il valore a `as_of` dipende da dove inizia il dataset e
da come si riparte dopo un gap; questo rompe la riproducibilità tra dataset con
inizio diverso e rende ambigue le regole sui gap. La media semplice ha memoria
finita di 15 slot. Approvata da Giacomo il 2026-11-03 (§9, H1).

Perché Donchian: sistema semplice e documentato, evento discreto con identità
naturale, nessuno stato, esercita tutto il motore. Parametri (20, 14, 1.5, 2)
convenzionali, **congelati prima di qualunque risultato e non ottimizzati**. Non
è presentata come profittevole e non è abilitabile al trading (ADR-0006).

Benchmark previsti in M2, nessuno in M1: entrata casuale con le stesse regole di
stop/target e stessa frequenza; sempre long; sempre short.

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
| Doppia elaborazione della stessa candela | `request_id` deterministico; stessa richiesta con contenuto diverso = errore di integrità; in M3 intento unico per `(event_id, account)` | Elaborare due volte la stessa barra → un solo record e un solo intento | M1/M3 |
| Gap M15/H1/H4 | Valutazione guidata dal calendario; HTF solo con copertura completa | Dataset sintetici con gap in punti noti | M1 |
| Timestamp incoerenti (fuori griglia, non monotoni, naive, DST) | Validatore; tipi UTC-aware; test transizioni DST USA/EU | Fixture dedicate | M1 |
| Duplicati conflittuali | Rifiuto dell'import o esclusione marcata, mai scelta arbitraria | Fixture | M1 |
| Prezzi invalidi, quote stale | Validatore e file `rejected`; gate `INVALID_DATA`; barra non arrivata → `BAR_NOT_AVAILABLE`; in M3 età massima tick in Execution | T6.3, T2.1; broker simulato | M1/M3 |
| Weekend/barre piatte trattate come mercato | Stato `FLAT_ZERO_VOLUME` in sessione, `CLOSED_FILLER` fuori sessione, calendario | T6.1, T6.2 | M1 |
| Replay non riproducibile dopo restart | Segmenti su file temporaneo + `os.replace`, manifest come unico commit, catena hash, recupero con quarantena (ADR-0014) | Crash iniettato in ogni punto + ripresa = stesso `output_hash` (T5.1) | M1 |
| Due replay sulla stessa destinazione | Lock esclusivo del sistema operativo, il secondo termina subito | T5.4 | M1 |
| Mutazione di snapshot o parametri tramite alias | Buffer `bytes` propri, `FrozenMap`, `FrozenJson` (ADR-0012) | T3.1–T3.5 | M1 |
| Non determinismo tra piattaforme | Solo operazioni IEEE correttamente arrotondate in ordine fissato, niente libm/riduzioni NumPy/parallelismo nel calcolo hashato; hash sui 64 bit esatti | CI Linux + Windows con hash attesi (T4.6) | M1 |
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
| D2 | Fonte dati storici M1 | file Dukascopy importati; downloader integrato; export MT5 | **Deciso da Giacomo il 2026-11-02: downloader integrato** (ADR-0015). Dati reali non committati nel repo; test su dati sintetici e server HTTP locale. |
| D3 | Allineamento H4/D1 | chiusura NY 17:00; mezzanotte UTC | **Chiusura NY**, coerente con la maggior parte dei server MT5 UTC+2/+3. |
| D4 | Baseline | Donchian breakout; incrocio medie; solo benchmark casuale | **Donchian breakout 20 su H1**, parametri §6. |
| D5 | Libreria dati | pandas; polars; pyarrow + numpy | **pyarrow + numpy** nel codice applicativo, **DuckDB** per le query di audit/ricerca. Niente pandas nel core. |
| D6 | Formato audit M1 | Parquet; SQLite; JSONL | **Parquet append-only per run** + manifest JSON, interrogato con DuckDB. SQLite solo per lo stato operativo da M3. |
| D7 | Tooling | pip/venv; uv | **uv** (lockfile, Python 3.12 gestito, funziona su Windows), ruff, mypy `--strict`, pytest, hypothesis, import-linter. |
| D8 | CI | nessuna; GitHub Actions Linux; Linux + Windows | **GitHub Actions su Linux e Windows** (il determinismo cross-platform è un requisito). |
| D9 | Lingua | tutto italiano; codice inglese e documenti italiani | **Codice e identificativi in inglese, documentazione e ADR in italiano.** |
| D10 | Periodo storico (serve per M2, utile saperlo ora) | p.es. 2012–2025 | **Decisa 2026-11-03: dal 1 gennaio 2015 a oggi**, EURUSD, GBPUSD, USDJPY, BID+ASK. Lo split dev/validation/holdout si fissa nella specifica M2 prima di guardare i risultati. |

Decisioni aggiunte dalla revisione v0.1 (H1 e H5 approvate il 2026-11-03; H2, H3, H4 ancora aperte):

| # | Decisione | Opzioni | Raccomandazione |
|---|---|---|---|
| H1 | Definizione ATR14 della baseline | media semplice di 14 TR; Wilder su finestra fissa (seed SMA + N barre di ricorsione); Wilder su tutta la storia | **Approvata 2026-11-03: media semplice.** Memoria finita di 15 slot, nessun parametro di warm-up arbitrario. Wilder su tutta la storia è escluso perché dipende dall'inizio del dataset. |
| H2 | Barra M15 piatta a volume zero in sessione | invalida la barra derivata e la finestra; trattata come valida; esclusa dagli indicatori | **Approvata 2026-11-03: invalida** (astensione `FLAT_BAR_IN_WINDOW`), conservativo e visibile; rivedibile con dati sulla frequenza reale per simbolo. |
| H3 | Valori di default in replay | `publication_delay = 0 s`, `evaluation_delay = 0 s`; valori positivi | **Approvata 2026-11-03: 0 s e 0 s** in M1; in M3 si fissa il valore runtime di `evaluation_delay` e lo si usa anche nei test di parità. |
| H4 | Candidato sull'ultimo slot prima di una chiusura | `MARKET_CLOSED`; candidato valido alla riapertura | **Approvata 2026-11-03: `MARKET_CLOSED`**: nessun candidato attraversa weekend o festività. |
| H5 | Ripresa con codice diverso | `code_version` nel fingerprint (ripresa solo con lo stesso commit); escluso | **Approvata 2026-11-03: incluso.** Un run ripreso con codice diverso sarebbe un run diverso. |

---

## 10. Compromessi tecnici

| Scelta | Vantaggio | Costo accettato |
|---|---|---|
| Modular monolith | Un processo, un deploy, test semplici, nessuna IPC da perdere | Isolamento solo logico; va difeso con test di architettura |
| Replay evento per evento con lo stesso `evaluate` (non vettorizzato) | Parità garantita con il runtime, lookahead strutturalmente difficile | Più lento di un backtest vettoriale; mitigabile con indicatori incrementali solo se misurato necessario |
| Strategie senza stato (stato derivabile dallo snapshot) | Ripresa dopo restart banale, niente stato da migrare | Finestre di lookback ricalcolate a ogni passo |
| Pydantic per i contratti, numpy per le serie | Validazione forte ai confini, velocità nei calcoli | Due rappresentazioni da tenere coerenti |
| HTF derivati da M15 | Una sola sorgente di verità, gap tracciabili | Possibili differenze rispetto alle barre H1/H4 native del broker; misurate in M3 |
| float64 per i calcoli, interi in punti all'execution; hash sui bit esatti | Nessuna perdita di informazione negli hash | Il calcolo hashato è vincolato a operazioni IEEE deterministiche in ordine fissato (contracts §9.4) |
| ATR a media semplice invece di Wilder | Memoria finita, indipendenza dall'inizio del dataset, regole sui gap semplici | Si discosta dalla definizione "classica" di Wilder |
| `as_of` nominale anche in runtime | Replay e runtime identici a parità di osservazioni | Una barra arrivata pochi secondi dopo `T_k` produce un'astensione invece di un segnale |
| Barra piatta in sessione invalida la finestra | Nessun dato dubbio entra negli indicatori | Possibili astensioni in più su simboli poco liquidi |
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
4. `data.sources`: downloader Dukascopy (candele M1 giornaliere BID e ASK, cache
   grezza con log e hash, ripresa, retry solo per errori transitori), parser
   `.bi5`, aggregazione M1 → M15, CSV generico documentato (ADR-0015).
5. `data.validate`: regole FR-D2 e report di qualità.
6. `data.store`: dataset Parquet immutabili con manifest e `dataset_id`.
7. `data.snapshot`: aggregazione M15 → H1/H4 con copertura completa; snapshot
   point-in-time.
8. `strategy`: protocollo, registry, indicatori puri (ATR14 a media semplice,
   canale Donchian), baseline v1 come in contracts §11.
9. `replay`: scheduler da calendario (contracts §7), loop, catena `output_hash`,
   protocollo di checkpoint e ripresa crash-safe con lock (contracts §10),
   comando `verify`.
10. `audit`: segmenti Parquet + manifest; comando `explain` (simbolo, istante)
    che risponde con richiesta, gate/filtri, misure, soglie, esito e annotazione
    ex post marcata come tale.
11. Generatore deterministico di dataset sintetici (con seed) con gap, weekend,
    DST, duplicati e prezzi invalidi in posizioni note.
12. CLI: `fxnext data download`, `fxnext data import`, `fxnext data inspect`, `fxnext replay run`,
    `fxnext replay verify`, `fxnext explain`.
13. Documentazione: contratti aggiornati, ADR, esempio riproducibile.

La revisione v0.1 non aggiunge elementi a questa lista: precisa come vanno
realizzati i punti 2, 7, 8, 9 e 10. Lo scheduler di M1 include la sola funzione
pura di classificazione `dispatch` (T2.4); il runtime che la usa resta in M3.

### Fuori da M1 (esplicitamente)

Simulatore e modello costi, outcome, metriche, walk-forward, benchmark; qualsiasi
codice MT5; Risk, Execution, Position Management, SQLite operativo, kill switch;
runtime live o paper; download di tick o di fonti diverse da Dukascopy; strategie oltre la baseline; filtri
discrezionali; AI; dashboard; ottimizzazione parametri; D1 e timeframe sotto M15.

Volume atteso: dell'ordine di 25–35 file di codice sorgente più test, in 4–6
incrementi verificabili, ciascuno con test verdi prima del successivo.

---

## 12. Criteri di accettazione

### M1 (verificati da test automatici, salvo dove indicato)

| # | Criterio | Come si verifica |
|---|---|---|
| A1 | Stessi input e configurazione producono gli stessi eventi e lo stesso `output_hash`. | Due run consecutivi; run su Linux e Windows in CI con hash di regressione committati (T4.6). |
| A2 | Nessun uso di candele future o incomplete. | T1.1–T1.6, T2.1. |
| A3 | Gap e dati invalidi non ignorati. | Dataset sintetico con gap inattesi in posizioni note: il numero di astensioni `DATA_GAP`/`BAR_NOT_AVAILABLE` coincide esattamente con quello calcolato dalle posizioni dei gap e dalla lunghezza della finestra (22 slot). Righe lette = righe accettate + righe `rejected` per motivo + riempitivi `CLOSED_FILLER`. T6.1–T6.5. |
| A4 | Ogni valutazione termina con candidato o astensione motivata. | Invariante del tipo + controllo su tutte le valutazioni di un run: zero casi senza esito, ogni `Abstention` ha `reason_code` uguale a quello del primo filtro `FAILED` (o `NO_EVENT`/`INVALID_GEOMETRY`), filtri dopo il primo fallito tutti `NOT_EVALUATED`. |
| A5 | Replay riproducibile dopo restart. | T5.1–T5.6. |
| A6 | Audit interrogabile. | `fxnext explain EURUSD 2024-03-12T10:15Z` restituisce richiesta, gate/filtri con misure e soglie, esito; per uno slot `UNAVAILABLE` aggiunge l'annotazione ex post marcata come tale; per un istante fuori sessione risponde con il motivo di calendario. |
| A7 | Suite automatica e regressione. | CI verde su Linux e Windows; test di architettura (import-linter e tipi dei campi dei Model); hash di regressione. |
| A8 | Esempio riproducibile. | Un comando documentato genera il dataset sintetico, esegue il replay e stampa `run_fingerprint` e `output_hash` attesi. (Revisione manuale.) |
| A9 | Limiti dichiarati. | Sezione "limiti" nel report di M1. (Revisione manuale.) |

Il numero di test passati non è un criterio: contano i criteri sopra.

### Test di accettazione per i punti della revisione v0.1

Ogni test è deterministico e ha un esito atteso esatto.

**1. Disponibilità temporale nelle serie**

- T1.1 Ogni `BarSeries` prodotta dal builder ha `available_at_ns` e `availability`
  per ogni riga con prezzi; righe con prezzi e `available_at < close_time` →
  `SeriesInvariantError`.
- T1.2 Barra H1 derivata da quattro M15 con `available_at` 10:15:05, 10:30:02,
  10:45:09, 11:00:03 (OBSERVED) → `available_at = 11:00:03`, `OBSERVED`. Con una
  sola sorgente MODELLED → `MODELLED`.
- T1.3 Una sorgente M15 con `available_at > as_of` → slot H1 `UNAVAILABLE` a quell'`as_of`
  e `VALID` a un `as_of` successivo.
- T1.4 Costruire a mano un `MarketSnapshot` con una barra `available_at > as_of`
  → `SnapshotInvariantError` (il core non la riceve mai).
- T1.5 Property test (hypothesis): per `as_of` casuali, modificare prezzi,
  aggiungere o togliere barre con `available_at > as_of` lascia invariati
  `snapshot_hash` ed `evaluation_hash` a `as_of`.
- T1.6 Una barra con `close_time > as_of` non compare mai nello snapshot, anche se
  ha un `available_at` (errato) precedente: il dataset la rifiuta in import.

**2. Scheduling**

- T2.1 Dataset OBSERVED in cui la barra di segnale arriva a `T_k + 1 s` →
  astensione `BAR_NOT_AVAILABLE`; `explain` riporta ex post `LATE_ARRIVAL`.
  Lo stesso slot senza barra → stessa astensione, ex post `MISSING`. Serie,
  esito e filtri sono identici; gli `evaluation_hash` differiscono solo perché
  lo snapshot cita `sources` (dataset_id diversi). L'informazione ex post non
  entra mai nell'hash. *(Corretto in M1: la v0.1 diceva "hash identici".)*
- T2.2 Esattamente una `EvaluationRequest` per ogni `C_k` in sessione
  nell'intervallo; nessuna fuori sessione; numero atteso calcolato dal calendario
  per una settimana con cambio DST USA e una con cambio DST solo europeo.
- T2.3 `as_of == C_k + evaluation_delay` per ogni richiesta; cambiare
  `evaluation_delay` cambia `run_fingerprint` e `request_id`.
- T2.4 (contratto per M3, testato in M1 sul solo scheduler puro) dati `T_k` e
  `W_k`, `dispatch` vale `NOT_DISPATCHED_LATE` sse `W_k − T_k > max_dispatch_lateness`;
  `evaluation_hash` è identico nei due casi.

**3. Immutabilità**

- T3.1 Su ogni colonna di `BarSeries`: assegnazione di elemento, `setflags(write=True)`
  e scrittura su una vista → eccezione.
- T3.2 Modificare l'array NumPy passato al costruttore dopo la costruzione non
  cambia la serie né il suo hash.
- T3.3 `FrozenMap` e `FrozenJson`: `__setitem__`, `update`, `append` su liste
  annidate impossibili; un `dict` o `list` in un campo `Model` → errore di validazione.
- T3.4 Test di architettura: nessun campo di `Model` con tipo `list`, `dict`,
  `set`, `bytearray`, `np.ndarray`.
- T3.5 Per ogni valutazione del run di regressione, `content_hash` di snapshot,
  config e stato è uguale prima e dopo `evaluate`.

**4. Identità e hashing**

- T4.1 Vettori di test dello schema `fxnext-hash-v1` (oggetto → byte canonici →
  hash) committati e verificati: ordine delle chiavi, `None` esplicito, frozenset
  ordinato, `-0.0 == 0.0`, NaN rifiutato, datetime in ns.
- T4.2 Due float che differiscono di 1 ULP producono hash diversi.
- T4.3 Cambiare `stop_atr_mult` cambia `config_hash` e `candidate_id` ma non
  `event_id`; cambiare `channel_slots` cambia anche `event_id`.
- T4.4 Due eventi con lo stesso `event_id` nella stessa valutazione →
  `DuplicateEventError`; due eventi sulla stessa barra e direzione con
  discriminanti diversi → `event_id` diversi.
- T4.5 Store di audit con stessa `request_id` e `evaluation_hash` diverso nello
  stesso run → `IntegrityError`.
- T4.6 `output_hash` del run di regressione identico su Linux e Windows.
- T4.7 Test di architettura: il core non chiama `math.exp/log/sqrt/pow`, `np.sum`,
  `np.mean` su grandezze hashate (lint mirato sul package `strategy`).

**5. Audit e checkpoint**

- T5.1 Iniezione di crash (eccezione a un punto di iniezione e, in un secondo test,
  `kill` del sottoprocesso) in ciascun punto: durante la scrittura del `.tmp`,
  dopo il suo `fsync`, dopo `os.replace` del segmento, dopo la scrittura di
  `manifest.json.tmp`, dopo il commit del manifest. Per ciascuno, `--resume` →
  `output_hash` identico al run ininterrotto.
- T5.2 Dopo ogni ripresa: `request_id` unici, `seq` contigui, orfani spostati in
  `quarantine/`, `verify` verde.
- T5.3 Un bit modificato in un segmento committato → la ripresa e `verify`
  falliscono con `IntegrityError`, nessuna scrittura.
- T5.4 Due processi di replay sulla stessa destinazione → il secondo termina con
  `DestinationLocked` entro 1 s e non crea file.
- T5.5 Il lock è rilasciato alla morte del processo: dopo `kill` del primo, una
  ripresa acquisisce il lock senza intervento manuale.
- T5.6 `explain` durante un run in corso legge solo i segmenti elencati nel
  manifest e mai i `.tmp`.

**6. Qualità delle barre e prezzi**

- T6.1 Barra piatta a volume zero in sessione → riga `FLAT_ZERO_VOLUME` nel
  dataset, slot H1 che la contiene non `VALID`, astensione `FLAT_BAR_IN_WINDOW`
  per le 22 valutazioni la cui finestra la include.
- T6.2 Barra piatta fuori sessione → conteggiata `CLOSED_FILLER`, assente dalle
  serie, nessuna astensione.
- T6.3 Riga con `high < close` → nel file `rejected` con motivo, slot `INVALID`,
  astensione `INVALID_DATA`.
- T6.4 Duplicato identico → deduplicato e contato; duplicato conflittuale →
  entrambe in `rejected` con `CONFLICTING_DUPLICATE`, slot `INVALID`.
- T6.5 Un run con dataset `SYNTHETIC` e uno `REAL` insieme → rifiutato prima di
  iniziare.
- T6.6 Ogni `Candidate` della baseline ha `level_basis = BID` e `entry_ref`
  uguale al `close` BID dello slot di segnale.

**7. Baseline Donchian**

- T7.1 Vettori calcolati a mano (fixture di 30 slot): `U_t`, `L_t`, `U_{t−1}`,
  `L_{t−1}`, `ATR_t`, `stop`, `target` confrontati a livello di bit.
- T7.2 `close_t == U_t` → `NO_EVENT`; `close_t > U_t` con `close_{t−1} > U_{t−1}`
  (breakout già in corso) → `NO_EVENT`.
- T7.3 Il massimo della barra `t` superiore a tutto il canale non cambia `U_t`.
- T7.4 Finestra che attraversa il weekend senza buchi → nessun `DATA_GAP`;
  stessa finestra con uno slot del giovedì mancante → `DATA_GAP` per le
  valutazioni che lo includono.
- T7.5 Prime 21 valutazioni dopo `coverage_start` → `INSUFFICIENT_HISTORY`; la
  22ª passa il gate.
- T7.6 Slot venerdì 16:00–17:00 NY e slot prima di una chiusura attesa →
  `MARKET_CLOSED`.
- T7.7 Ordine dei gate: una finestra con slot `UNAVAILABLE` e `INVALID` produce
  `DATA_GAP` (gate 3 prima del 4), con gate 4–7 `NOT_EVALUATED`.
- T7.8 Valutazione dello stesso `as_of` su dataset che iniziano in date diverse
  (entrambi con ≥ 22 slot prima) → `evaluation_hash` identico (memoria finita).

**8. Downloader Dukascopy (D2)**

- T8.1 Server HTTP locale (stdlib) che serve file `.bi5` generati dai test con
  contenuto noto: download di 3 giorni BID e ASK → cache con 6 file, log con URL,
  stato, byte e `sha256`; import → M15 identiche ai valori attesi.
- T8.2 URL costruiti con mese a base 0 e giorno UTC: 2024-01-31 →
  `.../2024/00/31/BID_candles_min_1.bi5`; prezzi di USDJPY divisi per 10^3, EURUSD per 10^5.
- T8.3 Risposta 200 con corpo vuoto → giorno `EMPTY` registrato, nessun errore;
  404 → `NOT_FOUND` registrato; nessuno dei due crea barre.
- T8.4 500 due volte poi 200 → file scaricato dopo esattamente 3 tentativi con
  backoff; 500 oltre il limite → giorno `FAILED`, comando esce con codice ≠ 0, il
  resto della cache resta valido.
- T8.5 Corpo troncato o LZMA corrotto → `CORRUPT`, nessun file nella cache, nessun
  `.tmp` rimasto.
- T8.6 Interruzione a metà download e nuovo avvio → solo i giorni mancanti
  vengono richiesti (contatore richieste del server di test).
- T8.7 Un riscaricamento che produce byte diversi da quelli in cache → il file
  esistente non viene sovrascritto, nuova versione conservata accanto, evento
  `SOURCE_CHANGED` nel log; `dataset_id` di un import già fatto non cambia.
- T8.8 Giorni non ancora interamente trascorsi (fine giorno UTC + margine > ora
  corrente) non vengono richiesti.
- T8.9 Minuto mancante nel file del giorno → M15 corrispondente `INVALID`
  (`INCOMPLETE_SOURCE`); 15 minuti piatti a volume zero → `FLAT_ZERO_VOLUME`;
  `ask_open < bid_open` o `ask_close < bid_close` → entrambe le M15 `INVALID` (`NEGATIVE_SPREAD`).
- T8.10 Due import della stessa cache, anche su Windows e Linux → stesso `dataset_id`.


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
| Replay e restart | Crash iniettato in ciascun punto del protocollo (eccezione e kill del processo), ripresa, corruzione di segmento, lock concorrente (T5.x) | M1 |
| Immutabilità e hashing | Tentativi di mutazione, alias NumPy, vettori di test dello schema hash, 1 ULP, collisioni simulate (T3.x, T4.x) | M1 |
| Architettura | import-linter: regole di dipendenza; tipi dei campi dei Model; funzioni vietate nel core | M1 |
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
- Formato `.bi5` delle candele (ordine dei campi, divisore dei prezzi, presenza dei
  minuti senza tick) da confermare su un file reale prima di congelare il parser;
  l'ambiente cloud attuale non raggiunge `datafeed.dukascopy.com`.
- Revisioni di barre già osservate (dati bitemporali) non sono supportate in
  `contracts-v0.1`: un secondo valore per lo stesso slot è un conflitto. Andrà
  rivisto in M3 se il broker corregge barre già pubblicate.
- Il `fsync` della directory non esiste su Windows: un crash di sistema operativo
  (non di processo) subito dopo `os.replace` potrebbe perdere l'ultimo commit,
  che verrebbe ricalcolato alla ripresa senza effetti sull'`output_hash`.
- I confini di segmento mensili sono una scelta di comodità; non influenzano gli hash.
