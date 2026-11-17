# M2: baseline Donchian sullo sviluppo 2015–2020 (EURUSD, GBPUSD, USDJPY)

Data: 14 novembre 2026. Split di sviluppo, dati reali Dukascopy M1 BID+ASK (2.192/2.192 giorni per coppia e lato), codice `a8291fb` (EURUSD: run di controllo identico a `eurusd-dev-001`). Rendimenti MODELLED, in R (R = 1,5 × ATR14 alla decisione). Sullo sviluppo il protocollo non prende decisioni (esito NOT_APPLICABLE).

Baseline congelata, identica per le tre coppie: canale 20, stop 1,5 ATR, target 2 R, costi `cost-model-v1` (commissione 0,35 pip per lato, slippage 0,1/0,2/0 pip).

## Risultati per coppia (exec ×1)

| Coppia | Trade | Netto | IC 95% | Costi ×1,5 | Costi ×2 | Lordo (signal) | IC lordo | PF | Max DD R | Tot R | %ile dir. casuale | %ile tempo casuale |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EURUSD | 1657 | -0,053 | [-0,119; 0,011] | -0,077 | -0,100 | -0,013 | [-0,075; 0,047] | 0,92 | 101,0 | -88,1 | 84,8 | 69,8 |
| GBPUSD | 1714 | -0,031 | [-0,099; 0,039] | -0,048 | -0,064 | +0,022 | [-0,043; 0,086] | 0,96 | 106,0 | -52,4 | 93,4 | 76,4 |
| USDJPY | 1640 | -0,036 | [-0,101; 0,030] | -0,060 | -0,083 | +0,028 | [-0,036; 0,092] | 0,95 | 78,5 | -59,4 | 69,8 | 80,5 |

Soglia prevista per la validazione sui benchmark: 95° percentile.

## R per anno (exec ×1)

| Coppia | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 |
|---|---|---|---|---|---|---|
| EURUSD | -17,6 | -21,1 | 13,9 | -25,4 | -23,2 | -14,9 |
| GBPUSD | -48,3 | 0,8 | -22,8 | -15,4 | 23,6 | 9,7 |
| USDJPY | -37,8 | 27,8 | -17,3 | -10,1 | -28,1 | 6,2 |
| somma (descrittiva) | -103,7 | 7,5 | -26,2 | -50,9 | -27,7 | 1,0 |

## Vista aggregata (descrittiva, non è un portafoglio)

- 5.011 trade, expectancy -0,040 R, IC 95% [-0,079; 0,000] con bootstrap a blocchi settimanali.
- I trade non sono indipendenti: 4.503 su 5.011 sono aperti insieme a un trade su un'altra coppia; 1.465 gruppi sovrapposti, di cui 957 con più coppie; massimo 3 posizioni aperte insieme, in media 1,75.
- Coppie di trade contemporanei con la stessa esposizione al dollaro: 3.219; opposta: 1.540. Le tre coppie ripetono in buona parte la stessa scommessa sul dollaro.
- I 508 trade isolati hanno expectancy -0,329, quelli sovrapposti -0,007. È un dato descrittivo, osservato dopo i risultati: non è un'ipotesi e non va usato per scegliere filtri.

## Diagnostica dei trade isolati (sola lettura, 14 novembre)

"Isolato" (nessuna sovrapposizione con trade di altre coppie) si conosce solo dopo l'uscita, quindi seleziona i trade brevi, che più spesso finiscono a stop.

- Costi uguali nei due gruppi (commissione 0,03 R, spread 0,02 R, rischio mediano 23–24 pip); gli isolati sono già a -0,295 R al lordo.
- A parità di motivo di uscita l'R è identico (STOP circa -1,06, TARGET circa +1,95); cambia la quota: target 24% negli isolati, 35% nei sovrapposti.
- Durata mediana 2,2 ore contro 6,8; la quota di isolati scende dal 22% sotto l'ora allo 0% oltre i 3 giorni. Pesando i sovrapposti con le durate degli isolati la loro expectancy passa da -0,007 a -0,215: la durata spiega circa due terzi della differenza.
- Il resto è concentrato in pochi trade lunghi (circa 84). Ipotesi non verificata: le rotture simultanee su più coppie sono mosse del dollaro più persistenti.
- Nessun indizio di errore. "Isolato" non è utilizzabile come filtro; una versione nota prima dell'ingresso (quante coppie danno segnale nella stessa ora) sarebbe una nuova ipotesi da pre-registrare.

## Integrità

- Riconciliazione bilanciata su tutte e tre le coppie: candidati = eseguiti + counterfactual + non entrati; nessuna entrata o uscita fuori dallo split; trade aperti a fine 2020 marcati `END_OF_DATA` ed esclusi (1 EURUSD, 1 GBPUSD, 0 USDJPY).
- Nessun minuto mancante sui percorsi; ambigui stop/target nello stesso minuto: 0, 2, 1.
- `eurusd-dev-001` è stato riprodotto identico con il codice nuovo; l'originale non è stato toccato.

| Esperimento | Fingerprint | Dataset BID | Percorso M1 |
|---|---|---|---|
| eurusd-dev-001 | `8744aac3…` (controllo `df4dfb55…`) | `1b962aea…` | `3f75a61a…` |
| gbpusd-dev-001 | `d7cad2df…` | `4e95b394…` | `098f7a1d…` |
| usdjpy-dev-001 | `b80ff56b…` | `a6859b4c…` | `dde6863b…` |

## Lettura

- Su nessuna coppia la baseline ha un vantaggio netto: expectancy negativa dopo i costi, intervalli che includono lo zero.
- Al lordo GBPUSD e USDJPY sono appena positivi (+0,02/+0,03 R), meno del costo per trade (circa 0,05 R): anche con costi zero il vantaggio sarebbe indistinguibile dal caso.
- Contro la direzione casuale nessuna coppia arriva al 95° percentile richiesto in validazione.
- Le tre coppie non sono tre prove indipendenti: buona parte dei trade è la stessa esposizione al dollaro.

## Proposta per chiudere M2

1. **Validazione della baseline congelata, una sola volta**: quando il 2021–2023 delle tre coppie è scaricato, eseguire le pre-registrazioni `*-val-001` (stesso testo, split validation, dati importati da dicembre 2020 per il warm-up). È il passo previsto dalla specifica (§8) per la baseline, decisa prima di ogni risultato, e dà l'esito formale VALIDATION_PASSED / REJECTED / INCONCLUSIVE.
2. **Holdout sigillato**: nessuna apertura, perché la baseline non è candidata.
3. **Variante 10 / 2,0 / 3,0**: resta un'ipotesi esplorativa per M4, con una sua pre-registrazione, senza usare ora la validazione.
4. **Report finale M2**: infrastruttura completata (dati, simulatore, protocollo, riconciliazione, riepilogo) e risultati così come sono, anche negativi.
5. **M3** (MT5 demo): solo dopo la tua approvazione. Senza una strategia candidata servirebbe a verificare l'esecuzione e il confronto broker, non a guadagnare.
