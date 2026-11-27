# Pre-registrazione `l1-composito-001`: filtro storico 1976–2003 (CONGELATA)

Stato: **congelata il 26 novembre 2026, 21:05 UTC**, con l'ok di Giacomo (card). Testo identico alla
bozza del 25–26/11 salvo questa intestazione. Non modificare: ogni cambiamento richiede un nuovo ID.
Nessun prezzo, tasso o dato macro guardato fino al congelamento. Scelta di
Giacomo del 25/11 (card): preparare il filtro 1976–2003 del composito lungo, senza toccare i dati
vergini. Contesto: `m10/strategie-lungo-periodo-fattibilita-2026-11-25.md`.

## 0. Cosa è e cosa non è

- È un **filtro economico** su un periodo mai usato da noi (1976–2003), dentro il campione dei
  paper. Se fallisce, la strada lunga si chiude. Se passa, **non** prova che il premio esista oggi:
  apre solo la domanda (da decidere a parte) su come provarlo nel 2014–2026, riservato a H1.
- Budget: **una variante, un solo run**. Nessuna modifica dopo l'esito; un cambiamento = nuovo ID.

## 1. Fonti delle regole

- Momentum economico: Dahlquist e Hasseltoft, JFE 136(1), 2020 (Sharpe 0,70 annuo nel campione
  gennaio 1976–marzo 2017, 21 mercati sviluppati più emergenti). **Il testo integrale non è
  accessibile gratis**: le regole sotto vengono dalle sintesi pubbliche (Alpha Architect, SHoF) e
  da Zhang, Kappou e Urquhart, JIFMIM 92, 2024, che dichiarano di seguire la procedura di D&H.
  Dove i dettagli di D&H non sono noti, la scelta è nostra ed è segnata come tale.
- Value: Asness, Moskowitz e Pedersen, JF 2013 (variazione a 5 anni del cambio reale, segno
  negativo); Menkhoff et al., RFS 2017.

## 2. Universo e prezzi

- 9 valute contro USD: DEM (fino a dicembre 1998) poi EUR, GBP, JPY, CHF, CAD, AUD, NZD, SEK, NOK.
- Prezzi: cambi giornalieri Fed H.10 su FRED (mezzogiorno di New York, prezzo medio), ultimo giorno
  lavorativo del mese. Scaricati dal PC (FRED bloccato dal cloud), salvati con hash.
- Rendimento in eccesso mensile di una posizione lunga nella valuta *i* contro USD:
  variazione log del cambio + (tasso a 3 mesi *i* − tasso a 3 mesi USA)/12, tassi OCSE mensili
  noti a fine mese precedente (come M5).

## 3. Segnali (tutti noti a fine mese *t*, posizione per il mese *t+1*)

**A. Momentum economico** (variabili da D&H secondo le sintesi, meno i prezzi alla produzione per
scelta del 25/11): produzione industriale, vendite al dettaglio, disoccupazione, prezzi al consumo.
- Per ogni variabile e valuta: crescita log cumulata su 12, 36 e 60 mesi, media delle tre
  finestre (scelta nostra: D&H indicano che contano soprattutto i trend di 3–5 anni e che i
  risultati reggono a diverse finestre).
- Segno: positivo per produzione, vendite, prezzi (D&H: l'inflazione passata prevede rendimenti
  positivi); negativo per la disoccupazione.
- Ritardo di pubblicazione: 2 mesi per i dati mensili; dati trimestrali ripetuti nel trimestre
  successivo e ritardati di un trimestre (come Zhang et al. attribuiscono a D&H).
- Per ogni variabile: rango fra le valute disponibili, centrato; segnale A = media dei ranghi
  delle variabili disponibili per quella valuta.

**B. Value**: − variazione log a 60 mesi del cambio reale (cambio × CPI USA / CPI estero), CPI con
ritardo di 2 mesi; rango centrato.

**Composito** = media dei ranghi centrati di A e B (pesi uguali).

## 4. Portafoglio

- Pesi proporzionali al rango centrato del composito, neutri in dollari: 1 $ lungo, 1 $ corto
  (come D&H). Ribilanciamento mensile all'ultimo giorno lavorativo.
- Nessun filtro di volatilità o di regime, nessuna leva.

## 5. Costi Naga

- Ricarico swap per coppia dalla tabella di `m5/costi-broker-naga-2026-11-18.md` (mediana 1,6%
  annuo per lato), applicato ogni mese al peso assoluto. DEM come EUR.
- Spread: mediana Naga per coppia, pagata sul turnover a ogni ribilanciamento.
- Stress ×1,5 su ricarico **e** spread (lezione del bilancio, sezione 4).

## 6. Periodo e controlli

- Inizio: primo mese in cui almeno 7 valute hanno tutti e due i segnali e il tasso a 3 mesi;
  fine dicembre 2003. Se l'inizio cade dopo il 1985, ci si ferma e si decide prima di proseguire.
- Controllo dati prima di qualunque esito (`check`): copertura per serie, buchi, salti
  (EUR/DEM 1999), serie trimestrali, nessuna osservazione dopo il 2003 caricata.

## 7. Criteri del filtro (soglia t ≥ 2,0 decisa il 26/11)

PASSA solo se valgono tutte:
1. Rendimento netto medio > 0 con costi Naga ×1;
2. t di Newey-West (6 ritardi) del netto ≥ 2,0;
3. netto > 0 in entrambe le metà del periodo;
4. netto > 0 anche con costi ×1,5;
5. sopra il 95° percentile di 1.000 portafogli con ranghi casuali (stessi costi).

Altrimenti: **FALLITO**, strada lunga chiusa.

## 7bis. Copertura dei dati gratuiti (verificata il 25/11 dal browser del PC)

Solo metadati delle pagine FRED (data di inizio e fine delle serie), nessun valore letto. Unica
eccezione involontaria: la prima pagina aperta (DEXJPUS) mostra l'ultima quotazione USDJPY del
20/11/2026; un singolo prezzo corrente, nessun esito di strategia. Dopo circa 180 pagine FRED ha
iniziato a rispondere 403 (limite di frequenza): Svezia e Norvegia per CPI, produzione e vendite
restano **da verificare** al momento del download.

| Valuta | Cambio giornaliero | Tasso 3 mesi | CPI | Produzione ind. | Vendite dettaglio | Disoccupazione | Prezzi produzione |
|---|---|---|---|---|---|---|---|
| USA | — | 1964 | M 1955 | M 1919 | M 1960 | M 1955 | M 1960 |
| DEM/EUR | **DEM giornaliero non su FRED** (solo media mensile); va preso dagli storici H.10 della Fed. EUR dal 1999 | 1960 | M 1955 | M 1958 | M 1960 | T 1962 (M dal 1991) | **solo dal 1995** |
| GBP | 1971 | 1957 | M 1955 | M 1948 | M 1960 | T 1971 (M dal 1983) | **solo dal 2008** |
| JPY | 1971 | interbancario solo dal 2002; CD 3 mesi dal 1979 | M 1955 | M 1955 | M 1960 | M 1955 | M 1960 |
| CHF | 1971 | interbancario solo dal 1999; call money dal 1972 | M 1955 | T 1959 | M 1960 | **solo T dal 1999** | **solo dal 2002** |
| CAD | 1971 | 1956 | M 1914 | M 1961 | M 1960 | M 1955 | M 1960 |
| AUD | 1971 | 1968 | T 1948 | T 1974 | da verificare | M 1978 | T 1968 |
| NZD | 1971 | 1973 | T 1914 | T 1977 | da verificare | T 1986 | T 1960 |
| SEK | 1971 | 1982 | da verificare | da verificare | da verificare | M 1983 | M 1982 |
| NOK | 1971 | 1979 | da verificare | da verificare | da verificare | M 1989 | M 1977 |

M = mensile, T = trimestrale. Conseguenze:
- I **prezzi alla produzione** mancano per Germania, Regno Unito e Svizzera fino a metà anni '90 o
  oltre: inutilizzabili per il 1976–2003.
- La **disoccupazione** svizzera manca fino al 1999.
- Con finestre fino a 60 mesi e tassi svedesi dal 1982, l'inizio realistico del test è intorno al
  **1983–1984**: circa 20 anni fino al 2003. Per t ≥ 2 serve uno Sharpe netto ≥ ~0,45.
- Tassi a 3 mesi: per JPY e CHF serve un sostituto prima del 2002/1999 (CD 3 mesi per il Giappone,
  call money per la Svizzera).

## 8. Scelte (tutte decise da Giacomo, 25–26 novembre)

1. ~~Variabili del momentum economico~~ **Deciso da Giacomo il 2026-11-25 22:03 UTC**: CPI,
   produzione industriale, vendite al dettaglio e disoccupazione, media delle disponibili per
   ogni valuta; prezzi alla produzione esclusi.
2. ~~Soglia del filtro~~ **Deciso da Giacomo il 2026-11-26 15:47 UTC**: t di Newey-West ≥ 2,0 sul netto.
3. ~~Tassi a 3 mesi mancanti~~ **Deciso da Giacomo il 2026-11-26 17:46 UTC**: sostituti, CD a 3 mesi
   per il Giappone fino a marzo 2002 (`IR3TCD01JPM156N`), call money per la Svizzera fino a giugno 1999
   (`IRSTCI01CHM156N`); poi interbancario OCSE.
4. ~~Macro per l'EUR~~ **Deciso da Giacomo il 2026-11-26 18:56 UTC**: dati tedeschi per tutto il
   periodo, anche dopo il 1999.
