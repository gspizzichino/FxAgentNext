# ADR-0010 — Identità basate su hash e versioning dei contratti

Stato: **Sostituita da [ADR-0013](0013-identita-e-hashing.md)** · Data: 2026-10-20 · Sostituita: 2026-10-23

> Motivo: l'arrotondamento dei float alla precisione dello strumento poteva rendere
> indistinguibili dati materialmente diversi e non separava le identità di
> richiesta, evento, candidato e contenuto.

## Contesto
Nel vecchio FxAgent le versioni di outcome e funnel sono state cambiate dopo
aver prodotto dati, lasciando righe non confrontabili.

## Decisione
Dataset, config, snapshot, eventi, valutazioni e run hanno identificativi
derivati dal contenuto canonico (JSON ordinato, float arrotondati alla
precisione dello strumento, UTC). Strategie, contratti, calendario, validatore e
simulatore hanno versioni esplicite incluse negli hash. Un cambiamento di
semantica richiede una nuova versione; i dati esistenti non vengono mai reinterpretati.

## Conseguenze
- Regressione per hash possibile e cross-platform.
- Serve una funzione di serializzazione canonica unica, testata.
