# Pre-registrazione E1 gotobi USDJPY (CONGELATA 2026-11-23)

ID proposto: `e1-gotobi-001`. Nasce dall'esplorazione del 23/11/2026
(`m8/esplorazione-dati-usati-2026-11-23.md`, piano sha256 `79045ac6…`), che ha già usato
USDJPY 2015–2023. Quindi **non c'è una fase di sviluppo**: questo è un solo test di conferma, su
dati che nessuno ha guardato. Scrivendo questa bozza non è stato aperto nessun dato nuovo.
Congelata il 23/11/2026 alle 10:12 UTC con la scelta di Giacomo. Ogni modifica alle regole è una nuova pre-registrazione con un nuovo ID.

## 1. Ipotesi

Nei giorni gotobi gli importatori giapponesi comprano dollari al fixing di Tokyo delle 9:55 (ora
di Tokyo). Il flusso è meccanico e prevedibile, quindi USDJPY tende a salire fra l'apertura di
Tokyo e il fixing. Ipotesi nulla: in quella finestra, nei giorni gotobi, il rendimento netto medio
di un acquisto di USDJPY è ≤ 0.

Fonti: Ito e Yamada, "Puzzles in the Forex Tokyo 'Fixing': Order Imbalances and Biased Pricing
by Banks", NBER Working Paper 22820 ([pagina](https://www.nber.org/papers/w22820)), sullo
squilibrio di ordini e sul movimento di USDJPY prima del fixing di Tokyo; lavoro su arXiv
2301.13204 sull'anomalia gotobi ([pagina](https://arxiv.org/abs/2301.13204)). Trovati il
23/11/2026 con una ricerca web: ho letto solo titoli e pagine indice, non i risultati numerici. Nell'esplorazione: +1,85 bp lordi (2015–2020), +1,77 (2021–2023),
positivo in 9 anni su 9.

## 2. Dati

- USDJPY M1 BID+ASK Dukascopy, importato con hash come i percorsi M1 esistenti.
- **Campione di conferma (deciso da Giacomo il 23/11/2026 10:10):** 2008-01-01 → 2015-01-01
  insieme a 2024-01-01 → 2026-11-01, giudicati come un unico campione. Il 2008–2014 non è mai stato scaricato al minuto (vergine come
  intraday); il giornaliero USDJPY 2004–2013 è stato usato in M5, ma la finestra 8:00–9:55 di
  Tokyo non si ricava dalle candele giornaliere.
- **Controllo prima di tutto, senza guardare i rendimenti**: per ogni giorno gotobi, presenza dei
  minuti 23:00 e 00:54 UTC, spread mediano a quegli istanti. Giorni senza dati si contano e si
  escludono, non si riempiono. Il report di controllo esce prima di qualsiasi esito.

## 3. Calendario

- Giorni gotobi: 5, 10, 15, 20, 25 e ultimo giorno del mese di calendario.
- Se il giorno non è lavorativo a Tokyo (sabato, domenica, festa nazionale giapponese, 1–3
  gennaio, 31 dicembre), il gotobi si sposta al **giorno lavorativo precedente**.
- Festività giapponesi dalla libreria `jpholiday` (versione fissata nel lock del codice).
- Se due date spostate cadono nello stesso giorno, conta un'operazione sola.
- Giorni di controllo: tutti gli altri giorni lavorativi di Tokyo.

## 4. Regola

1. Ingresso: acquisto di USDJPY all'**ASK di chiusura del minuto 22:59 UTC** del giorno prima
   (cioè alle 8:00 di Tokyo; il Giappone non ha ora legale).
2. Uscita: vendita al **BID di chiusura del minuto 00:54 UTC** (alle 9:55 di Tokyo).
3. Una sola posizione, 1 unità di nozionale. Nessuno stop, nessun target, nessun filtro.
4. La finestra non attraversa il rollover delle 17:00 di New York, quindi niente swap.

## 5. Costi

- Spread osservato (ASK in entrata, BID in uscita), più slippage 0,1 pip per lato.
- Versione Naga: se lo spread Dukascopy al minuto è più basso di 0,9 pip a/r, si usa 0,9 pip.
  **Il giudizio usa la versione Naga (deciso da Giacomo il 23/11/2026).**
- Stress: costo totale ×1,5 e ×2.

## 6. Misure

- Rendimento netto per operazione in bp: media, deviazione, quota vinte, IC 95% bootstrap per
  giorno (10.000 campioni, seed 20261123).
- Stessa finestra nei giorni di controllo, e **differenza gotobi meno controllo** con IC, per
  separare l'effetto da un trend generale dello yen (come il 2022).
- Per anno e per tipo di giorno (5, 10, 15, 20, 25, fine mese), solo descrittivo.

## 7. Benchmark

Giorni casuali: 10.000 volte si estrae dai giorni di controllo lo stesso numero di giorni dei
gotobi validi, stessa regola e stessi costi (seed 20261124). Si confronta la media reale con il 95°
percentile. Una sola regola e nessuna variante: nessuna correzione max-of-K.

## 8. Criteri (decisi prima, una sola esecuzione)

- **PASSED** se valgono tutte: media netta Naga > 0; IC 95% tutto sopra zero; sopra il 95°
  percentile dei giorni casuali; media > 0 anche con costi ×1,5; differenza gotobi meno controllo > 0.
- **INCONCLUSIVE**: media netta > 0 e sopra il 95° percentile, ma l'IC comprende lo zero.
- **REJECTED**: ogni altro caso.
- Dopo l'esito: nessuna variante, nessun nuovo campione. Se REJECTED, la famiglia si chiude.
- Se PASSED: si può proporre di farla girare sul conto demo Naga (0,01 lotti), con una tua
  approvazione separata. Mai su un conto reale senza un'altra decisione.

## 9. Scelte da confermare (una alla volta nel thread)

1. ~~Campione di conferma~~ **Deciso 23/11 10:10: 2008–2014 più 2024–2026** (circa 700 gotobi).
2. ~~Costi~~ **Deciso 23/11 10:10: versione Naga, minimo 0,9 pip a/r.**
3. ~~Confronto con i giorni di controllo~~ **Deciso 23/11 10:11: "differenza gotobi meno controllo > 0" è dentro PASSED.**

## 10. Potenza (dichiarata prima)

Deviazione per operazione circa 15 bp (esplorazione). Se l'effetto vero netto è circa 1 bp:
- solo 2024–2026 (n ≈ 200): errore standard ≈ 1,06 bp, probabilità di PASSED circa 15%;
- 2008–2014 più 2024–2026 (n ≈ 700): errore standard ≈ 0,57 bp, probabilità di PASSED circa 40–45%.
L'esito più probabile con il solo holdout è INCONCLUSIVE anche se l'effetto è vero.
Guadagno atteso anche se PASSED: circa +0,7% l'anno sul nozionale. È un test di conoscenza, non
una fonte di reddito.

## 11. Lavoro necessario (dopo il congelamento, PR da approvare)

Download USDJPY M1 BID+ASK 2008–2014 dal PC, calendario gotobi con test, ipotesi nel
modulo `research/hyp` sul simulatore M2 senza modifiche alle regole di esecuzione, TOML generato
da questo testo, controllo dati, poi un solo run.
