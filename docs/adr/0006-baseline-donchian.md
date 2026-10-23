# ADR-0006 — Baseline Donchian breakout H1

Stato: Proposta · Data: 2026-10-20 · Rivisto: 2026-10-23 (contracts-v0.1)

## Contesto
Serve una sola baseline molto semplice per verificare il motore di ricerca, non
per guadagnare. S1–S4 non vengono migrati. La v0 lasciava impliciti indici del
canale, freschezza del breakout, warm-up dell'ATR e comportamento su gap e chiusure.

## Decisione
`baseline_donchian_breakout` v1 su H1 BID derivato da M15, senza stato,
`research_only`. Definizione normativa in contracts §11:
- indici sugli slot del calendario (weekend e chiusure attese non sono gap);
- canale su `t−20 … t−1`, barra corrente esclusa; breakout fresco rispetto al
  canale della barra precedente (`t−21 … t−2`), disuguaglianza stretta;
- ATR14 come media semplice di 14 True Range (warm-up 15 slot), somma sequenziale;
- stop a 1.5 × ATR, target a 2R, scadenza alla fine dello slot successivo;
- sette gate in ordine fisso; qualsiasi slot non `VALID` nella finestra
  produce astensione; l'ultimo slot prima di una chiusura produce `MARKET_CLOSED`.

Parametri e regole congelati prima di qualunque risultato e non ottimizzati.

## Motivazione della modifica all'ATR
La v0 indicava Wilder. La ricorsione di Wilder ha memoria infinita: il valore a
`as_of` dipende dall'inizio del dataset e dalla regola di ripartenza dopo un gap,
e due dataset con inizio diverso darebbero valutazioni diverse per lo stesso
istante. La media semplice ha memoria finita. Soggetta ad approvazione (decisione H1).

## Conseguenze
- Esercita finestre, indicatori, gate dati, livelli, scadenza e astensioni.
- Qualunque filtro aggiunto in futuro va giustificato con ablation su dati non
  usati per sceglierlo, come nuova versione.
