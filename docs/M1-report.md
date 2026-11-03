# M1 — Data foundation + Strategy Engine: report

Data: 2026-11-03 · Contratti: `contracts-v0.2.1` · Stato: **in attesa di approvazione**

Nessun codice MT5, risk, execution, simulatore o AI. Nessun ordine. Vecchio
FxAgent non toccato.

## 1. Decisioni applicate

- H1 (ATR14 media semplice) e H5 (`code_version` nel fingerprint): approvate da Giacomo.
- H2 (barra piatta in sessione invalida la finestra), H3 (ritardi 0 s in replay),
  H4 (`MARKET_CLOSED` sull'ultimo slot prima di una chiusura): **applicate secondo la
  mia raccomandazione**, in attesa di conferma.
- D2: downloader Dukascopy (ADR-0015).

## 2. Cosa è stato implementato

| Modulo | File | Contenuto |
|---|---|---|
| `domain` | `frozen.py`, `model.py`, `hashing.py`, `enums.py`, `timeutil.py`, `calendar.py`, `series.py`, `contracts.py` | Tipi immutabili, `FrozenMap`/`FrozenJson`, hash `fxnext-hash-v1`, calendario FX con DST USA calcolato aritmeticamente (nessun database di fusi), `BarSeries` su buffer `bytes` read-only, contratti Pydantic con invarianti. |
| `strategy` | `base.py`, `donchian.py` | Protocollo `Strategy`, baseline `baseline_donchian_breakout` v1 come in contracts §11, `research_only`. |
| `data` | `bi5.py`, `dukascopy.py`, `rows.py`, `importers.py`, `store.py`, `snapshot.py`, `synthetic.py` | Codec `.bi5`, downloader con cache immutabile e log, import M1 → M15 e CSV generico, dataset Parquet con `dataset_id` verificato a ogni lettura, snapshot point-in-time con H1/H4 derivate, generatore sintetico deterministico. |
| `replay` | `runner.py` | Scheduler guidato dal calendario, `as_of = C_k + evaluation_delay`, classificazione `dispatch`, ripresa. |
| `audit` | `lock.py`, `store.py`, `explain.py` | Lock del sistema operativo, segmenti Parquet + manifest come unico commit, quarantena, verifica a catena, `explain` con annotazione ex post. |
| CLI | `cli.py` | `download`, `import`, `synthetic`, `inspect`, `replay`, `verify`, `explain`. |

Tooling: uv con `uv.lock`, ruff, mypy `--strict`, import-linter (3 contratti di
dipendenza), pytest + hypothesis, CI GitHub Actions su Ubuntu e Windows.

## 3. Risultati dei controlli (Linux, in questo ambiente)

| Controllo | Esito |
|---|---|
| `pytest` | **62 test passati** |
| `ruff check` / `ruff format --check` | puliti |
| `mypy --strict` | nessun errore (28 file) |
| `lint-imports` | 3 contratti rispettati: `domain` non importa altri moduli, `strategy` solo `domain`, nessun I/O/rete/storage nel core |

Copertura dei criteri di accettazione:

| Criterio | Test |
|---|---|
| A1 determinismo | `test_regression.py` (hash committati), T4.x |
| A2 nessun dato futuro | T1.1–T1.6 (incluso property test di perturbazione del futuro), T2.1 |
| A3 gap e dati invalidi | T6.1–T6.4, T7.4, `test_every_evaluation_has_exactly_one_outcome` |
| A4 esito unico motivato | invarianti del modello + test su tutte le valutazioni di un run |
| A5 ripresa dopo restart | T5.1 (crash in 5 punti + kill del processo), T5.2, T5.3 |
| A6 audit interrogabile | T2.1, T5.6, esempio sotto |
| A7 suite e regressione | CI configurata; vedi limite 2 |
| T8 downloader | T8.1–T8.10 contro un server HTTP locale |

Il numero di test non è un criterio: conta che ogni criterio abbia un test con esito esatto.

## 4. Esempio riproducibile

Vedi [README](../README.md). Su dati sintetici (seed 7, EURUSD, 8 gen – 27 feb 2024):

- `dataset_id` `dc80b3db…98060c5`
- `run_fingerprint` `86683eaa…a8d76`, `output_hash` `05941f85…0e15b`, 864 valutazioni.

`fxnext explain … EURUSD 2024-02-07T10:15Z` restituisce la valutazione delle 10:00
(slot 09:00–10:00): tutti i gate superati, canale 1.10481–1.10532, ATR 0.000226,
chiusura 1.10473 sotto il canale → candidato SHORT. I risultati su dati sintetici
non dicono nulla sulla strategia: servono solo a verificare il motore.

Prestazioni (informative, NFR-8): un anno H1 di una coppia (6.240 valutazioni) in
circa 12 s qui; 10 anni stimati intorno ai 2 minuti.

## 5. Limiti rimasti

1. **Formato Dukascopy non verificato su un file reale.** Questo ambiente non
   raggiunge `datafeed.dukascopy.com`. Parser ed encoder dei test condividono la
   stessa ipotesi sul formato (ordine dei campi, divisore dei prezzi). Prima di
   usare dati reali serve un download di prova dal tuo PC e il confronto con un
   grafico Dukascopy di riferimento.
2. **CI Windows non ancora eseguita**: richiede il push. Fino ad allora la parità
   degli hash Linux/Windows (T4.6) e il lock `msvcrt` non sono verificati.
3. Calendario v1: solo chiusure 25 dicembre e 1 gennaio. Dati reali in quei giorni
   vengono scartati come `out_of_session` (contati nel report di qualità).
4. Un file Parquet per dataset, senza partizione per anno (sufficiente ai volumi
   attuali).
5. Le righe rifiutate usano sempre disponibilità modellata (`close_time +
   publication_delay`).
6. Il property test di perturbazione del futuro gira con 15 esempi per contenere
   i tempi.
7. Correzione alla specifica: in T2.1 la v0.1 diceva che arrivo tardivo e dato
   mancante danno lo stesso `evaluation_hash`; in realtà differiscono perché lo
   snapshot cita il `dataset_id`. Serie, esito e filtri sono identici.

## 6. Prossimi passi proposti

1. Push e CI su Windows.
2. Download di prova di pochi giorni di EURUSD dal tuo PC e verifica del parser.
3. Conferma di H2, H3, H4.
4. Approvazione di M1 prima di iniziare M2 (simulatore, costi, metriche, walk-forward).
