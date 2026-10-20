# ADR-0006 — Baseline Donchian breakout H1

Stato: Proposta · Data: 2026-10-20

## Contesto
Serve una sola baseline molto semplice per verificare il motore di ricerca, non
per guadagnare. S1–S4 non vengono migrati.

## Decisione
`baseline_donchian_breakout` v1 su H1 derivato da M15: evento fresco di chiusura
oltre il massimo/minimo delle 20 barre precedenti; stop a 1.5 × ATR14; target a
2R; scadenza una barra; nessun filtro discrezionale. Parametri fissati prima di
qualunque risultato e non ottimizzati. La strategia non è abilitabile al trading
da configurazione: il registry la marca `research_only`.

## Conseguenze
- Esercita finestre, indicatori, gate dati, livelli, scadenza e astensioni.
- Qualunque filtro aggiunto in futuro va giustificato con ablation su dati non
  usati per sceglierlo, come nuova versione.
