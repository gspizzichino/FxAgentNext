# ADR-0011 — Valutazione guidata dal calendario, non dalla presenza dei dati

Stato: Proposta · Data: 2026-10-20 · Rivisto: 2026-10-23 (contracts-v0.1)

## Contesto
Un loop che itera sulle barre presenti salta in silenzio i buchi nei dati. La v0
non distingueva chiusura attesa, disponibilità, istante di valutazione e ritardo.

## Decisione
- Per ogni slot k del timeframe di segnale in sessione: chiusura attesa `C_k`,
  disponibilità `A_k`, istante di valutazione `T_k = C_k + evaluation_delay`
  (nominale, anche in runtime), istante reale `W_k` (solo runtime).
- Una sola richiesta per `(strategia, config, simbolo, C_k)`, nessun retry.
- A `T_k` dato mancante e dato non ancora arrivato sono la stessa situazione
  (`UNAVAILABLE` → `BAR_NOT_AVAILABLE` o `DATA_GAP`); la distinzione è solo un'
  annotazione ex post fuori dall'hash.
- Runtime: oltre `max_dispatch_lateness` la valutazione è registrata ma non
  inoltrata (`NOT_DISPATCHED_LATE`); slot persi per downtime `MISSED_DOWNTIME`.
- Fuori sessione non si valuta; `explain` risponde con il motivo di calendario.

## Conseguenze
- Replay e runtime producono le stesse valutazioni a parità di osservazioni registrate.
- Copertura misurabile come valutazioni con dati / valutazioni attese.
- Calendario e chiusure attese sono dati versionati.
