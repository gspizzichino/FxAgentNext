# FxAgent Next

Piattaforma di ricerca quantitativa Forex, ricostruita da zero. Nessun trading
reale è abilitato e in questa fase non esiste alcun codice broker.

Stato: **M2 chiuso il 2026-11-17: baseline Donchian respinta in validazione (EURUSD, GBPUSD REJECTED; USDJPY INCONCLUSIVE). Holdout sigillato.**

- [Specifica M0](docs/M0-specifica.md) · [Contratti](docs/contracts.md) · [ADR](docs/adr/README.md)
- [Report M1](docs/M1-report.md) · [Specifica M2](docs/M2-specifica.md) · [Report M2](docs/M2-report.md)

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

`import` scrive i dataset M15 BID e ASK in `data/datasets/` e il percorso M1
BID/ASK per il simulatore in `data/paths/`. Stampa gli identificativi da usare
nella pre-registrazione.

## Esperimenti (M2)

Ogni esperimento è una pre-registrazione TOML congelata prima di guardare i
risultati. Gli split sono fissati nel codice: sviluppo 2015–2020, validazione
2021–2023, holdout 2024 → ottobre 2026.

```toml
experiment_id = "baseline-donchian-v1"
hypothesis = "Il breakout Donchian H1 congelato ha expectancy netta positiva"
split = "validation"            # development | validation | holdout

[[data]]
symbol = "EURUSD"
m15_bid_dataset = "<dataset_id BID stampato da import>"
path = "<PATH stampato da import>"
```

```powershell
uv run fxnext research prereg.toml          # replay + simulazione + benchmark + report
uv run fxnext research prereg.toml --grid   # griglia informativa, solo sviluppo
uv run fxnext research-summary research/<fp1> research/<fp2> --out research/summary-dev
uv run fxnext hypothesis prereg/s1-failure-dev-001.toml   # ipotesi S0-S3, S7 (solo sviluppo)
```

- **Ipotesi S0–S3 e S7**: le pre-registrazioni sono in `prereg/`, generate dal
  testo congelato l'11/11/2026. Leggono i percorsi M1 dei report dev e girano
  solo sullo split di sviluppo. Dettagli e scelte di lettura del testo in
  [docs/M4-ipotesi.md](docs/M4-ipotesi.md).

- **Confini**: ogni esperimento usa solo prezzi del proprio split; i trade
  ancora aperti alla fine sono `END_OF_DATA` ed esclusi. Per validazione e
  holdout importa i dati partendo un mese prima dello split (warm-up).

- **Risultato**: per ogni esperimento il comando crea `research/<fingerprint>/`,
  che contiene:
  - `report.md` e `report.json` con metriche, benchmark, copertura ed esito dei
    criteri;
  - `outcomes.parquet` con un esito simulato per candidato e variante.
- **Holdout**: va aperto con `split = "holdout"` e `holdout = true`. Ogni
  apertura resta registrata in `research/holdout_log.jsonl`; per una seconda
  apertura serve `--reopen-holdout`.
- **Interpretazione**: tutti i rendimenti sono `MODELLED` e nessun report
  dimostra un edge da solo.

## Carry e momentum G10 (M5, solo sviluppo)

Pre-registrazione `prereg/g10-cm-dev-001.toml`, generata dal testo congelato il 2026-11-17.
Scelte di dettaglio in `docs/M5-g10.md`.

```bash
uv run fxnext g10-download --to-year 2013        # candele giornaliere BID+ASK 2003-2013, 9 coppie
uv run fxnext g10-rates                          # tassi OCSE 3 mesi da FRED, CSV con hash
uv run fxnext g10-check prereg/g10-cm-dev-001.toml   # controllo dei dati, nessun esito
uv run fxnext g10-run prereg/g10-cm-dev-001.toml     # 7 varianti, casuali, criteri
```

## App web (prime schermate)

```powershell
uv run fxnext dashboard                      # http://127.0.0.1:8765
uv run fxnext dashboard --frozen <cartella>  # verifica anche i documenti congelati (*.sha256)
```

L'app gira sul PC e legge soltanto: `research/` (report, riepiloghi, esiti),
`data/prereg/` (sha256 ricalcolato a ogni apertura), `data/datasets/` (manifest) e
i registri del downloader in `data/raw/`. Non avvia run né download, non ha rotte
che scrivono e non mostra mai i numeri dell'holdout. Le schermate operative
(Oggi, Posizioni, Segnali, Proposte AI) sono segnaposto fino a M3/M4.

Dal telefono: installa Tailscale su PC e telefono e avvia con
`--host <IP Tailscale del PC>`; non usare `0.0.0.0` su reti che non controlli.

Avvisi Telegram: crea un bot con @BotFather, imposta `FXNEXT_TELEGRAM_TOKEN` e
`FXNEXT_TELEGRAM_CHAT_ID` sul PC e prova con `uv run fxnext notify-test`.

## Archivio news e sentiment (preparazione M4)

```bash
uv run fxnext news-archive --out data/news   # uno snapshot di calendario, COT e sentiment retail
```

Ogni esecuzione aggiunge la risposta grezza di ciascuna fonte, con l'ora UTC di
ricezione, a `data/news/<fonte>/<anno>/<giorno>.jsonl`; non riscrive mai nulla e
salta i record identici al precedente. Fonti: calendario settimanale Forex
Factory (feed pubblico non ufficiale), COT CFTC (Traders in Financial Futures per
EUR, GBP, JPY), Myfxbook Community Outlook. Il sentiment richiede
`FXNEXT_MYFXBOOK_EMAIL` e `FXNEXT_MYFXBOOK_PASSWORD` nell'ambiente; le
credenziali non vengono salvate. Motore e ricerca non importano questo modulo
(import-linter): i dati entrano in una strategia solo con una pre-registrazione.

Esecuzione automatica ogni 30 minuti su Windows (MSYS2): `scripts/news-archive.sh`
da Utilità di pianificazione; il registro è in `data/news/archive.log`.
