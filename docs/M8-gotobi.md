# M8: gotobi USDJPY (e1-gotobi-001)

Origine: esplorazione del 23/11/2026 su dati già usati (copia in `/mnt/project-files/m8/`).
Testo congelato: `docs/M8-prereg-e1-gotobi-001-congelato.md` (sha256 `64bf779a…`, uguale alla
copia nella cartella del progetto). Una sola esecuzione, nessuna fase di sviluppo.

## Come gira (sul PC)

```bash
cd ~/FxAgentNext-hyp
# 1. dati: USDJPY M1 2008-2014 (cache separata) e 2024-2026 (holdout, cache del download principale)
uv run fxnext import --cache data/raw-m1-2008 --symbol USDJPY --from 2008-01-01 --to 2015-01-01
uv run fxnext import --cache <cache holdout> --symbol USDJPY --from 2024-01-01 --to 2026-11-01
# 2. scrivere i due path id in prereg/e1-gotobi-001.toml (paths = [...])
# 3. controllo dati: copertura e spread, nessun rendimento
uv run fxnext gotobi prereg/e1-gotobi-001.toml --check
# 4. un solo run (rifiuta una seconda esecuzione)
uv run fxnext gotobi prereg/e1-gotobi-001.toml
```

## Letture del testo congelato

1. **Esecuzione**: fill delle regole M2 applicati direttamente (acquisto all'ASK di chiusura del
   minuto 22:59 UTC, vendita al BID di chiusura del minuto 00:54 UTC), senza passare dal
   simulatore: con un ingresso e un'uscita a orario fisso, senza stop, il risultato è lo stesso.
2. **Minuti**: il minuto d'ingresso deve avere tick (VALID); quello d'uscita può essere FLAT, come
   in M2. Un minuto assente esclude il giorno.
3. **Costo Naga**: spread a/r = metà spread in entrata + metà in uscita, con un minimo di 0,9 pip,
   più 0,1 pip di slippage per lato. Stress ×1,5 e ×2 sul costo totale.
4. **Benchmark**: 10.000 estrazioni senza ripetizione dai giorni di controllo validi, tante quanti
   i gotobi validi.
5. **Finestre**: il codice rifiuta path fuori da 2008-01-01 → 2015-01-01 e 2024-01-01 → 2026-11-01.
