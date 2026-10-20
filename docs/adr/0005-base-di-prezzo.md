# ADR-0005 — Base di prezzo esplicita e nessuna quota fabbricata

Stato: Proposta · Data: 2026-10-20

## Contesto
Il backtest del vecchio FxAgent costruiva bid/ask sintetici da un midpoint con
spread costante. La specifica vieta di fabbricare quote storiche reali quando
esiste solo OHLC.

## Decisione
Ogni serie dichiara `price_basis` (BID, ASK, MID). Ogni quota dichiara
`provenance` (OBSERVED, MODELLED). Se esiste solo BID, lo spread viene applicato
dal simulatore tramite un modello versionato e i risultati sono marcati
`MODELLED`. Il core non mescola basi diverse in uno stesso calcolo.

## Conseguenze
- Risultati con spread osservato e modellato non vengono aggregati insieme.
- Le metriche riportano sempre la provenienza dei costi.
