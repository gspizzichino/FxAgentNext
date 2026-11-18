# Costi veri del broker: Naga Markets demo (18 novembre 2026)

Lettura in sola lettura dal terminale MT5 sul PC di Giacomo, circa 07:49 UTC. Nessun ordine,
nessuna posizione toccata. File grezzi sul PC: `C:\Users\HP\FxAgentNext\data\mt5\costs_probe_2026-11-18.json`
e `spread_stats_2015_2023.json` (script in `~/mt5probe/costs_probe.py` e `spread_stats.py`).

Conto: NagaMarkets-Demo, leva 1:30, valuta EUR, margine retail hedging. Contratto 100.000,
lotto minimo 0,01. Commissioni: 0 su tutti gli 83 deal esistenti (conto "solo spread").
Swap in punti per lotto per notte, triplo il mercoledì.

## 1. Swap: il ricarico vero è più alto dell'1% usato nei test

Se lo swap del broker è (differenziale − ricarico) per il long e (−differenziale − ricarico)
per lo short, la somma long + short vale −2 × ricarico, indipendentemente dai tassi. Da qui il
ricarico per lato in % annua del nozionale (prezzi approssimati a novembre 2026, errore di
qualche %):

| Coppia | Swap long, % annuo | Swap short, % annuo | Ricarico per lato | Differenziale implicito |
|---|---|---|---|---|
| EURUSD | −3,35 | +0,47 | 1,44% | −1,91% |
| GBPUSD | −1,23 | −1,59 | 1,41% | +0,18% |
| USDJPY | +0,47 | −5,53 | 2,53% | +3,00% |
| USDCHF | −0,05 | −6,48 | 3,26% | +3,22% |
| AUDUSD | −2,25 | −0,56 | 1,40% | −0,84% |
| NZDUSD | −2,56 | +0,06 | 1,25% | −1,31% |
| USDCAD | +0,26 | −2,09 | 0,91% | +1,17% |
| USDNOK | −1,52 | −1,84 | 1,68% | +0,16% |
| USDSEK | −0,31 | −3,11 | 1,71% | +1,40% |
| EURGBP | −3,45 | +0,14 | 1,66% | −1,80% |
| EURJPY | −0,20 | −5,37 | 2,79% | +2,59% |
| EURCHF | +0,20 | −4,32 | 2,06% | +2,26% |

- Ricarico mediano circa **1,6% per lato**, da 0,9% (USDCAD) a 3,3% (USDCHF). Nei test abbiamo
  usato 1,0%: il broker vero costa di più.
- Esempio carry: long USDJPY incassa il +0,47% annuo su un differenziale di circa +3%. Il broker
  trattiene più dell'80% del premio.
- Conseguenza: con questo broker il carry G10 di M5 sarebbe andato peggio, non meglio. Nessun
  esito negativo dei test cambia con i costi veri.

## 2. Spread

Spread live (3 letture, in pip): EURUSD 0,5 (gruppo "Forex Gold", non confrontabile), GBPUSD 1,2,
USDJPY 0,8–0,9, USDCHF 1,6, AUDUSD 1,2, NZDUSD 1,6, USDCAD 2,0–2,2, USDNOK 16–18, USDSEK 16
(circa 1,7 bp), EURGBP 1,2, EURJPY 1,9–2,1, EURCHF 1,8. Nessuna commissione.

Storico dal CSV MT5 (solo colonna spread, 2015–2023, nessun prezzo, holdout non letto),
mediana in pip:

| Anno | GBPUSD | USDJPY | EURUSD |
|---|---|---|---|
| 2015–2017 | 1,2–1,3 | 0,9 | nessun dato |
| 2018 | 0,8 | 0,4 | nessun dato |
| 2019–2020 | 1,3–1,5 | 1,1–1,6 | inaffidabile (spread 0 nel 41% delle barre 2020) |
| 2021–2023 | 1,4–1,7 | 1,6–1,8 | 1,0–1,2 |

Intorno alla mezzanotte server (rollover) gli spread salgono del 30–80%.

Confronto con i test M2: lo spread Dukascopy più la commissione di 0,35 pip per lato valevano
circa 1,2–1,4 pip andata e ritorno. Naga, senza commissione, sta a 1,2–1,8 pip sulle coppie
principali, cioè fra il costo ×1 e il costo ×1,5 dei test. Lo stress ×1,5 era realistico.

## 3. Cosa cambia

- Intraday: costi simili a quelli dei test, al limite un po' più alti. Le conclusioni di M2 e M4
  restano.
- Strategie che tengono posizioni di notte: il costo che decide è il ricarico sullo swap, circa
  1,6% annuo per lato (fino al 3%). Una strategia lenta deve rendere al lordo almeno 2–3% annuo
  sul nozionale solo per pagare il mantenimento.
- I dati di costo per un nuovo modello (`cost-model` Naga) ci sono: spread per coppia e per ora,
  ricarico per coppia.

## Limiti

1. Conto demo e una sola lettura: un conto reale può avere condizioni diverse; gli swap cambiano
   con i tassi.
2. Il calcolo del ricarico assume un ricarico uguale su long e short; prezzi approssimati.
3. AUDJPY senza tick live (non in Market Watch, terminale non modificato).
4. EURUSD storico inaffidabile 2019–2021; margin_initial 100.000 da verificare.
5. Per tua informazione: nel log del download Dukascopy in background l'ultima riga è EURUSD ASK
   2026-05-13 FAILED (HTTP 503). Il log è stato solo letto.

## Nel codice (`broker-costs-v1`)

- Profilo: `src/fxnext/research/broker/naga-demo-2026-11-18.json`, controllato per sha256 al
  caricamento (`PROFILES`). Un file cambiato è un profilo nuovo. I prezzi medi
  `reference_mid_approx` sono approssimati a mano e servono solo a convertire i punti di swap
  in percentuale; con `--price` (o `prices=`) si usano prezzi veri.
- `fxnext broker-costs [--price USDJPY=154.2 ...]` stampa la tabella di questo documento.
- G10: versione costi `g10-cost-naga-v1`, solo per una pre-registrazione nuova. Ricarico per
  valuta (`markup_pct_by_currency`, dal profilo), commissione da mettere a 0, spread a zero
  sostituito come in v2, stress ×1,5/×2 applicato anche a spread e ricarico. Il report registra
  nome e sha256 del profilo. Le pre-registrazioni congelate (`g10-cost-v1`, `v2`) non cambiano.
- Intraday: `spread_pips(coppia, ora_server)` dà la mediana storica Naga per ora (EURUSD,
  GBPUSD, USDJPY) o la lettura live. Il simulatore M2 resta su `cost-model-v1`; collegarlo al
  profilo richiede una nuova versione del modello costi.
