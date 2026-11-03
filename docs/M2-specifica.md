# FxAgent Next — M2: specifica

Stato: **approvata 2026-11-03**, decisioni E1–E8 (§10) come proposte.
Riferimenti: [M0-specifica.md](M0-specifica.md)
§2 (FR-R2, FR-R3), §6, §13, §14; [contracts.md](contracts.md) §11–§12.

## 0. Sintesi

M2 aggiunge il modulo `fxnext.research`, che prende i candidati prodotti dal
replay di M1 e calcola quello che sarebbe successo dopo, con assunzioni
dichiarate e versionate. Il lavoro si divide in cinque parti:

1. Un **simulatore di esecuzione** che segue i prezzi minuto per minuto. Usa
   BID e ASK osservati, costi espliciti e regole conservative quando l'ordine
   degli eventi dentro un minuto è ambiguo.
2. **Quattro rendimenti separati**: segnale, controfattuale, esecuzione
   simulata e broker osservato. L'ultimo resta vuoto fino a M3.
3. **Metriche in R, benchmark nulli e sensibilità**, secondo il protocollo di
   M0 §14.
4. **Split temporale e holdout sigillato**, fissati in questo documento prima
   di vedere qualunque risultato.
5. Un **test di parità**: un ciclo "come in runtime", con barre che arrivano una
   alla volta, deve produrre valutazioni identiche al replay.

Nulla in M2 è abilitabile al trading. Ogni risultato del simulatore è marcato
`MODELLED`.

Nota di trasparenza: per verificare il parser ho guardato una settimana reale,
EURUSD dal 14 al 20 gennaio 2024 (§8, holdout proposto). Ho guardato prezzi,
spread e numero di valutazioni del replay. Non ho calcolato esiti, né contato
o guardato candidati.

## 1. Confini di M2

### Dentro

- Dataset M1 BID e ASK, importato dalla stessa cache Dukascopy, usato **solo**
  dal simulatore per seguire il percorso dei prezzi (E1). La strategia continua
  a vedere solo M15, H1 e H4.
- `ExecutionModel` v1, `CostModel` v1 e la politica delle posizioni.
- `SimulatedOutcome` per ogni candidato, persistito accanto all'audit del run,
  con lo stesso schema crash-safe di ADR-0014.
- Metriche, benchmark nulli, sensibilità ai costi e ai parametri (solo
  informativa), stabilità per anno e per coppia.
- Pre-registrazione degli esperimenti, split temporale e registro delle aperture
  dell'holdout.
- Report riproducibile in JSON e Markdown, con hash di tutti gli input.
- Test di parità replay/"runtime simulato".

### Fuori

- Risk, sizing in lotti, esposizione per valuta, MT5, broker reale (M3).
- Ottimizzazione dei parametri della baseline, che restano congelati (M0 §6).
- Swap e rollover (limite dichiarato, §11).
- Dashboard e AI (M4).

## 2. Dati usati dal simulatore

- **Percorso**: minuti M1 BID e ASK nell'intervallo `[entry, exit]`. Ogni minuto
  ha lo stesso stato di M1 (`VALID`, `FLAT_ZERO_VOLUME`, `INVALID`,
  `UNAVAILABLE`) e la propria disponibilità.
- Lo spread è sempre quello osservato al minuto, `ask - bid`. Se l'ASK di un
  minuto manca, il simulatore non lo ricostruisce: quel minuto non è eseguibile.
- I minuti fuori sessione (weekend) non fanno parte del percorso. Alla
  riapertura il primo minuto può aprire lontano dall'ultimo prezzo: è un gap, e
  viene trattato come in §3.4.
- Il simulatore legge il futuro rispetto ad `as_of` per costruzione. Per questo
  vive fuori dal core: `research` può importare `strategy` e `domain`, mai il
  contrario. Il controllo è un nuovo contratto di import-linter.

## 3. Modello di esecuzione `exec-model-v1`

### 3.1 Entrata

- Ordine a mercato al primo minuto del `next_slot`, cioè quello che apre ad
  `as_of`. Con `evaluation_delay = 0` (H3) coincide con l'apertura dello slot di
  entrata.
- **LONG** compra all'`ask_open` di quel minuto, **SHORT** vende al `bid_open`.
- A questo si aggiunge lo slippage di entrata (§4), sempre a sfavore.
- Latenza configurabile in minuti interi, default 0: con latenza `k` l'entrata
  avviene all'apertura del minuto `k`.
- Se il minuto di entrata non è eseguibile (BID o ASK non `VALID`), si prova il
  minuto successivo, fino a `expires_at`. Se si arriva a `expires_at` senza
  entrata, l'esito è `NOT_FILLED_NO_QUOTE`, senza trade.

### 3.2 Livelli di uscita

- `stop` e `target` sono quelli del candidato, su base BID, e non vengono mai
  modificati (contracts §12).
- I trigger seguono la semantica MT5:
  - un **LONG** esce sul **BID**: stop se `bid_low <= stop`, target se
    `bid_high >= target`;
  - uno **SHORT** esce sull'**ASK**: stop se `ask_high >= stop`, target se
    `ask_low <= target`.
- Nella pratica uno SHORT paga lo spread anche in uscita. È voluto ed è
  esattamente ciò che succede su MT5.

### 3.3 Ambiguità dentro il minuto

- Se nello stesso minuto vengono toccati sia lo stop sia il target, si assume
  **lo stop per primo** (conservativo, E3).
- L'esito è marcato `ambiguous = true`. Il report mostra quanti esiti sono
  ambigui e quanto cambierebbe l'expectancy assumendo il target per primo.
- Il minuto di entrata conta: se dopo il fill lo stesso minuto tocca lo stop,
  il trade si chiude in quel minuto. Anche in questo caso, se sono toccati
  entrambi i livelli, l'esito è ambiguo con stop per primo.

### 3.4 Gap

Il gap riguarda il primo minuto dopo un buco del percorso o dopo il weekend.

- Se quel minuto apre già oltre lo stop, l'uscita avviene all'apertura del
  minuto, quindi a un prezzo peggiore dello stop. Esito `STOP_GAP`.
- Se apre già oltre il target, l'uscita avviene **al livello del target** e non
  all'apertura più favorevole (conservativo, E3). Esito `TARGET`.

### 3.5 Uscita a tempo e weekend (E2)

- La posizione può restare aperta durante il weekend: è il comportamento
  naturale di un breakout H1, e il gap del lunedì viene simulato.
- Uscita a tempo dopo **120 slot H1 in sessione** (5 giorni di trading) dalla
  entrata, al `bid_close` (LONG) o all'`ask_close` (SHORT) dell'ultimo minuto.
  Esito `TIME`. Serve a limitare l'orizzonte, e quindi la sovrapposizione degli
  eventi (M0 §14.3). È un parametro del modello di esecuzione, non della
  strategia, ed è congelato qui.

### 3.6 Buchi nei dati durante la posizione

- Se dei minuti in sessione mancano, il percorso riprende dal primo minuto
  disponibile, con la regola del gap (§3.4).
- Se il buco supera 60 minuti in sessione, l'esito è comunque calcolato ma
  marcato `path_gap = true`. Il report lo mostra a parte, così si vede se i
  buchi spostano il risultato.

### 3.7 Politica delle posizioni (E4)

- Al massimo una posizione per simbolo.
- Un candidato che arriva mentre una posizione è aperta non viene eseguito, ma
  il suo esito viene calcolato lo stesso e finisce nel rendimento
  **controfattuale** (§5).
- Nessuna posizione opposta e nessun cambio di direzione: il candidato opposto
  è controfattuale anche lui.

## 4. Modello dei costi `cost-model-v1`

I costi si esprimono in pip per simbolo (pip = 0,0001, oppure 0,01 per JPY),
in un file versionato con hash nel fingerprint del run.

| Voce | Default | Dove si applica |
|---|---|---|
| Spread | osservato (ASK − BID), mai inventato | implicito in §3.1 e §3.2 |
| Commissione | 0,35 pip per lato (circa 7 USD a lotto andata e ritorno su EURUSD) | entrata e uscita |
| Slippage entrata | 0,1 pip | sempre a sfavore |
| Slippage stop | 0,2 pip | uscite `STOP` e `TIME`, non `STOP_GAP` (già all'apertura) |
| Slippage target | 0 | gli ordini limite non scivolano |

- Sensibilità obbligatoria: commissione e slippage moltiplicati per ×1, ×1,5 e
  ×2 (M0 §14.4). Lo spread osservato resta invariato, perché è un dato.
- I default sono una proposta (E5). Andranno confrontati con le condizioni
  reali del broker in M3.

## 5. Quattro rendimenti

L'unità di rischio di ogni candidato è `R = d = 1,5 × ATR14`, cioè la distanza
`|entry_ref − stop|` sul BID alla decisione. È la stessa per tutti i rendimenti,
quindi la differenza tra due rendimenti misura direttamente l'effetto di
esecuzione e costi.

| Rendimento | Definizione | Costi |
|---|---|---|
| `signal` | Entrata a `entry_ref` (BID), uscita a stop o target su BID con le stesse regole di §3.3–§3.6, senza spread. Misura l'idea, non il trade. | nessuno |
| `counterfactual` | Come `exec`, ma per i candidati non eseguiti per la politica delle posizioni (§3.7). | come `exec` |
| `exec` | Esecuzione simulata `exec-model-v1` con `cost-model-v1`. | tutti |
| `broker` | Fill reali MT5 riconciliati. | vuoto fino a M3 |

Ogni `SimulatedOutcome` contiene i seguenti campi:

- `candidate_id`, `stream`, `exec_model_version` e `cost_model_hash`;
- `entry_time` ed `entry_price`, `exit_time` ed `exit_price`, `exit_reason`
  (uno tra `STOP`, `STOP_GAP`, `TARGET`, `TIME`, `NOT_FILLED_NO_QUOTE`);
- `r_gross` e `r_net`, con il dettaglio dei costi in pip e in R;
- `ambiguous` e `path_gap`;
- `holding_slots`, `weekend_crossings` e `mae_r`/`mfe_r` (escursione massima
  sfavorevole e favorevole);
- `provenance = MODELLED`.

L'identità di ogni outcome è `outcome_id = H(candidate_id, stream,
exec_model_version, cost_model_hash, path_dataset_ids)`, con lo schema hash
`fxnext-hash-v1`.

## 6. Benchmark nulli

Tutti i benchmark usano le stesse regole di stop, target e tempo della baseline,
la stessa `d` (ATR al momento) e lo stesso modello di esecuzione e costi.

| Benchmark | Definizione | Cosa verifica |
|---|---|---|
| `random_direction` | Agli stessi istanti dei candidati della baseline, direzione casuale; 1.000 ripetizioni con seed fissati. | Se la direzione del breakout aggiunge qualcosa. |
| `random_time` | Stesso numero di entrate per mese, a slot H1 in sessione scelti a caso, direzione casuale; 1.000 ripetizioni. | Se il momento dell'entrata aggiunge qualcosa. |
| `always_long` / `always_short` | Agli stessi istanti dei candidati, sempre LONG o sempre SHORT. | Deriva del periodo. |

Il risultato della baseline si riporta come percentile della distribuzione di
ciascun benchmark casuale.

## 7. Metriche

Le metriche sono quelle di M0 §14.4. Ognuna si calcola per coppia, per anno e
in aggregato, e per ogni split.

- **Expectancy netta in R**, con intervallo di confidenza al 95% da block
  bootstrap per settimana (10.000 ricampionamenti, seed fissato).
- **Numero di trade**, grezzo e indipendente. Un trade è indipendente se non si
  sovrappone a un altro trade sulla stessa coppia; tra coppie diverse vale lo
  stesso controllo, riportato a parte perché le coppie sono correlate tramite
  il dollaro.
- **Frequenza** (trade per mese), percentuale di vincite, **profit factor**,
  **drawdown massimo in R** e durata del drawdown.
- **Copertura dei dati**: valutazioni e astensioni per codice, candidati
  esclusi per `path_gap`, quota di esiti ambigui.
- **Sensibilità**: costi ×1, ×1,5 e ×2; ambiguità risolta con target per primo;
  griglia informativa dei parametri della baseline (canale 10/20/40, stop 1,0,
  1,5 e 2,0 ATR, target 1,5, 2 e 3 R). La griglia gira **solo sullo split di
  sviluppo**, è riportata ma non serve a scegliere nulla.

## 8. Split temporale e protocollo

Dati: dall'1 gennaio 2015 al 31 ottobre 2026 (D10).

| Split | Intervallo | Uso |
|---|---|---|
| Sviluppo | 2015-01-01 → 2020-12-31 (6 anni) | Debug del simulatore, sensibilità, griglia informativa |
| Validazione | 2021-01-01 → 2023-12-31 (3 anni) | Valutazione della baseline congelata e dei benchmark |
| Holdout | 2024-01-01 → 2026-10-31 (circa 2,8 anni) | Aperto una sola volta per ipotesi, dopo l'approvazione |

- **Warm-up**: le prime 22 valutazioni H1 di ogni split usano dati dello split
  precedente. Questa è solo storia necessaria agli indicatori, non
  contaminazione: non vengono letti esiti dello split precedente.
- **Walk-forward**: con parametri congelati non c'è niente da stimare, quindi il
  walk-forward diventa una valutazione per finestre annuali scorrevoli. La
  macchina "stima su train, valuta su test" si costruisce e si testa, ma per la
  baseline la stima è l'identità. Servirà dalle proposte di M4.
- **Pre-registrazione**: ogni esperimento è un file TOML congelato con
  strategia, config, `dataset_id`, split, modello di esecuzione e costi, e i
  criteri di §9. Il suo hash entra nel fingerprint del run e nel report.
- **Holdout sigillato**: il runner rifiuta un intervallo che tocca l'holdout se
  manca una pre-registrazione con `holdout: true`. Ogni apertura viene
  registrata in `research/holdout_log.jsonl`, append-only. Una seconda apertura
  per la stessa ipotesi richiede `--reopen-holdout` e viene registrata come
  tale.
- **Ordine dei simboli**: EURUSD per primo, appena il download è completo.
  GBPUSD e USDJPY si aggiungono con lo stesso protocollo, senza cambiare nulla
  dopo aver visto EURUSD.

## 9. Criteri di esito (da approvare ora, prima dei risultati) (E6)

Gli esiti possibili sono `REJECTED`, `INCONCLUSIVE` e `CANDIDATE_FOR_DEMO`.

**`CANDIDATE_FOR_DEMO`** richiede tutte queste condizioni:

1. Sulla validazione, con costi ×1:
   - expectancy netta > 0 e limite inferiore dell'intervallo al 95% > 0;
   - almeno 150 trade indipendenti, sommando le coppie disponibili.
2. Expectancy netta > 0 anche con costi ×1,5.
3. Expectancy della baseline sopra il 95° percentile di `random_direction` e di
   `random_time`.
4. Nessun anno della validazione sotto −10 R.
5. Sull'holdout, aperto una volta:
   - expectancy netta > 0 con costi ×1;
   - drawdown massimo non oltre 1,5 volte quello della validazione.

**`REJECTED`**: expectancy netta sulla validazione ≤ 0 con limite superiore
dell'intervallo < 0, oppure non supera i benchmark casuali.

**`INCONCLUSIVE`**: tutto il resto, inclusi pochi trade e intervalli che
contengono lo zero. Viene riportato come tale, senza aggiustamenti.

Queste soglie sono una proposta e non sono prova statistica (M0 §14.6). Una
strategia `CANDIDATE_FOR_DEMO` va in demo solo con un'approvazione separata, in
M3.

## 10. Decisioni da approvare

| ID | Decisione | Proposta | Alternativa |
|---|---|---|---|
| E1 | Risoluzione del percorso | M1 BID e ASK osservati | Solo M15, con regola conservativa a livello di barra (più ambiguità) |
| E2 | Weekend e orizzonte | Posizione tenuta nel weekend con gap simulato; uscita a tempo dopo 120 slot H1 | Chiusura forzata prima del weekend |
| E3 | Ambiguità e gap | Stop per primo; target in gap uscito al livello | Ordine deciso dal minuto di apertura o di chiusura |
| E4 | Posizioni | Una per simbolo, gli altri candidati nel controfattuale | Posizioni multiple |
| E5 | Costi | Commissione 0,35 pip per lato, slippage 0,1/0,2/0 pip | Valori del tuo broker, se li conosci già |
| E6 | Criteri di esito | §9 | Soglie diverse, purché fissate ora |
| E7 | Split | Sviluppo 2015–2020, validazione 2021–2023, holdout 2024 → ottobre 2026 | Holdout più corto (2025 → 2026) per avere più validazione |
| E8 | Swap | Non modellato in v1, con le notti tenute riportate nel report | Costo fisso per notte in pip |

## 11. Limiti

- Lo swap non è modellato (E8).
- Lo slippage è fisso e non dipende da volatilità o orario.
- L'ambiguità resta dentro il minuto e viene risolta in modo conservativo.
- Dukascopy non è il feed del broker. Spread e prezzi di esecuzione reali
  saranno diversi e il confronto si fa in M3 con il rendimento `broker`.
- I costi di default sono stimati e non vengono da un contratto broker.
- Con una sola strategia congelata, il walk-forward misura la stabilità nel
  tempo, non la capacità di adattamento.
- La prima valutazione usa solo EURUSD se il download delle altre coppie non è
  ancora finito; il report lo dichiara.

## 12. Test di accettazione M2

### T9 Simulatore (con percorsi sintetici costruiti a mano)

- **T9.1** LONG: entrata all'`ask_open` più lo slippage; stop toccato sul
  `bid_low`; R netto calcolato a mano.
- **T9.2** SHORT: uscita sull'ASK; uno spread che si allarga fa scattare lo
  stop prima di quanto farebbe il BID.
- **T9.3** Stop e target nello stesso minuto → `STOP` con `ambiguous = true`;
  la variante con target per primo dà `TARGET`.
- **T9.4** Gap del weekend oltre lo stop → `STOP_GAP` all'apertura; gap oltre il
  target → `TARGET` al livello.
- **T9.5** Uscita a tempo esattamente al 120° slot H1 in sessione, con il
  weekend nel mezzo.
- **T9.6** ASK mancante nel minuto di entrata → entrata al minuto successivo;
  ASK mancante fino a `expires_at` → `NOT_FILLED_NO_QUOTE`.
- **T9.7** Buco di 61 minuti durante la posizione → `path_gap = true`.
- **T9.8** Un candidato con posizione aperta va nel controfattuale e non nel
  rendimento `exec`.
- **T9.9** Perturbare il percorso dopo `exit_time` non cambia l'outcome.
- **T9.10** Stessi input → stesso `outcome_id` e stesso hash del report, su
  Linux e Windows.

### T10 Metriche e benchmark

- **T10.1** Expectancy, profit factor e drawdown su una sequenza di R scritta a
  mano.
- **T10.2** Block bootstrap riproducibile a seed fissato; l'intervallo contiene
  la media.
- **T10.3** Conteggio dei trade indipendenti su trade sovrapposti costruiti a
  mano.
- **T10.4** Benchmark casuali riproducibili; i percentili della baseline sono
  calcolati correttamente su una distribuzione nota.
- **T10.5** Con costi ×2 l'expectancy netta è ≤ quella con costi ×1, su ogni
  trade.

### T11 Parità

- **T11.1** Lo stesso dataset in due modi: replay di M1, oppure ciclo
  "runtime" in cui le barre arrivano una alla volta all'istante
  `available_at`, con lo snapshot ricostruito incrementalmente. Le sequenze di
  `evaluation_hash` sono identiche.
- **T11.2** Con barre in ritardo (disponibilità modellata > `as_of`) la parità
  vale ancora: entrambi i percorsi si astengono con lo stesso codice.

### T12 Protocollo

- **T12.1** Un run sull'holdout senza pre-registrazione viene rifiutato.
- **T12.2** Ogni apertura dell'holdout viene registrata; una seconda apertura
  senza `--reopen-holdout` viene rifiutata.
- **T12.3** Cambiare un byte della pre-registrazione cambia il fingerprint del
  run.
- **T12.4** La griglia di sensibilità rifiuta intervalli fuori dallo split di
  sviluppo.

## 13. Piano di lavoro

1. Import del dataset M1 e test di parità (T11).
2. Simulatore e costi (T9).
3. Metriche e benchmark (T10).
4. Protocollo e holdout (T12).
5. Primo report su EURUSD, prima sviluppo e poi validazione. L'holdout si apre
   solo dopo una tua approvazione esplicita.

Ogni passo si chiude con test verdi in CI su Linux e Windows. Il report finale di
M2 riporta i risultati così come sono, anche se negativi o inconcludenti.

## 14. Precisazioni nate dall'implementazione (2026-11-03)

Nessuna di queste cambia una decisione approvata.

- La pre-registrazione è in **TOML** invece che in YAML, perché Python lo legge
  senza dipendenze nuove (`tomllib`).
- Gli **split** sono costanti nel codice (`fxnext.research.protocol.SPLITS`). Un
  esperimento sceglie lo split ma non può spostarne i confini.
- La **regola del gap** (§3.4) si applica all'apertura di ogni minuto, escluso
  quello di entrata. Su dati continui non scatta quasi mai, ma copre anche i
  salti dentro la sessione.
- Il **risultato di un esperimento** si scrive tutto insieme (directory
  temporanea più `os.replace`) sotto il suo fingerprint. Se il processo si
  interrompe, si ricalcola da capo. Il replay sottostante resta crash-safe e
  riprendibile come in ADR-0014.
- Sulla validazione l'esito positivo si chiama **`VALIDATION_PASSED`**, perché
  `CANDIDATE_FOR_DEMO` richiede anche l'holdout (§9).
- I candidati che si fermano a `END_OF_DATA` (percorso finito prima dell'uscita)
  sono esclusi dalle metriche e contati nella copertura.
- I **minuti senza tick** (piatti, volume zero) valgono per le uscite ma non per
  l'entrata.
