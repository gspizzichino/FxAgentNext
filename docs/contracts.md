# Contratti — FxAgent Next

Stato: **bozza M0** · Versione contratti: `contracts-v0.2` (non ancora `v1`)

Pseudocodice Python 3.12. I nomi sono quelli che userà il codice. Le regole
normative sono marcate **DEVE** / **NON DEVE**. Le decisioni dietro i contratti
sono in [ADR-0002](adr/0002-modello-del-tempo.md), [0005](adr/0005-base-di-prezzo.md),
[0006](adr/0006-baseline-donchian.md), [0011](adr/0011-valutazione-guidata-dal-calendario.md),
[0012](adr/0012-immutabilita-profonda.md), [0013](adr/0013-identita-e-hashing.md),
[0014](adr/0014-audit-crash-safe.md).

## Changelog

- **v0.2.1** (2026-11-03, implementazione M1): `Timeframe.M1` per la sola sorgente;
  `dataset_id` calcolato sugli sha256 delle colonne canoniche (equivalente alle
  righe canoniche, indipendente dal writer Parquet); `evaluate()` costruisce da sé
  la `EvaluationRequest` da snapshot e config (`scheduler_version` costante del motore).
- **v0.2** (2026-11-02): sorgente Dukascopy con downloader e cache grezza (§13,
  ADR-0015). Nessun altro contratto cambia.
- **v0.1** (2026-10-23): `available_at` e `availability` per ogni osservazione
  anche in forma colonnare; serie allineate agli slot del calendario con stato
  esplicito; disponibilità delle barre derivate; scheduling (`as_of = chiusura
  attesa + evaluation_delay`), esito ritardato e ritardo massimo; immutabilità
  profonda (buffer NumPy non riscrivibili, `FrozenMap`, `FrozenJson`); identità
  separate (richiesta, evento, candidato, contenuto, run, output) con schema di
  hash `fxnext-hash-v1` senza arrotondamenti; protocollo audit/checkpoint
  crash-safe; stati di barra distinti; base di prezzo dei livelli; baseline
  Donchian formalizzata. Rimossi `STALE_SNAPSHOT` e il flag `NO_TRADE`.
- **v0** (2026-10-20): prima bozza.

---

## 1. Tipi di base immutabili

```python
class Model(pydantic.BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    # Ogni campo DEVE avere un tipo immutabile: scalari, str, AwareDatetime,
    # Enum, tuple[...], frozenset[...], FrozenMap, FrozenJson, Model, BarSeries.
    # list, dict, set, bytearray e np.ndarray NON sono ammessi come campi.
    # Un test di architettura scorre tutti i Model e fallisce se trova un
    # campo con tipo mutabile.

class FrozenMap(Mapping[K, V]):
    """Mapping immutabile, hashable, iterato in ordine canonico delle chiavi.

    Costruito da una copia privata dei dati; non espone __setitem__, __delitem__,
    update, né il dict interno. I valori DEVONO essere a loro volta immutabili
    (verificato in costruzione)."""

FrozenJson = None | bool | int | float | str | tuple["FrozenJson", ...] | FrozenMap[str, "FrozenJson"]
# Costruito da JSON con conversione ricorsiva: dict -> FrozenMap, list -> tuple.
# float NaN/±inf rifiutati. Chiavi solo str.
```

Nessuna nuova dipendenza: `FrozenMap` è una classe di ~50 righe in `domain`.

## 2. Tempo, calendario, strumenti

```python
class Timeframe(StrEnum):
    M1 = "M1"                             # solo sorgente Dukascopy (§13), mai negli snapshot
    M15 = "M15"; H1 = "H1"; H4 = "H4"     # .duration -> timedelta

class PriceBasis(StrEnum):
    BID = "BID"; ASK = "ASK"; MID = "MID"

class Provenance(StrEnum):               # per quote e prezzi di esecuzione
    OBSERVED = "OBSERVED"; MODELLED = "MODELLED"

class Availability(StrEnum):             # come è stato ottenuto available_at
    OBSERVED = "OBSERVED"                # istante reale di ricezione registrato
    MODELLED = "MODELLED"                # close_time + publication_delay (storico)

class DatasetProvenance(StrEnum):
    REAL = "REAL"; SYNTHETIC = "SYNTHETIC"

class Instrument(Model):
    symbol: str                          # normalizzato, es. "EURUSD"
    base: str; quote: str
    digits: int                          # 5 per EURUSD, 3 per USDJPY
    point: Decimal

class Slot(Model):
    """Intervallo atteso dal calendario per un timeframe."""
    timeframe: Timeframe
    open_time: AwareDatetime             # UTC, inclusivo
    close_time: AwareDatetime            # open_time + duration, esclusivo

class FxCalendar(Protocol):
    calendar_version: str
    def slots(self, tf: Timeframe, start: AwareDatetime, end: AwareDatetime) -> tuple[Slot, ...]: ...
    def slot_ending_at(self, tf: Timeframe, close_time: AwareDatetime) -> Slot | None: ...
    def previous_slots(self, slot: Slot, n: int) -> tuple[Slot, ...]: ...   # salta weekend e chiusure attese
    def next_slot(self, slot: Slot) -> Slot | None: ...
    def is_in_session(self, instant: AwareDatetime) -> bool: ...
```

Regole del calendario:

- Sessione settimanale: domenica 17:00 → venerdì 17:00 America/New_York. Chiusure
  attese da file versionato. Fuori sessione **non esistono slot**.
- Uno slot appartiene alla sessione se `[open_time, close_time)` è interamente
  in sessione. Con l'ancoraggio a NY tutti i confini M15/H1/H4 coincidono con
  i confini di sessione, quindi non esistono slot parziali.
- Gli slot H1 sono consecutivi "in tempo di sessione": l'ultimo slot del venerdì
  e il primo della domenica sono adiacenti, il weekend non è un gap.

## 3. Stato delle barre

Cinque situazioni distinte, mai equivalenti:

| Situazione | Dove vive | Significato |
|---|---|---|
| `VALID` | riga nel dataset, slot nello snapshot | Barra osservata che supera la validazione. |
| `FLAT_ZERO_VOLUME` | riga nel dataset, slot nello snapshot | O=H=L=C e volume 0. Osservata ma non affidabile come misura di mercato. Non è scartata né considerata valida. |
| `INVALID` | riga in `rejected` del dataset, slot nello snapshot senza prezzi | Riga presente ma che viola la validazione (OHLC incoerente, non finita, ≤ 0, ask < bid, conflitto). |
| `UNAVAILABLE` | solo nello snapshot | Slot atteso senza barra con `available_at <= as_of`. Al momento della decisione "mancante" e "non ancora arrivata" sono indistinguibili; la distinzione è solo ex post (§8). |
| `BEFORE_COVERAGE` | solo nello snapshot | Slot precedente all'inizio di copertura dichiarato dal dataset. |
| mercato chiuso | nessuno slot | Intervallo fuori sessione o chiusura attesa: non è un dato mancante e non compare nelle serie. |
| sintetico | livello dataset (`DatasetProvenance.SYNTHETIC`) | Solo dataset di test generati. Mai mescolati con `REAL` in un run; il run fingerprint include la provenienza. Gli importer non producono mai barre sintetiche. |

Barre piatte fuori sessione (riempitivi di weekend dei file Dukascopy) sono
contate nel report di qualità come `CLOSED_FILLER` e non entrano nel dataset.

```python
class SlotStatus(IntEnum):               # ordinato per gravità (precedenza)
    VALID = 0
    FLAT_ZERO_VOLUME = 1
    INVALID = 2
    UNAVAILABLE = 3
    BEFORE_COVERAGE = 4
```

## 4. Serie colonnari

```python
class BarSeries:
    """Serie allineata agli slot: una riga per ogni slot atteso della finestra,
    in ordine crescente di open_time. Immutabile in profondità (ADR-0012)."""
    symbol: str; timeframe: Timeframe; basis: PriceBasis
    # colonne (np.ndarray read-only su buffer bytes immutabile, vedi §5)
    open_time_ns: int64        # = slot.open_time
    close_time_ns: int64       # = slot.close_time
    available_at_ns: int64     # INT64_MIN se status in {UNAVAILABLE, BEFORE_COVERAGE}
    availability: uint8        # Availability; 255 se non applicabile
    status: uint8              # SlotStatus
    open, high, low, close: float64   # NaN se status in {INVALID, UNAVAILABLE, BEFORE_COVERAGE}
    volume: float64            # NaN se ignoto
    source_count: uint8        # 1 per M15 nativo; 4 (H1) o 16 (H4) per derivate
```

Invarianti verificate in costruzione (eccezione `SeriesInvariantError`):

1. `open_time_ns` strettamente crescente e uguale agli slot del calendario per
   la finestra; `close_time_ns = open_time_ns + duration`.
2. Per `VALID` e `FLAT_ZERO_VOLUME`: prezzi finiti > 0, `high >= max(open, close)`,
   `low <= min(open, close)`, `available_at_ns >= close_time_ns`.
3. Per gli altri stati: prezzi NaN.

### 4.1 `available_at` e `availability`

- **Storico (modellata)**: `available_at = close_time + publication_delay`,
  `availability = MODELLED`. `publication_delay` è un parametro del run
  (default proposto 0 s), incluso nel run fingerprint.
- **Osservata**: `available_at` = istante UTC in cui l'adapter ha ricevuto per la
  prima volta la barra chiusa, `availability = OBSERVED`. Il CSV generico può
  fornire una colonna `available_at` opzionale (usata dai test e da registrazioni
  del runtime); se presente, le righe sono `OBSERVED`.
- **Derivata H1/H4 da M15**: esiste come `VALID` solo se tutte le 4/16 barre M15
  sorgente dello slot sono `VALID` e disponibili a `as_of`.
  `available_at = max(available_at delle sorgenti)`;
  `availability = OBSERVED` solo se tutte le sorgenti sono `OBSERVED`,
  altrimenti `MODELLED`. Lo stato di uno slot derivato non `VALID` è lo stato
  più grave tra le sorgenti secondo `SlotStatus`. Ne segue
  `available_at >= close_time` della derivata.
- Revisioni di una barra già osservata non sono supportate in v0.1: due righe
  diverse per lo stesso slot sono un conflitto (`INVALID`, motivo `CONFLICTING_DUPLICATE`).

### 4.2 Garanzia di non disponibilità del futuro

- Lo snapshot builder include una barra solo se `available_at <= as_of`; altrimenti
  lo slot è `UNAVAILABLE`.
- `MarketSnapshot` **verifica di nuovo** in costruzione che per ogni slot
  `close_time <= as_of` e, se ha prezzi, `available_at <= as_of`. Una violazione
  solleva `SnapshotInvariantError`: il bug di un builder non può raggiungere il core.
- La stessa regola vale per le righe `rejected`: uno slot è `INVALID` solo se la
  riga invalida era disponibile a `as_of` (stessa regola di `available_at`),
  altrimenti è `UNAVAILABLE`.
- Lo snapshot contiene solo gli slot della finestra dichiarata da
  `Strategy.requirements()`, non l'intero dataset. Il core non riceve riferimenti
  al dataset, allo store o all'orologio.

## 5. Immutabilità profonda (ADR-0012)

- **Ownership NumPy**: `BarSeries` copia ogni colonna in un oggetto `bytes`
  proprio e la espone come `np.frombuffer(buf, dtype)`. Un array costruito su
  `bytes` è read-only e **non può essere reso scrivibile** (`setflags(write=True)`
  solleva `ValueError`), perché il buffer sottostante è immutabile. Nessun
  chiamante conserva alias scrivibili: gli array passati al costruttore sono
  copiati, non adottati.
- Le viste (slicing) restano read-only. Chi nel core ha bisogno di un array di
  lavoro mutabile **DEVE** fare `.copy()` esplicito.
- `Mapping` nei modelli: solo `FrozenMap`. `MarketSnapshot.series` è
  `FrozenMap[Timeframe, BarSeries]`.
- Parametri strategia: modelli `Model` con soli campi immutabili; il registry
  rifiuta un `params_model` che violi la regola.
- JSON congelato: solo `FrozenJson`.
- Test di non mutazione: lo `content_hash` di snapshot, config e stato è
  ricalcolato dopo ogni `evaluate` nei test e confrontato con quello iniziale.

## 6. Snapshot

```python
class DatasetRef(Model):
    dataset_id: str                      # content hash (§9)
    symbol: str; timeframe: Timeframe; basis: PriceBasis
    provenance: DatasetProvenance
    coverage_start: AwareDatetime        # primo slot coperto dichiarato
    coverage_end: AwareDatetime

class SnapshotRequirements(Model):
    timeframe: Timeframe
    basis: PriceBasis
    slots_back: int                      # slot richiesti prima dello slot di segnale
    # la finestra è [slot_t - slots_back, slot_t], slots_back + 1 slot

class MarketSnapshot(Model):
    symbol: str
    instrument: Instrument
    as_of: AwareDatetime
    signal_slot: Slot                    # slot che chiude a expected_close (§7)
    next_slot: Slot | None               # slot successivo in sessione, None se non esiste
                                         #   prima di signal_slot.close_time + duration
    calendar_version: str
    series: FrozenMap[Timeframe, BarSeries]
    quote: Quote | None                  # None in M1
    sources: tuple[DatasetRef, ...]
    snapshot_hash: str                   # content hash, calcolato in costruzione
```

## 7. Scheduling (ADR-0011)

Quattro istanti distinti:

| Simbolo | Nome | Definizione |
|---|---|---|
| `C_k` | chiusura attesa | `close_time` dello slot k del timeframe di segnale, dal calendario. |
| `A_k` | disponibilità della barra | `available_at` della barra dello slot k (osservata o modellata). |
| `T_k` | istante di valutazione (`as_of`) | `C_k + evaluation_delay`. **Sempre nominale, mai l'orologio reale.** |
| `W_k` | istante effettivo di esecuzione | Orologio reale del runtime quando la valutazione viene calcolata. Solo runtime; in replay `W_k = T_k`. |

Parametri del run (inclusi nel fingerprint): `evaluation_delay` (default
proposto 0 s in replay; valore runtime fissato in M3 e usato anche nei test di
parità) e `max_dispatch_lateness` (solo runtime, ≤ validità del candidato).

Regole:

1. Una e una sola richiesta di valutazione per `(strategia, config, simbolo, C_k)`.
   Nessun retry con `as_of` diverso.
2. Lo snapshot usa `as_of = T_k`: entra solo ciò che era disponibile a `T_k`. Se
   la barra dello slot k ha `A_k > T_k` lo slot è `UNAVAILABLE` e l'esito è
   l'astensione `BAR_NOT_AVAILABLE`, deterministicamente.
3. Valutazione tardiva (solo runtime): se `W_k − T_k > max_dispatch_lateness` la
   valutazione viene comunque calcolata e registrata per l'audit (è funzione pura
   di `T_k`), ma con `dispatch = NOT_DISPATCHED_LATE`: il candidato non raggiunge
   Risk/Execution. Uno slot interamente perso per downtime è registrato come
   `MISSED_DOWNTIME` dopo il riavvio, senza dispatch.
4. Il candidato ha comunque `expires_at`; Execution (M3) rifiuta oltre quell'istante.

```python
class EvaluationRequest(Model):
    strategy_id: str; strategy_version: str; config_hash: str
    symbol: str
    signal_timeframe: Timeframe
    expected_close: AwareDatetime        # C_k
    as_of: AwareDatetime                 # T_k
    scheduler_version: str
    request_id: str                      # content hash dei campi sopra (§9)

class Dispatch(StrEnum):                 # solo runtime (M3); in replay sempre REPLAY
    REPLAY = "REPLAY"
    DISPATCHED = "DISPATCHED"
    NOT_DISPATCHED_LATE = "NOT_DISPATCHED_LATE"
    MISSED_DOWNTIME = "MISSED_DOWNTIME"

class ScheduleRecord(Model):             # metadato operativo, fuori dall'output_hash
    request_id: str
    dispatch: Dispatch
    executed_at: AwareDatetime | None    # W_k
```

## 8. Strategia

```python
class StrategyConfig(Model):
    strategy_id: str                     # "baseline_donchian_breakout"
    strategy_version: str                # "1"
    params: Model                        # modello params specifico, campi immutabili
    detection_param_names: tuple[str, ...]   # sottoinsieme che definisce l'evento
    config_hash: str                     # calcolato (§9)

class StrategyState(Model):
    schema_version: str
    payload: FrozenJson                  # vuoto per strategie senza stato
    state_hash: str

class Comparator(StrEnum):
    GT = ">"; GE = ">="; LT = "<"; LE = "<="; EQ = "=="; IN = "in"

class FilterKind(StrEnum):
    DATA_GATE = "DATA_GATE"; MARKET_GATE = "MARKET_GATE"; STRATEGY_FILTER = "STRATEGY_FILTER"

class FilterStatus(StrEnum):
    PASSED = "PASSED"; FAILED = "FAILED"; NOT_EVALUATED = "NOT_EVALUATED"

class FilterResult(Model):
    name: str
    kind: FilterKind
    status: FilterStatus
    measure: FrozenJson                  # valore osservato (es. conteggio slot non validi)
    threshold: FrozenJson
    comparator: Comparator | None
    reason_code: ReasonCode | None       # valorizzato se e solo se FAILED

class ReasonCode(StrEnum):
    BAR_NOT_AVAILABLE = "BAR_NOT_AVAILABLE"      # barra di segnale non disponibile a as_of
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"  # finestra prima della copertura del dataset
    DATA_GAP = "DATA_GAP"                          # slot UNAVAILABLE nella finestra
    INVALID_DATA = "INVALID_DATA"                  # slot INVALID nella finestra
    FLAT_BAR_IN_WINDOW = "FLAT_BAR_IN_WINDOW"      # slot FLAT_ZERO_VOLUME nella finestra
    MARKET_CLOSED = "MARKET_CLOSED"                # nessuno slot di entrata in sessione
    NO_EVENT = "NO_EVENT"
    FILTER_REJECTED = "FILTER_REJECTED"            # riservato a filtri futuri
    INVALID_GEOMETRY = "INVALID_GEOMETRY"

class Direction(StrEnum):
    LONG = "LONG"; SHORT = "SHORT"

class StructuralEvent(Model):
    event_id: str                        # §9
    kind: str                            # "donchian_breakout"
    timeframe: Timeframe
    anchor_open_time: AwareDatetime      # slot che genera l'evento
    direction: Direction
    discriminator: FrozenJson            # struttura che distingue eventi sulla stessa barra/direzione
    details: FrozenJson

class Candidate(Model):
    candidate_id: str                    # §9
    event_id: str
    direction: Direction
    level_basis: PriceBasis              # base di entry_ref, stop e target
    entry_ref: float                     # riferimento, NON prezzo eseguibile
    stop: float
    target: float
    available_at: AwareDatetime          # = as_of
    expires_at: AwareDatetime
    # invarianti: LONG => stop < entry_ref < target; SHORT => target < entry_ref < stop;
    # expires_at > available_at

class Abstention(Model):
    reason_code: ReasonCode
    detail: str                          # testo deterministico da template, non LLM

class StrategyEvaluation(Model):
    request: EvaluationRequest
    engine_version: str
    snapshot_hash: str
    events: tuple[StructuralEvent, ...]  # event_id unici; vuota se nessun evento
    outcome: Candidate | Abstention      # esattamente uno
    filters: tuple[FilterResult, ...]    # tutti quelli dichiarati, in ordine
    measures: FrozenMap[str, float]
    next_state: StrategyState
    evaluation_hash: str                 # content hash dell'intera valutazione (§9)

class Strategy(Protocol):
    strategy_id: ClassVar[str]
    strategy_version: ClassVar[str]
    params_model: ClassVar[type[Model]]
    research_only: ClassVar[bool]
    declared_filters: ClassVar[tuple[str, ...]]
    def requirements(self, params) -> tuple[SnapshotRequirements, ...]: ...
    def evaluate(self, market_snapshot: MarketSnapshot,
                 strategy_config: StrategyConfig,
                 strategy_state: StrategyState) -> StrategyEvaluation: ...
```

Regole della valutazione:

- I filtri vengono valutati nell'ordine di `declared_filters`. Al primo `FAILED`
  i successivi sono `NOT_EVALUATED`. `filters` contiene sempre tutti i filtri
  dichiarati.
- `outcome` è `Abstention` con il `reason_code` del primo filtro fallito,
  oppure `NO_EVENT`/`INVALID_GEOMETRY`, oppure `Candidate`.
- Due eventi con lo stesso `event_id` nella stessa valutazione sono un errore di
  programma (`DuplicateEventError`), non un esito.
- Il core NON DEVE leggere l'orologio, usare casualità non seminata da config,
  fare I/O, chiamare LLM o mutare input.

### Annotazione ex post (solo `explain`)

`explain` può aggiungere, marcata `ex_post`, la causa di uno slot `UNAVAILABLE`:
`LATE_ARRIVAL(available_at)` se la barra esiste nel dataset con `A_k > T_k`,
`MISSING` se non esiste, `INVALID_ROW(motivo)` se è nel file `rejected`. Questa
informazione non entra mai nella valutazione né nel suo hash.

## 9. Identità e hashing (ADR-0013)

### 9.1 Schema `fxnext-hash-v1`

```
content_hash(type_tag, obj) = sha256( b"fxnext-hash-v1\x00" + type_tag.encode() + b"\x00" + canon(obj) ).hexdigest()
```

- `type_tag` = nome del tipo + versione di schema del tipo, es. `"StrategyEvaluation/1"`.
  Aggiungere, togliere o rinominare un campo richiede di incrementare la
  versione del tipo. Separazione di dominio: oggetti di tipo diverso non possono
  collidere per costruzione dell'input.
- `canon(obj)`: JSON UTF-8 senza spazi (`separators=(",", ":")`), `allow_nan=False`,
  dopo trasformazione dei valori:

| Valore | Codifica |
|---|---|
| mappe / modelli | oggetto JSON con chiavi ordinate per byte UTF-8; **tutti** i campi presenti, `None` esplicito |
| tuple | array JSON, ordine preservato |
| frozenset | array JSON ordinato per codifica canonica degli elementi |
| int | numero JSON intero |
| bool, None | `true`/`false`/`null` |
| float | stringa `"f:" + float.hex(x)` — rappresentazione **esatta** dei 64 bit. `-0.0` normalizzato a `0.0`; NaN e ±inf rifiutati |
| Decimal | stringa `"d:" + forma normalizzata` |
| datetime | stringa `"t:" + nanosecondi UTC interi` |
| Enum | stringa del valore |
| str | stringa NFC; identificativi solo ASCII |
| BarSeries | oggetto con metadati + per colonna `sha256` dei byte little-endian del buffer |

Nessun arrotondamento: due valori float diversi in un solo bit producono hash
diversi. Il determinismo cross-platform è ottenuto vincolando il calcolo
(§9.4), non perdendo informazione nell'hash.

### 9.2 Identità

| Identità | Definizione | Cosa identifica |
|---|---|---|
| `dataset_id` | `content_hash("Dataset/1", {schema, symbol, tf, basis, provenance, coverage, righe canonical ordinate per open_time})` calcolato in streaming | contenuto, indipendente da compressione e writer Parquet |
| `config_hash` | `content_hash("StrategyConfig/1", {strategy_id, strategy_version, params, detection_param_names})` | configurazione |
| `request_id` | `content_hash("EvaluationRequest/1", campi di §7)` | la **domanda**: strategia+config+simbolo+istante; indipendente dai dati |
| `snapshot_hash` | `content_hash("MarketSnapshot/1", snapshot senza snapshot_hash)` | dati visti |
| `event_id` | `content_hash("StructuralEvent/1", {strategy_id, strategy_version, detection_params, symbol, timeframe, kind, direction, anchor_open_time, discriminator})` | l'evento strutturale; non dipende da parametri di stop/target |
| `candidate_id` | `content_hash("Candidate/1", {event_id, config_hash, direction, level_basis, entry_ref, stop, target, expires_at})` | la proposta operativa per quell'evento con quella config |
| `evaluation_hash` | `content_hash("StrategyEvaluation/1", valutazione senza evaluation_hash)` | il **contenuto** della risposta |
| `run_fingerprint` | `content_hash("RunFingerprint/1", {dataset_ids ordinati, provenance, config_hash, engine_version, scheduler_version, calendar_version, hash_schema, evaluation_delay, publication_delay, simboli ordinati, start, end, code_version})` | gli input completi del run |
| `output_hash` | catena: `h_0 = content_hash("RunOutput/1", run_fingerprint)`, `h_i = sha256(h_{i-1} ‖ request_id_i ‖ evaluation_hash_i)` sulle valutazioni in ordine `(as_of, symbol, strategy_id)` | l'output completo, verificabile incrementalmente |

`code_version` è il commit git; con working tree sporco il run è marcato
`non_reproducible` e la ripresa è rifiutata.

Discriminante: ogni strategia definisce una struttura `discriminator` che rende
unici gli eventi sulla stessa barra e direzione (per la baseline: slot di inizio e
fine del canale e livello del canale). Il discriminante fa parte del contratto
della versione della strategia.

### 9.3 Collisioni

Gli identificativi sono sha256 completi (64 hex), mai troncati nel dominio. Ogni
store che indicizza per identità conserva anche il contenuto canonico o il suo
`content_hash`: stesso identificativo con contenuto diverso è un
`IntegrityError` che interrompe il run (si tratta di un bug, non di una
collisione credibile). In particolare: stessa `request_id` con `evaluation_hash`
diverso nello stesso `run_fingerprint` → errore. Gli identificativi corti per il
campo `comment` di MT5 (M3) sono derivati al confine e verificati unici
nell'outbox.

### 9.4 Vincoli di calcolo per il determinismo

- Le grandezze che entrano negli hash DEVONO essere calcolate solo con
  `+ − × ÷`, `abs`, `max`, `min` e confronti in IEEE-754 binary64, in un ordine
  fissato dalla specifica (somme in ordine crescente di indice, sequenziali).
- NON ammessi nel core per grandezze hashate: funzioni trascendenti di libm,
  riduzioni NumPy con ordine non garantito (`np.sum`, `np.mean` su float),
  parallelismo, `float32`.
- Questi vincoli sono verificati da hash di regressione identici su Linux e Windows in CI.

## 10. Audit e checkpoint (ADR-0014)

Layout di una destinazione di run:

```
runs/<run_fingerprint>/
  LOCK                       # lock esclusivo del processo scrivente
  manifest.json              # unico punto di commit
  segments/seg-000001.parquet
  segments/.tmp-*            # file in scrittura (mai letti)
  quarantine/                # orfani spostati dal recupero
```

```python
class SegmentEntry(Model):
    seq: int                             # 1, 2, 3, ... senza buchi
    file: str
    sha256: str                          # dei byte del file
    rows: int
    first_request_id: str; last_request_id: str
    first_as_of: AwareDatetime; last_as_of: AwareDatetime
    chain_head: str                      # output chain dopo l'ultima riga del segmento

class RunManifest(Model):
    manifest_version: int                # incrementato a ogni commit
    run_id: str                          # casuale, per diagnostica
    run_fingerprint: str
    fingerprint_inputs: FrozenJson
    status: Literal["RUNNING", "COMPLETED", "NON_REPRODUCIBLE"]
    segments: tuple[SegmentEntry, ...]
    last_committed_as_of: AwareDatetime | None
    chain_head: str
    output_hash: str | None              # = chain_head finale, solo COMPLETED
    evaluation_count: int
```

Protocollo di scrittura:

1. **Lock**: lock esclusivo sul file `LOCK` con `fcntl.flock` (POSIX) o
   `msvcrt.locking` (Windows), entrambi stdlib, rilasciati dal sistema operativo
   alla morte del processo. Se il lock è già preso il secondo replay termina
   subito con `DestinationLocked`, senza attendere e senza scrivere.
2. **Segmento**: le valutazioni sono accumulate in memoria per mese UTC di
   `as_of` (confine deterministico). Scrittura in `segments/.tmp-<seq>-<rand>`,
   `flush` + `fsync`, calcolo `sha256` dei byte, `os.replace` sul nome finale
   `seg-<seq>.parquet`, `fsync` della directory dove supportato (POSIX).
3. **Commit**: scrittura di `manifest.json.tmp` con il nuovo segmento, `fsync`,
   `os.replace` su `manifest.json`. **Questo è il commit del checkpoint.** Un
   segmento non elencato nel manifest non esiste per nessun lettore.
4. **Completamento**: dopo l'ultimo segmento il manifest passa a `COMPLETED` con
   `output_hash`. Una destinazione `COMPLETED` accetta solo `verify`.

Protocollo di recupero (ripresa con `--resume`, o avvio su destinazione esistente):

1. Acquisire il lock.
2. Leggere `manifest.json`. Se manca ma esistono segmenti → errore, intervento manuale.
3. Verificare per ogni segmento elencato esistenza, `sha256`, conteggio righe e
   continuità della catena; un errore → `IntegrityError`, nessuna riparazione automatica.
4. Spostare in `quarantine/` ogni `.tmp-*` e ogni `seg-*` non elencato (orfani di
   un crash tra `os.replace` del segmento e commit del manifest).
5. Riprendere dal primo `C_k` con `T_k > last_committed_as_of`, ripristinando
   `chain_head`. Il segmento in corso viene ricalcolato da capo.

Garanzie:

- **Nessun duplicato**: i segmenti hanno `seq` contigui e intervalli di
  `request_id`/`as_of` disgiunti e crescenti; `verify` ricalcola la catena e
  controlla l'unicità di `request_id`.
- **Lettori** (`explain`, DuckDB) leggono il manifest e solo i segmenti elencati,
  mai un glob della cartella; non hanno bisogno del lock.
- **Ripresa = run ininterrotto**: la catena dipende solo dalle valutazioni in
  ordine, non dai confini di segmento né dal numero di riprese.
- Windows: `os.replace` è atomico sullo stesso volume NTFS; il `fsync` della
  directory non esiste ed è documentato come limite.

## 11. Baseline `baseline_donchian_breakout` v1

Parametri congelati: `channel_slots = 20`, `atr_slots = 14`,
`stop_atr_mult = 1.5`, `target_r = 2.0`, `signal_timeframe = H1`,
`basis = BID`, `validity_slots = 1`. `detection_param_names = ("channel_slots",
"signal_timeframe", "basis")`. `research_only = True`. Senza stato.

Notazione: `t` = slot H1 che chiude a `C_k`; `t−j` = j-esimo slot in sessione
precedente secondo il calendario (salta weekend e chiusure attese). Finestra
richiesta: slot `t−21 … t` (22 slot, `slots_back = 21`).

Grandezze (solo slot `VALID`):

- `U_t = max(high[t−20 … t−1])`, `L_t = min(low[t−20 … t−1])` — 20 slot, **la barra
  `t` è esclusa**.
- `U_{t−1} = max(high[t−21 … t−2])`, `L_{t−1} = min(low[t−21 … t−2])` — canale
  della barra precedente.
- `TR_i = max(high_i − low_i, abs(high_i − close_{i−1}), abs(low_i − close_{i−1}))`
  per `i = t−13 … t` (14 valori, usano `close` da `t−14`).
- `ATR_t = (((TR_{t−13} + TR_{t−12}) + …) + TR_t) / 14` — media semplice,
  somma sequenziale in ordine crescente. Warm-up = esattamente 15 slot; memoria
  finita, nessuna dipendenza dall'inizio del dataset.

Filtri dichiarati, in ordine:

| # | Nome | Tipo | Passa se | Motivo se fallisce |
|---|---|---|---|---|
| 1 | `signal_bar_valid` | DATA_GATE | `status[t] == VALID` | `UNAVAILABLE`→`BAR_NOT_AVAILABLE`; `INVALID`→`INVALID_DATA`; `FLAT_ZERO_VOLUME`→`FLAT_BAR_IN_WINDOW`; `BEFORE_COVERAGE`→`INSUFFICIENT_HISTORY` |
| 2 | `history_coverage` | DATA_GATE | nessuno slot `BEFORE_COVERAGE` in `t−21 … t−1` | `INSUFFICIENT_HISTORY` |
| 3 | `window_available` | DATA_GATE | nessuno slot `UNAVAILABLE` in `t−21 … t−1` | `DATA_GAP` |
| 4 | `window_valid` | DATA_GATE | nessuno slot `INVALID` in `t−21 … t−1` | `INVALID_DATA` |
| 5 | `window_not_flat` | DATA_GATE | nessuno slot `FLAT_ZERO_VOLUME` in `t−21 … t−1` | `FLAT_BAR_IN_WINDOW` |
| 6 | `atr_positive` | DATA_GATE | `ATR_t > 0` | `INVALID_GEOMETRY` |
| 7 | `entry_slot_in_session` | MARKET_GATE | `next_slot` esiste e `next_slot.open_time == t.close_time` | `MARKET_CLOSED` |

`measure` dei gate 2–5: numero di slot offending e `open_time` del primo.

Evento (dopo i gate):

- LONG se `close_t > U_t` **e** `close_{t−1} <= U_{t−1}`.
- SHORT se `close_t < L_t` **e** `close_{t−1} >= L_{t−1}`.
- Disuguaglianze strette sul breakout: `close_t == U_t` non è breakout. LONG e
  SHORT insieme sono impossibili (`L_t <= U_t`). Nessun evento → `NO_EVENT`.
- `discriminator = {channel_first_open, channel_last_open, level}` con `level = U_t` o `L_t`.

Livelli (base BID, aritmetica esatta in quest'ordine):

- `e = close_t`, `d = 1.5 * ATR_t`
- LONG: `stop = e − d`, `target = e + 2.0 * d`; SHORT: `stop = e + d`, `target = e − 2.0 * d`
- Geometria non coerente o `stop <= 0` o `target <= 0` → `INVALID_GEOMETRY`.
- `available_at = as_of`; `expires_at = next_slot.close_time` (fine dello slot di entrata).

Comportamenti:

- Weekend e chiusure attese: non sono slot, quindi il canale li scavalca; non
  producono `DATA_GAP`.
- Ultimo slot prima di una chiusura (es. venerdì 16:00–17:00 NY): gate 7 fallisce
  → `MARKET_CLOSED`. Nessun candidato attraversa il weekend.
- Gap, barre non disponibili, invalide o piatte in qualunque slot della finestra
  → astensione con il motivo del primo gate fallito; nessun valore imputato.

## 12. Base di prezzo dei livelli

- Il segnale, `entry_ref`, `stop` e `target` della baseline sono tutti su base
  **BID** (`level_basis = BID`), la sola sempre disponibile.
- `entry_ref` è un riferimento di decisione, non un prezzo eseguibile.
- I prezzi effettivi di esecuzione (LONG su ASK, SHORT su BID) e la traduzione
  dei livelli nei trigger broker (in MT5 SL/TP di un BUY scattano sul BID, di un
  SELL sull'ASK) appartengono al modello di esecuzione (M2) o al broker (M3), che
  li registra separatamente con `Provenance`. I livelli del candidato non vengono
  mai sovrascritti.

## 13. Sorgente Dukascopy (ADR-0015)

```python
class RawFetchStatus(StrEnum):
    OK = "OK"                     # file scaricato e decompresso correttamente
    EMPTY = "EMPTY"               # 200 con corpo vuoto: nessun dato quel giorno
    NOT_FOUND = "NOT_FOUND"       # 404
    FAILED = "FAILED"             # errori transitori oltre il limite di tentativi
    CORRUPT = "CORRUPT"           # corpo troncato o LZMA non valido
    SOURCE_CHANGED = "SOURCE_CHANGED"   # nuovo download con byte diversi dal file in cache

class RawFetchRecord(Model):      # una riga per tentativo concluso, append-only (JSONL)
    url: str
    symbol: str; basis: PriceBasis
    day: date                     # giorno UTC del file
    status: RawFetchStatus
    http_status: int | None
    attempts: int
    bytes: int
    sha256: str | None            # dei byte compressi
    cache_path: str | None
    fetched_at: AwareDatetime
    downloader_version: str
```

- URL: `https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{YYYY}/{MM-1:02d}/{DD:02d}/{BID|ASK}_candles_min_1.bi5`
  (mese a base 0, giorno UTC).
- Cache: `data/raw/dukascopy/{SYMBOL}/{BASIS}/{YYYY}/{YYYY-MM-DD}.{sha256[:12]}.bi5`,
  scritta con `.tmp` + `os.replace`, mai sovrascritta. Più versioni dello stesso
  giorno possono coesistere; l'import usa quella indicata dal log (la prima `OK`,
  salvo scelta esplicita).
- Parser: record da 24 byte big-endian `(secondi dall'inizio del giorno: u32,
  open, close, low, high: i32, volume: f32)`, prezzo = intero / 10^digits della
  sorgente (5, 3 per JPY). Da confermare su un file reale prima di `v1`.
- Aggregazione M1 → M15: richiede i 15 record del quarto d'ora; un minuto assente
  → M15 `INVALID` (`INCOMPLETE_SOURCE`); tutti i 15 piatti a volume zero →
  `FLAT_ZERO_VOLUME`; altrimenti O = primo open, H = max, L = min, C = ultimo
  close, volume = somma sequenziale. Le M15 fuori sessione seguono §3 (`CLOSED_FILLER`).
- Controllo incrociato BID/ASK sulla stessa M15: `ask_open >= bid_open` e
  `ask_close >= bid_close`; violazione → entrambe `INVALID` (`NEGATIVE_SPREAD`).
- `dataset_id` dipende dal contenuto canonico delle righe; il manifest elenca
  anche gli `sha256` dei file grezzi usati e la `parser_version`.

## 14. Contratti futuri (solo nomi)

- M2: `CostModel`, `ExecutionModel`, `SimulatedFill`, `Outcome`, `ExperimentSpec`, `MetricsReport`.
- M3: `AccountIdentity`, `SymbolSpec`, `RiskDecision`, `OrderIntent`, `OrderState`,
  `BrokerPosition`, `ReconciliationReport`, `KillSwitch`.
- M4: `ResearchProposal`.
