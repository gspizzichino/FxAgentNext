# Contratti — FxAgent Next

Stato: **bozza M0** · Versione contratti: `contracts-v0` (diventa `v1` all'approvazione di M1)

Pseudocodice Python 3.12. Tutti i modelli sono immutabili (`frozen=True`),
rifiutano campi extra e datetime naive. I nomi sono quelli che userà il codice.

## Tempo e strumenti

```python
class Timeframe(StrEnum):
    M15 = "M15"; H1 = "H1"; H4 = "H4"
    # .duration -> timedelta

class PriceBasis(StrEnum):
    BID = "BID"; ASK = "ASK"; MID = "MID"

class Provenance(StrEnum):
    OBSERVED = "OBSERVED"; MODELLED = "MODELLED"

class Instrument(Model):
    symbol: str                 # normalizzato, es. "EURUSD"
    base: str; quote: str
    digits: int                 # 5 per EURUSD, 3 per USDJPY
    point: Decimal              # 0.00001, 0.001
```

## Barre e serie

```python
class BarFlag(StrEnum):
    NO_TRADE = "NO_TRADE"           # barra piatta a volume zero
    DERIVED = "DERIVED"             # aggregata da timeframe inferiore

class Bar(Model):
    symbol: str
    timeframe: Timeframe
    basis: PriceBasis
    open_time: AwareDatetime        # UTC, inclusivo
    close_time: AwareDatetime       # open_time + timeframe, esclusivo
    available_at: AwareDatetime     # >= close_time
    open: float; high: float; low: float; close: float
    volume: float | None
    flags: frozenset[BarFlag]
    # invarianti: high >= max(open, close), low <= min(open, close), prezzi > 0 finiti

class BarSeries:                    # rappresentazione colonnare (numpy) per il core
    symbol: str; timeframe: Timeframe; basis: PriceBasis
    open_time: np.ndarray[int64]    # ns UTC, strettamente crescente
    open, high, low, close: np.ndarray[float64]
    flags: np.ndarray[uint8]
    # solo barre chiuse e disponibili a as_of; i gap sono assenze, descritte in DataQuality
```

## Snapshot

```python
class GapInfo(Model):
    timeframe: Timeframe
    expected_open_time: AwareDatetime
    kind: Literal["UNEXPECTED", "EXPECTED_CLOSURE"]

class DataQuality(Model):
    gaps_in_window: tuple[GapInfo, ...]
    invalid_bars_in_window: int
    no_trade_bars_in_window: int

class Quote(Model):
    symbol: str; time: AwareDatetime
    bid: float; ask: float          # ask >= bid
    provenance: Provenance

class DatasetRef(Model):
    dataset_id: str                 # sha256 del contenuto canonico
    symbol: str; timeframe: Timeframe; basis: PriceBasis

class MarketSnapshot(Model):
    symbol: str
    instrument: Instrument
    as_of: AwareDatetime
    calendar_version: str
    expected_last_close: AwareDatetime | None   # dal calendario; None se mercato chiuso
    series: Mapping[Timeframe, BarSeries]       # solo barre con available_at <= as_of
    quote: Quote | None                         # None in replay su soli OHLC
    quality: Mapping[Timeframe, DataQuality]
    sources: tuple[DatasetRef, ...]
    snapshot_hash: str
```

## Strategia

```python
class StrategyConfig(Model):
    strategy_id: str                # "baseline_donchian_breakout"
    strategy_version: str           # "1"
    params: FrozenParams            # modello Pydantic specifico della strategia
    config_hash: str                # calcolato, non fornito

class StrategyState(Model):
    schema_version: str
    payload: FrozenJson             # vuoto per strategie senza stato
    state_hash: str

class Comparator(StrEnum):
    GT = ">"; GE = ">="; LT = "<"; LE = "<="; EQ = "=="; IN = "in"

class FilterKind(StrEnum):
    DATA_GATE = "DATA_GATE"         # qualità/sufficienza dati
    STRATEGY_FILTER = "STRATEGY_FILTER"

class FilterStatus(StrEnum):
    PASSED = "PASSED"; FAILED = "FAILED"; NOT_EVALUATED = "NOT_EVALUATED"

class FilterResult(Model):
    name: str                       # stabile, es. "history_length"
    kind: FilterKind
    status: FilterStatus
    measure: float | str | None     # valore osservato
    threshold: float | str | None
    comparator: Comparator | None
    reason_code: ReasonCode | None  # valorizzato se FAILED

class ReasonCode(StrEnum):
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    DATA_GAP = "DATA_GAP"
    INVALID_DATA = "INVALID_DATA"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    MARKET_CLOSED = "MARKET_CLOSED"
    NO_EVENT = "NO_EVENT"
    FILTER_REJECTED = "FILTER_REJECTED"
    INVALID_GEOMETRY = "INVALID_GEOMETRY"

class Direction(StrEnum):
    LONG = "LONG"; SHORT = "SHORT"

class StructuralEvent(Model):
    event_id: str
    kind: str                       # "donchian_breakout"
    timeframe: Timeframe
    bar_open_time: AwareDatetime    # barra che ha generato l'evento
    direction: Direction
    details: FrozenJson             # es. livello del canale

class Candidate(Model):
    candidate_id: str               # sha256(event_id, config_hash)
    direction: Direction
    entry_ref: float                # riferimento, NON prezzo eseguibile
    entry_ref_basis: PriceBasis
    stop: float
    target: float
    available_at: AwareDatetime
    expires_at: AwareDatetime
    # invarianti: geometria coerente con la direzione, expires_at > available_at

class Abstention(Model):
    reason_code: ReasonCode
    detail: str                     # testo breve deterministico, non generato da LLM

class StrategyEvaluation(Model):
    evaluation_id: str
    engine_version: str
    strategy_id: str
    strategy_version: str
    config_hash: str
    symbol: str
    as_of: AwareDatetime
    snapshot_hash: str
    event: StructuralEvent | None
    outcome: Candidate | Abstention       # esattamente uno, mai None
    filters: tuple[FilterResult, ...]     # in ordine di valutazione
    measures: Mapping[str, float]         # es. atr14, channel_high, channel_low
    next_state: StrategyState

class Strategy(Protocol):
    strategy_id: ClassVar[str]
    strategy_version: ClassVar[str]
    params_model: ClassVar[type[Model]]
    def requirements(self, params) -> SnapshotRequirements: ...   # timeframe e lookback
    def evaluate(self, market_snapshot: MarketSnapshot,
                 strategy_config: StrategyConfig,
                 strategy_state: StrategyState) -> StrategyEvaluation: ...
```

## Audit e run (M1)

```python
class RunManifest(Model):
    run_id: str                       # casuale
    run_fingerprint: str              # sha256(dataset_ids, config_hash, engine_version,
                                      #        calendar_version, start, end)
    code_version: str                 # git sha
    datasets: tuple[DatasetRef, ...]
    config: StrategyConfig
    start: AwareDatetime; end: AwareDatetime
    status: Literal["RUNNING", "INTERRUPTED", "COMPLETED"]
    last_committed_as_of: AwareDatetime | None
    output_hash: str | None           # solo se COMPLETED
    evaluation_count: int
```

Il record di audit è la `StrategyEvaluation` serializzata in forma canonica più
`run_id`. La scrittura è idempotente per `(run_fingerprint, evaluation_id)`.

## Contratti futuri (solo nomi, dettagliati all'inizio della milestone)

- M2: `CostModel`, `ExecutionModel`, `SimulatedFill`, `Outcome` (con
  `provenance` e versione), `ExperimentSpec` (pre-registrazione), `MetricsReport`.
- M3: `AccountIdentity`, `SymbolSpec`, `RiskDecision`, `OrderIntent`,
  `OrderState`, `BrokerPosition`, `ReconciliationReport`, `KillSwitch`.
- M4: `ResearchProposal` (schema chiuso, convertibile in `StrategyConfig` +
  `ExperimentSpec`, senza campi di esecuzione).
