# FxAgent Next

Piattaforma di ricerca quantitativa Forex, ricostruita da zero. Nessun trading
reale è abilitato e in questa fase non esiste alcun codice broker.

Stato: **M1 — data foundation + Strategy Engine, in attesa di approvazione.**

- [Specifica M0](docs/M0-specifica.md) · [Contratti](docs/contracts.md) · [ADR](docs/adr/README.md)
- [Report M1](docs/M1-report.md)

## Installazione (Windows o Linux)

```powershell
uv sync
uv run pytest
```

Richiede [uv](https://docs.astral.sh/uv/); Python 3.12 viene gestito da uv.

## Esempio riproducibile (offline, dati sintetici)

```powershell
uv run fxnext synthetic --datasets data/datasets --from 2024-01-01T00:00Z --to 2024-03-01T00:00Z
# -> dc80b3dbb3603610ba43a3b7d56234d08a858f84423bfb0afea160a9e98060c5
uv run fxnext replay --datasets data/datasets --runs runs `
    --dataset dc80b3dbb3603610ba43a3b7d56234d08a858f84423bfb0afea160a9e98060c5 `
    --from 2024-01-08T00:00Z --to 2024-02-27T00:00Z --code-version regression
# -> run_fingerprint 86683eaa216aa86def056e0b3d8b2cc73f5434b686d23e3c9a9669ee406a8d76
# -> output_hash     05941f857c57805cc0bcb470194b210471e5760f4e0f40120cb0e92651c0e15b (864 valutazioni)
uv run fxnext verify runs/86683eaa216aa86def056e0b3d8b2cc73f5434b686d23e3c9a9669ee406a8d76
uv run fxnext explain runs/86683eaa216aa86def056e0b3d8b2cc73f5434b686d23e3c9a9669ee406a8d76 EURUSD 2024-02-07T10:15Z
```

`--code-version regression` fissa la versione del codice per riprodurre gli hash
del documento; senza l'opzione viene usato il commit git corrente e un working
tree modificato viene rifiutato.

## Dati reali Dukascopy

```powershell
uv run fxnext download --symbol EURUSD --from 2024-01-01 --to 2024-03-31
uv run fxnext import --symbol EURUSD --from 2024-01-01 --to 2024-03-31
```

Il download è l'unico comando che usa la rete e va eseguito su una macchina che
raggiunge `datafeed.dukascopy.com`. I dati restano in `data/` (ignorata da git).
La sorgente limita le richieste (HTTP 429/503 frequenti): il downloader attende
`--min-interval` secondi tra una richiesta e l'altra (default 1), ritenta gli
errori transitori e, se un giorno resta `FAILED`, basta rilanciare lo stesso
comando: i giorni già scaricati vengono saltati.
