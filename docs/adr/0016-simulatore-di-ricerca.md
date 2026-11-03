# ADR-0016 — Simulatore di ricerca su percorso M1 BID/ASK

Stato: Accettata (2026-11-03, specifica M2 approvata, decisioni E1–E8)

## Contesto

M2 deve stimare cosa sarebbe successo dopo ogni candidato, con costi espliciti e
senza assunzioni ottimistiche nascoste. Le barre M15 non dicono in che ordine
sono stati toccati stop e target, e il BID da solo non dice quanto spread paga uno
SHORT in uscita. Il simulatore, per costruzione, legge il futuro rispetto ad
`as_of`.

## Decisione

- Il percorso dei prezzi è un dataset separato `m1-path-v1` (minuti in sessione
  con BID e ASK osservati entrambi), con identità `path_id` calcolata sul
  contenuto. La strategia non lo vede mai.
- Il modulo `fxnext.research` è l'unico che usa i percorsi. import-linter vieta
  che `domain`, `strategy`, `data`, `replay` o `audit` importino `research`.
- Il modello di esecuzione (`exec-model-v1`) e quello dei costi (`cost-model-v1`)
  sono parametri versionati, e il loro hash entra nel fingerprint dell'esperimento.
- Le regole conservative sono queste: stop per primo nel minuto ambiguo, target
  in gap uscito al livello, stop in gap all'apertura. È tenuta al massimo una
  posizione per simbolo, e gli altri candidati finiscono nel controfattuale.
- Gli esperimenti sono pre-registrati in TOML. Gli split sono fissati nel codice
  e l'holdout ha un registro append-only delle aperture.
- Il risultato di un esperimento è una directory scritta atomicamente sotto il
  suo fingerprint. Il replay sottostante usa l'audit crash-safe di ADR-0014.

## Conseguenze

- I risultati sono riproducibili e confrontabili tra varianti (costi ×1, ×1,5,
  ×2; target per primo).
- Il costo è un secondo import (M1) e circa 4–5 milioni di righe per coppia su 12
  anni, cioè qualche centinaio di MB in memoria durante un esperimento.
- Spread e prezzi restano quelli di Dukascopy. Il confronto con il broker reale
  arriva in M3 con lo stream `broker`.
