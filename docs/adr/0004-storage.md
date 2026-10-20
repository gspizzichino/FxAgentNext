# ADR-0004 — Parquet/DuckDB per dataset e audit, SQLite per lo stato operativo

Stato: Proposta · Data: 2026-10-20

## Contesto
Il vecchio FxAgent usava un unico SQLite con 34 tabelle per eventi, trading,
ricerca e governance, con retention e archivi mensili aggiunti dopo.

## Decisione
- Dataset di mercato: Parquet immutabili partizionati, con manifest JSON e
  `dataset_id` = hash del contenuto canonico.
- Audit delle valutazioni e risultati di ricerca: Parquet append-only per run,
  interrogati con DuckDB (embedded, nessun server).
- Stato operativo (da M3): SQLite in WAL, poche tabelle esplicite (intenti,
  posizioni osservate, binding account, kill switch, heartbeat). Transazioni
  brevi; il broker resta la verità sulle posizioni.
- Il DB del vecchio FxAgent non viene mai aperto.

## Conseguenze
- Analisi colonnari veloci e riproducibili; stato operativo piccolo e leggibile.
- Due tecnologie di storage con confini netti.
