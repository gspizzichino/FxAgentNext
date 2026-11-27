# Pre-registrazione H1: deriva prima delle decisioni delle banche centrali (CONGELATA 2026-11-27)

ID: `h1-cbdrift-001`. Congelata il 2026-11-27 alle 08:41 UTC con la scelta di Giacomo. Bozza del 23 novembre 2026, completata il 27 novembre, scritta senza guardare prezzi FX né
valori dei tassi (dei file dei tassi e del calendario si sono lette solo date, conteggi ed esiti
delle decisioni): il paper (Borisenko e Pozdeev 2017), `m6/shortlist-ipotesi.md`,
`m6/h1-fattibilita-dati.md`, `m5/bilancio-ricerca.md` e `docs/costi-broker-naga.md`.
Scelte di Giacomo del 2026-11-23: validazione unica 2014–2026, correzione con la mediana di un anno, Norvegia inclusa se i dati passano il controllo.
sha256 di questo testo nel file `.sha256` accanto. Ogni modifica alle regole è una nuova
pre-registrazione con un nuovo ID.

## 0. Ipotesi e budget

- **Ipotesi.** Nei 10 giorni lavorativi prima di una riunione programmata, la valuta si muove nella
  direzione della decisione attesa dal mercato monetario: si apprezza prima di un rialzo atteso,
  si deprezza prima di un taglio atteso.
- **Ragione economica.** Incorporazione graduale dell'informazione e posizionamento prima
  dell'evento (paper: circa +0,5% prima dei rialzi, −0,7% prima dei tagli, 2000–2017).
- **Budget fisso.** Una sola variante, un solo run di sviluppo, una sola validazione. Se fallisce,
  nessuna variante (soglie, finestre, sostituti diversi): la famiglia si chiude.
- **Probabilità a priori di PASSED**: circa 10% (fattibilità, §6).

## 1. Dati

### 1.1 Banche centrali e valute

| Banca centrale | Valuta | Coppia Dukascopy | Note |
|---|---|---|---|
| Fed (FOMC) | USD | paniere delle valute ammesse nel run | vedi §3.3 |
| BCE | EUR | EURUSD | |
| BoE | GBP | GBPUSD | |
| SNB | CHF | USDCHF | esclusa dal 6/9/2011 al 15/1/2015 (cambio minimo), come nel paper |
| RBA | AUD | AUDUSD | solo riunioni dal 2007-12-01 (§1.2) |
| RBNZ | NZD | NZDUSD | solo validazione: tasso a 1 mese giornaliero dal 2009 (§1.3) |
| BoC | CAD | USDCAD | |
| Riksbank | SEK | USDSEK | |
| Norges Bank | NOK | USDNOK | esclusa: non passa la regola di ammissione del §1.3 (scelta di Giacomo del 2026-11-23: dentro solo se i dati la passano) |

BoJ esclusa (tassi a zero quasi sempre, esclusa anche nel paper).

### 1.2 Calendario delle riunioni

- File CSV costruito dagli archivi ufficiali di ogni banca, dal PC con il browser: data e ora
  dell'annuncio (convertita in UTC), riunione programmata sì/no, tasso di policy prima e dopo.
- Entrano solo le riunioni **programmate e annunciate in anticipo**. Le decisioni d'emergenza
  (es. ottobre 2008, marzo 2020) si escludono: non sono prevedibili 12 giorni prima.
- Controllo incrociato: ogni variazione del tasso di policy nelle serie storiche deve
  corrispondere a una riunione del calendario o essere marcata come emergenza. Le discrepanze si
  contano e si riportano prima di qualsiasi esito.
- Il calendario si raccoglie per tutto il 2003–2026 prima del congelamento: contiene solo date ed
  esiti delle decisioni, che conosciamo già a memoria, nessun prezzo FX.
- **Raccolto il 2026-11-26** dal PC: `C:\Users\HP\FxAgentNext\data\h1-rates\calendar\meetings.csv`,
  1.538 annunci di 8 banche (Norges Bank non serve), 395 fonti ufficiali con sha256 in
  `sources_manifest.csv`, 31 annunci non programmati, 0 discrepanze con le serie dei tassi (RBNZ compresa, verificata il 2026-11-27).
  sha256 al 2026-11-26: `meetings.csv` a9aa354d27a1064e45a29ac51a5eed95cfd887c83a901ef620f5d1cf3e25eba5, `sources_manifest.csv` 8ce226ce1ad3d81556cf75b929d30709fd4c7fa8f96c4277a84fde74c54a6625 (si ricontrollano al congelamento).
- **RBA solo dal 2007-12-01.** Prima la RBA non annunciava le decisioni di non variare: il
  calendario conterrebbe solo le riunioni con un cambio, cioè eventi scelti dall'esito. Si escludono
  tutte le riunioni RBA precedenti.
- **Riunione programmata** = riunione ordinaria nel calendario che la banca aveva pubblicato in
  anticipo. Riksbank 2015-03-18 è non programmata ed esclusa: il Rapporto annuale 2015 della
  Riksbank (p. 28) la indica come riunione fuori dalle riunioni ordinarie (fonte salvata con sha256).
  Totale annunci non programmati: 31.
- Gli esiti (rialzo, taglio, invariato) non entrano nella regola: servono solo al controllo
  incrociato e alle spaccature del report.

### 1.3 Attese di mercato (sostituto dell'OIS)

Gli OIS sono a pagamento. Si usa un **tasso di mercato a 1 mese** e un **tasso overnight o di
policy** per valuta, giornalieri. File scaricati dal PC il 2026-11-26 in
`C:\Users\HP\FxAgentNext\data\h1-rates\raw\`, con sha256 in `data\h1-rates\manifest.csv`
(letti solo date e numero di righe, nessun valore).

| Valuta | Tasso a 1 mese | Overnight / policy |
|---|---|---|
| USD | T-bill 4 settimane, FRED `DTB4WK` (dal 2001-07) | effective fed funds, FRED `DFF` |
| EUR | Euribor 1 mese, Banco de España `ti_1_7`, colonna `D_DNBAC172` | EONIA `EON.D.EONIA_TO.RATE` fino al 2021-12-31, poi €STR `EST.B.EU000A2X2A25.WT` + 8,5 bp (definizione ufficiale di EONIA dal 2019, nessun salto) |
| GBP | sviluppo: tasso interbancario 1 mese BoE `IUDVNEA` (1997–2017-12); validazione: OIS 1 mese della curva spot BoE (`oisddata`, dal 2009) | SONIA `IUDSOIA` |
| AUD | sviluppo: OIS 1 mese RBA F1 `FIRMMOIS1D` (2001-07 → 2022-12); validazione: BABs 30 giorni `FIRMMBAB30D` | cash rate `FIRMMCRTD` |
| CAD | T-bill 1 mese BoC Valet `TB.CDN.30D.MID` (dal 2001) | CORRA `AVG.INTWO` |
| SEK | T-bill 1 mese Riksbank `SETB1MBENCHC` (STIBOR finisce nel 2020-07) | repo rate `SECBREPOEFF` |
| NZD | bank bill 30 giorni RBNZ B2 `INM.DB01.NZZV` (giornaliero solo dal 2009-01-29) | OCR `INM.DP1.N` (l'overnight interbancario `INM.DN.NZK` ha buchi dopo il 2018 e non si usa) |
| CHF | repo a 1 mese SAR1M, SNB `zirepo` H4 (dal 1999-07) | SARON, SNB `zirepo` H0 |
| NOK | **nessun tasso a 1 mese prospettico prima del 2020** (NIBOR a licenza; T-bill solo da 3 mesi e dal 2019; NOWA 1M è una media passata) | tasso di policy `IR/B.KPRA.SD.R` |

**Regola per la scelta della serie**: per ogni run si usa una sola serie a 1 mese per valuta, la
prima della lista "OIS, poi tasso di mercato" che copre tutto il run compreso il riscaldamento
(250 giorni feriali per la mediana più 5 per la media). Nessun cambio di serie dentro un run,
perché un salto di livello passerebbe nella mediana di un anno come falso segnale. Le medie
composte all'indietro (SARON 1M compound, NOWA 1M average) non sono attese e non si usano.

Conseguenze già note dai soli metadati: **NOK non passa la regola di ammissione** in nessuno dei
due run ed è esclusa (scelta del 23/11: dentro solo se i dati passano il controllo); **NZD è
esclusa dallo sviluppo** (tasso a 1 mese solo dal 2009) ed entra nella validazione se passa il
controllo. Il paniere della Fed usa le valute ammesse in ciascun run.

sha256 di `data\h1-rates\manifest.csv` al 2026-11-27: d4804daa18c4722eca52d5a9e64d3910761a7e7a8365bd714f49d74cac901bac.

**Ammissione di una valuta**: nel periodo del run, la serie a 1 mese e quella overnight hanno
valori in almeno il 90% dei giorni feriali. Sotto soglia la valuta si esclude dal run intero e si
conta. Nessun riempimento oltre 5 giorni feriali (si porta avanti l'ultimo valore al massimo
5 giorni).

### 1.4 Prezzi

Candele giornaliere Dukascopy BID e ASK delle coppie contro USD delle valute ammesse (giorno UTC 00:00–24:00), già sul
PC (`~/FxAgentNext-hyp/data/raw-d1`), importate con hash come in M5. Giorno valido come in M5
(feriale, BID e ASK presenti, volumi > 0, 0 < BID ≤ ASK).

## 2. Split

**Deciso da Giacomo il 2026-11-23**: validazione unica 2014–2026.

| Split | Periodo | Stato dei dati |
|---|---|---|
| Sviluppo | 2004-01-01 → 2014-01-01 (tassi dal 2003 per il riscaldamento) | G10 giornaliero già usato in M5, ma con segnali mensili di carry e momentum, mai a finestre di evento |
| Validazione | 2014-01-01 → 2026-11-01 (eventi con uscita entro il 2026-10-31), **una sola volta**, unica | vergine come paniere |

Motivo: con la sola validazione 2014–2019 l'esito sarebbe quasi certamente INCONCLUSIVE anche
con un effetto vero (fattibilità, §5: t atteso ~1,5 con effetto pieno, ~0,8 con metà). Costo:
si spende in un colpo tutto il G10 giornaliero vergine, compreso il giornaliero di EUR, GBP, JPY
2024–2026, che dopo non sarebbe più vergine nemmeno come direzione dei cambi per l'holdout
intraday M2.

Alternativa scartata: validazione 2014–2019 e holdout 2020–2026 chiuso, come in M5.

## 3. Regola

### 3.1 Tempi

Per ogni riunione programmata con annuncio nel giorno UTC **T**:
- **Giorno del segnale S** = 12 giorni feriali validi prima di T.
- **Ingresso** alla chiusura del giorno T−11, **uscita** alla chiusura del giorno T−1 (giorni
  UTC): 10 giorni in posizione, mai attraverso l'annuncio.
- Le ore di molti annunci sono solo "ora usuale" della banca. Contano solo per la RBNZ, che ha
  annunciato sia alle 9:00 sia alle 14:00 di Wellington, cioè prima o dopo la mezzanotte UTC. Per
  non dipendere dall'ora, per la RBNZ **T = data locale dell'annuncio − 1 giorno** sempre (uscita
  una chiusura prima se l'annuncio era alle 14:00). Per le altre 7 banche l'annuncio cade sempre di
  giorno nel giorno UTC della data locale, e T è quella data.

### 3.2 Attesa e soglia

1. Spread grezzo a S: media su 5 giorni feriali fino a S del tasso a 1 mese meno media del tasso
   overnight sugli stessi giorni.
2. **Correzione del premio di credito** (deciso da Giacomo il 2026-11-23): si sottrae la mediana dello spread
   grezzo nei 250 giorni feriali che finiscono il giorno prima di S. Motivo: i tassi interbancari e
   i T-bill hanno un premio strutturale (credito, fuga verso la qualità, scarto fra tasso di policy
   e overnight) che non è un'attesa di decisione; la mediana di un anno lo toglie e lascia il
   movimento recente. Alternative scartate: nessuna correzione (come il paper con l'OIS vero, ma con falsi
   rialzi quando gli spread esplodono), oppure variazione del tasso a 1 mese nelle 4 settimane
   prima di S.
3. **Cambio atteso** Δ = spread corretto × 30 / (30 − d), dove d sono i giorni di calendario da S
   all'annuncio (la parte del mese a 1 mese che cade dopo la riunione).
4. **Rialzo previsto** se Δ ≥ +10 bp, **taglio previsto** se Δ ≤ −10 bp, altrimenti nessuna
   posizione (soglia del paper).

### 3.3 Posizioni

- Riunione di una banca non USA: rialzo previsto → lunghi la valuta contro USD; taglio previsto →
  corti. Un'unità di nozionale in USD per evento, senza leva.
- Riunione Fed: rialzo previsto → lunghi USD contro il paniere a pesi uguali delle valute ammesse
  (1/n per coppia); taglio previsto → corti USD.
- Eventi sovrapposti (riunioni vicine di banche diverse) restano posizioni separate: ogni evento ha
  il suo rendimento.
- Rendimento di un evento: variazione del cambio fra le chiusure di ingresso e di uscita ai prezzi
  medi, più il differenziale dei tassi a 1 mese × giorni di calendario / 360, meno i costi.

## 4. Costi (Naga, misurati il 18/11)

- Acquisto all'ASK e vendita al BID di chiusura Dukascopy (mezzo spread per lato); spread a zero
  sostituito dalla mediana degli spread non nulli della coppia nel run, come `g10-cost-v2`, con
  conteggio delle sostituzioni.
- Commissione 0 (conto Naga solo spread); slippage 0,1 pip per lato.
- Ricarico sullo swap: **1,6% annuo per lato** (mediana Naga) su ogni notte in posizione, tripla
  il mercoledì.
- Stress ×1,5 e ×2 su **spread, slippage e ricarico insieme** (lezione del bilancio, §4).

## 5. Misure

Per evento: rendimento netto in bp. Media, deviazione standard, t, quota positiva; IC 95% della
media con bootstrap a blocchi per mese di calendario (eventi dello stesso mese ricampionati
insieme, perché si sovrappongono e condividono il dollaro), 10.000 campioni, seed 20261123.
Spaccature riportate, non usate per decidere: rialzi e tagli, banca per banca, anno per anno,
lordo e netto, con e senza ricarico. Conteggio degli eventi previsti e degli esclusi per motivo.

## 6. Benchmark

- **Direzione casuale**: stessi eventi, stessi tempi, stessi costi, segno lungo o corto estratto a
  caso; 1.000 ripetizioni, seed 20261124. La media reale si confronta con il 95° percentile.
- **Riferimento**: lunghi la valuta contro USD in ogni evento previsto, qualunque sia la
  direzione (misura la deriva del dollaro del periodo).
- Una sola variante: nessuna correzione max-of-K dentro la famiglia. Il bilancio ricorda che le
  famiglie già provate sono 8: per questo la soglia di validazione chiede l'IC tutto sopra zero.

## 7. Criteri

**Sviluppo → proposta di validazione**, se valgono tutte:
1. media netta per evento > 0 con costi ×1;
2. sopra il 95° percentile della direzione casuale;
3. media netta > 0 con costi ×1,5;
4. media netta > 0 in almeno 2 dei 3 sottoperiodi 2004–2007, 2008–2010, 2011–2013 fra quelli con
   almeno 10 eventi.

Sotto questa soglia la famiglia si chiude su sviluppo.

**Validazione** (una volta, la stessa regola):
- **PASSED**: valgono 1–3 e l'IC 95% è tutto sopra zero;
- **INCONCLUSIVE**: valgono 1–3 ma l'IC comprende lo zero;
- **REJECTED**: ogni altro caso.

Un PASSED non porta al conto demo in automatico: serve una decisione separata.

## 8. Controlli prima di qualsiasi esito

Calendario (riunioni per banca e anno, discrepanze con le variazioni dei tassi), copertura di ogni
serie di tasso, valute ammesse ed escluse, giorni non validi per coppia, spread a zero. Il report di
controllo dello sviluppo si scrive e si legge prima di calcolare un solo rendimento. Per la
validazione lo stesso controllo si fa solo dentro il run di validazione, dopo l'approvazione.

## 9. Attese dichiarate prima dei risultati

- Sviluppo 2004–2013: cicli di rialzo 2004–2007 e tagli 2008–2009 danno molti eventi previsti
  (ordine di grandezza 100–200, stima a memoria, non contata).
- Il sostituto è rumoroso: nel 2007–2009 lo spread interbancario contro OIS è salito oltre 100 bp;
  la correzione del §3.2 lo riduce ma non lo elimina.
- Dopo la pubblicazione (2017) l'effetto può essersi ridotto: un INCONCLUSIVE è l'esito più
  probabile anche con un effetto vero.

## 10. Rischi di snooping

- Ricordiamo a memoria le grandi decisioni (2008, SNB 2015, 2022): nessun filtro su questi eventi,
  le regole si applicano a tutti; SNB 2015 trattata dalla regola del cambio minimo, già nel paper.
- Il G10 2004–2013 è già stato visto a livello mensile in M5 (carry e momentum): non a finestre di
  evento, ma il trend del dollaro di quegli anni è noto.
- Nessuna soglia, finestra o tasso scelto guardando esiti: tutto viene dal paper o dalla logica
  scritta sopra.

## 11. Lavoro nel codice (una PR da approvare prima dei run)

Import del calendario e dei tassi CSV con hash, modulo `research/cbdrift` (segnale, eventi,
rendimenti, costi Naga, direzione casuale, bootstrap per mese), split fissati nel codice con la
validazione apribile una sola volta e solo con approvazione scritta, TOML
`prereg/h1-cbdrift-001.toml` generato da questo testo.
