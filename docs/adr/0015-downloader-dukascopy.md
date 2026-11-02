# ADR-0015 — Downloader Dukascopy con cache grezza immutabile

Stato: Proposta · Data: 2026-11-02 · Decisione D2 di Giacomo

## Contesto
Serve una fonte storica con BID e ASK reali. In M0 avevo proposto l'import di
file forniti a mano; Giacomo ha scelto di includere un downloader in M1. Il
vecchio FxAgent usava il pacchetto `duka` invocato come processo esterno.

## Decisione
- Fonte: datafeed pubblico Dukascopy, candele **M1 giornaliere** BID e ASK
  (`{BID|ASK}_candles_min_1.bi5`, LZMA). Niente tick in M1: le candele M1 bastano
  per costruire M15 e pesano molto meno.
- Implementazione con sola libreria standard (`urllib`, `lzma`); nessun pacchetto
  esterno di download.
- Due passi separati: `fxnext data download` (unico comando che usa la rete)
  scrive una **cache grezza immutabile** e un log JSONL append-only per giorno;
  `fxnext data import` lavora solo sulla cache, offline e deterministico.
- File in cache con hash nel nome, scritti con `.tmp` + `os.replace`, mai
  sovrascritti; un riscaricamento diverso è conservato accanto e segnalato
  `SOURCE_CHANGED`.
- GET è idempotente, quindi retry con backoff esponenziale solo su timeout e
  5xx, con limite. 200 vuoto = `EMPTY` (nessun dato), 404 = `NOT_FOUND`, corpo
  non decomprimibile = `CORRUPT`; niente di questo crea barre.
- Download sequenziale (concorrenza configurabile, default 1) per non
  sovraccaricare la sorgente; ripresa saltando i giorni già `OK` in cache.
- Non si scaricano giorni non ancora interamente trascorsi.
- Aggregazione M1 → M15 e controlli di coerenza come in contracts §13.
- I dati reali non entrano nel repository. I test usano file `.bi5` generati dai
  test e un server HTTP locale; un test reale opzionale si esegue a mano dove la
  rete lo permette.

## Conseguenze
- Riproducibilità: un dataset è ricostruibile offline dalla cache e dal log.
- Il downloader dipende da un formato non documentato ufficialmente: il parser ha
  una `parser_version` e va confermato su un file reale prima di `contracts-v1`.
- Il download va eseguito su una macchina che raggiunge
  `datafeed.dukascopy.com` (il PC di Giacomo, o l'ambiente cloud se l'host viene
  consentito).
