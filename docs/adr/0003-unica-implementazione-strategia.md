# ADR-0003 — Una sola implementazione della strategia e contratto `evaluate`

Stato: Proposta · Data: 2026-10-20

## Contesto
Nel vecchio FxAgent detector legacy, Strategy Lab e backtest avevano logiche
proprie (per esempio l'uscita su barra nel backtest), con rischio di divergenza
tra ciò che si misura e ciò che si esegue.

## Decisione
`evaluate(market_snapshot, strategy_config, strategy_state) -> StrategyEvaluation`
è l'unico punto in cui vive la logica di segnale. Replay, backtest, paper e
runtime MT5 lo chiamano identico. L'output contiene esattamente un `Candidate`
oppure una `Abstention` con `reason_code`, la lista completa dei filtri con
misura e soglia, e `next_state`. Le strategie sono preferibilmente senza stato;
se servisse stato, è un valore immutabile serializzabile e ricostruibile dal replay.
Il simulatore (M2) è un modulo separato che consuma candidati, non li produce.

## Conseguenze
- Test di parità replay/runtime in M2.
- Replay evento per evento, più lento di un backtest vettoriale.
