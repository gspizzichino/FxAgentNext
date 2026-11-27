# M11: H1 `h1-cbdrift-001`, deriva prima delle decisioni delle banche centrali

Testo congelato: `docs/M11-prereg-h1-cbdrift-001-congelato.md` (sha256 `b1bb6179…`, congelato il
27/11/2026 alle 08:41 UTC con l'ok di Giacomo; copia identica nei file di progetto, cartella `m6`).
TOML: `prereg/h1-cbdrift-001.toml`. Codice: `src/fxnext/research/cbdrift/`. Test:
`tests/test_t21_h1_cbdrift.py`.

## Comandi (dal PC)

I dati sono già sul PC: tassi in `data/h1-rates/raw/` (sha256 in `data/h1-rates/manifest.csv`),
calendario in `data/h1-rates/calendar/meetings.csv`, candele giornaliere G10 2003–2013 in
`~/FxAgentNext-hyp/data/raw-d1`. Le letture dei file `.xls`/`.xlsx` richiedono l'extra `h1`
(`uv sync --extra h1`, cioè `openpyxl` e `xlrd`).

```bash
fxnext h1-check prereg/h1-cbdrift-001.toml --cache ~/FxAgentNext-hyp/data/raw-d1 \
    --rates ~/FxAgentNext/data/h1-rates/raw --calendar ~/FxAgentNext/data/h1-rates/calendar/meetings.csv
fxnext h1-run   prereg/h1-cbdrift-001.toml   # stessi argomenti; un secondo run restituisce lo stesso fingerprint
```

`h1-check` riporta copertura dei tassi, valute ammesse, calendario per banca e anno, giorni non
validi e spread a zero per coppia, **senza calcolare alcun rendimento** (testo congelato §8).

La validazione 2014–2026 non è eseguibile con questo TOML: serve un file nuovo con
`split = "validation"` e l'approvazione di Giacomo scritta in `validation_approval`, e prima il
download delle candele giornaliere 2014–2026 (oggi non sul disco).

## Letture del testo congelato (scelte di codice, fissate prima di qualsiasi dato di prezzo)

1. **Giorno feriale valido.** Per una banca non USA: giorno feriale con candela valida della sua
   coppia (come M5). Per la Fed: giorno valido per tutte le coppie del paniere.
2. **S, ingresso, uscita.** Il 12°, l'11° e il 1° giorno valido prima di T. T è la data locale
   dell'annuncio; per la RBNZ la data locale − 1 giorno (§3.1).
3. **Appartenenza allo split.** Un evento conta se l'ingresso è dopo l'inizio e l'uscita prima
   della fine dello split. Se fra la fine dello split e T c'è un giorno feriale, l'uscita potrebbe
   cadere lì (giorni non caricati): l'evento si esclude come `exit_after_split`.
4. **Tassi sulla griglia dei giorni feriali** (lun–ven), in percento, letti dal 1/11/2002 per il
   riscaldamento di 250 + 5 giorni prima del primo segnale del 2004 (§1.3). Ogni valore mancante si
   riempie con l'ultimo noto per al massimo 5 giorni feriali; oltre resta mancante.
5. **Spread grezzo** = media dei 5 giorni feriali fino a S (compreso) del tasso a 1 mese meno media
   dell'overnight. **Mediana** sui valori giornalieri (non riempiti) di tasso a 1 mese meno
   overnight nei 250 giorni feriali prima di S; servono almeno 225 valori (90%), altrimenti nessun
   segnale (`rates_missing_in_median_window`).
6. **d** = giorni di calendario da S alla data locale dell'annuncio. Con d ≥ 30 nessun segnale.
7. **Ammissione** (§1.3): copertura dei valori pubblicati (non riempiti) sui giorni feriali dello
   split. NOK non ha una serie a 1 mese: esclusa. NZD nello sviluppo non passa (bank bill dal
   2009). Il paniere della Fed è fatto delle valute ammesse nel run; CHF resta nel paniere anche
   durante il cambio minimo (l'esclusione del §1.1 riguarda le riunioni SNB).
8. **Carry** = (tasso a 1 mese della valuta − T-bill USD 4 settimane) al giorno d'ingresso ×
   giorni di calendario / 360, con il segno della posizione.
9. **Spread**: mezzo spread relativo all'ingresso e all'uscita. Spread a zero: mediana degli spread
   relativi non nulli della coppia su tutti i giorni validi dello split.
10. **Slittamento** 0,1 pip per lato, relativo al prezzo medio di quel giorno.
11. **Ricarico** 1,6% annuo per lato × notti / 365; notti = giorni feriali da ingresso (compreso) a
    uscita (escluso), il mercoledì vale 3 (una settimana = 7 notti).
12. **Stress** ×1,5 e ×2 su spread, slittamento e ricarico insieme.
13. **Bootstrap** dell'IC: si ricampionano i mesi di calendario (mese del giorno d'ingresso) con
    reinserimento; la media è il totale dei rendimenti diviso il numero di eventi del campione.
14. **Direzione casuale**: segno ±1 estratto per ogni evento negoziato, stessi costi.
15. **Sottoperiodi** per anno del giorno d'ingresso; contano quelli con almeno 10 eventi.
