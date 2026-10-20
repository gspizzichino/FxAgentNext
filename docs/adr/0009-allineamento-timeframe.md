# ADR-0009 — Timeframe superiori derivati da M15 e allineati alla chiusura di New York

Stato: Proposta · Data: 2026-10-20

## Contesto
Barre H1/H4 parziali o allineate in modo diverso dal broker producono segnali
diversi. Il vecchio Strategy Lab scartava già i bucket HTF parziali.

## Decisione
H1 e H4 sono derivati da M15 solo con copertura completa (4 e 16 barre). H4 è
allineato ai confini 17, 21, 01, 05, 09, 13 ora di New York, coerente con la
chiusura giornaliera FX e con i server MT5 a UTC+2/+3. Le barre derivate
portano il riferimento alla sorgente.

## Conseguenze
- Una sola sorgente di verità per timeframe, gap tracciabili.
- In M3 si misura la differenza rispetto alle barre native del broker.
- Alternativa scartata: allineamento a mezzanotte UTC, diverso dalla maggior
  parte dei broker e dalla chiusura FX.
