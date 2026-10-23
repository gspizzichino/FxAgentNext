# ADR-0005 — Base di prezzo esplicita, stati di barra distinti, nessuna quota fabbricata

Stato: Proposta · Data: 2026-10-20 · Rivisto: 2026-10-23 (contracts-v0.1)

## Contesto
Il backtest del vecchio FxAgent costruiva bid/ask sintetici da un midpoint con
spread costante. La specifica vieta di fabbricare quote storiche reali quando
esiste solo OHLC. La v0 trattava le barre piatte a volume zero con un flag
`NO_TRADE` escluso dagli indicatori, confondendolo di fatto con un dato mancante.

## Decisione
- Ogni serie dichiara `price_basis` (BID, ASK, MID). Ogni quota o prezzo di
  esecuzione dichiara `provenance` (OBSERVED, MODELLED). Il core non mescola basi.
- I livelli di un candidato (`entry_ref`, `stop`, `target`) dichiarano
  `level_basis`; per la baseline è BID. I prezzi eseguibili e la traduzione nei
  trigger broker sono del modello di esecuzione (M2) o del broker (M3), registrati
  a parte; i livelli del candidato non si sovrascrivono.
- Stati distinti: `VALID`, `FLAT_ZERO_VOLUME`, `INVALID` (riga nel file
  `rejected`), `UNAVAILABLE`, `BEFORE_COVERAGE`; mercato chiuso = nessuno slot;
  sintetico = provenienza del dataset, mai mescolata con dati reali.
- Barra piatta a volume zero in sessione: conservata, non valida per gli
  indicatori, causa astensione `FLAT_BAR_IN_WINDOW`. Fuori sessione:
  `CLOSED_FILLER` contato nel report di qualità.

## Conseguenze
- Risultati con spread osservato e modellato non vengono aggregati insieme.
- Nessuna situazione di qualità dei dati è silenziosa: ognuna ha uno stato e un motivo.
