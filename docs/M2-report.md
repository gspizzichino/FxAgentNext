# M2: report finale

Data: 17 novembre 2026. Codice `a8291fb` (main). Tutti i rendimenti sono MODELLED, in R (R = 1,5 × ATR14 alla decisione). Questo report riporta i risultati così come sono (spec M2 §13).

## 1. Cosa è stato costruito

- **Dati**: downloader Dukascopy robusto a 429/503 e timeout, cache verificabile con fetch_log. Import M15 BID/ASK e percorso M1 BID+ASK. Copertura 2015-01-01 → 2023-12-31 completa per EURUSD, GBPUSD e USDJPY (BID e ASK, 0 giorni mancanti negli split usati). Il 2024–2026 (holdout) è in download e non è stato usato.
- **Simulatore di ricerca** (ADR-0016): fill su ask/bid con slippage, uscite con la semantica MT5, ordine dentro il minuto, gap, uscita a tempo, una posizione per simbolo, quattro stream (signal, exec, counterfactual, broker vuoto fino a M3), costi a 3 livelli (×1, ×1,5, ×2).
- **Protocollo**: split fissati nel codice, pre-registrazione TOML con hash nel fingerprint, criteri decisi prima dei risultati, holdout con registro di apertura, benchmark nulli (direzione casuale, tempo casuale, sempre long/short), bootstrap a blocchi settimanali.
- **Integrità** (spec §15): ogni esperimento usa solo prezzi del proprio split; riconciliazione candidati = eseguiti + counterfactual + non entrati, altrimenti il report non si scrive; parità replay/runtime (T11); riproducibilità verificata rilanciando `eurusd-dev-001` con il codice nuovo (numeri identici).
- **Riepilogo tra coppie** (`research-summary`): numeri affiancati e vista aggregata che separa i trade sovrapposti; la somma degli R è descrittiva, non un portafoglio.

## 2. Ipotesi testata

Baseline Donchian breakout H1 congelata (canale 20, stop 1,5 ATR, target 2 R), su EURUSD, GBPUSD e USDJPY, costi `cost-model-v1`. Nessun parametro cambiato dopo i risultati.

## 3. Sviluppo 2015–2020 (nessuna decisione per protocollo)

| Coppia | Trade | Netto | IC 95% | Lordo (signal) | %ile dir. casuale |
|---|---|---|---|---|---|
| EURUSD | 1657 | -0,053 | [-0,119; 0,011] | -0,013 | 84,8 |
| GBPUSD | 1714 | -0,031 | [-0,099; 0,039] | +0,022 | 93,4 |
| USDJPY | 1640 | -0,036 | [-0,101; 0,030] | +0,028 | 69,8 |
| Tre coppie | 5011 | -0,040 | [-0,079; 0,000] | | |

Dettagli in [M2-risultati-sviluppo.md](M2-risultati-sviluppo.md).

## 4. Validazione 2021–2023 (eseguita una sola volta)

| Coppia | Trade | Netto | IC 95% | ×1,5 | Lordo (signal) | %ile dir. casuale | %ile tempo casuale | Esito |
|---|---|---|---|---|---|---|---|---|
| EURUSD | 898 | -0,128 | [-0,214; -0,042] | -0,152 | -0,040 | 8,7 | 10,7 | **REJECTED** |
| GBPUSD | 883 | -0,180 | [-0,260; -0,096] | -0,198 | -0,066 | 2,2 | 2,2 | **REJECTED** |
| USDJPY | 845 | +0,030 | [-0,055; 0,112] | +0,009 | +0,097 | 97,6 | 95,5 | **INCONCLUSIVE** |
| Tre coppie | 2626 | -0,095 | [-0,148; -0,041] | | | | | |

R per anno (exec ×1):

| Coppia | 2021 | 2022 | 2023 |
|---|---|---|---|
| EURUSD | -50,1 | -47,5 | -17,5 |
| GBPUSD | -54,6 | -54,7 | -49,6 |
| USDJPY | 24,8 | 29,1 | -28,7 |

- USDJPY è INCONCLUSIVE perché l'IC include lo zero e il 2023 (-28,7 R) è sotto il limite di -10 R. Nello stesso periodo "sempre long" su USDJPY vale circa zero (-0,002) e "sempre short" -0,179: il risultato è coerente con il forte rialzo del dollaro sullo yen nel 2021–2022 più che con un vantaggio stabile del segnale. È un'ipotesi, non verificata.
- Riconciliazione bilanciata, 0 entrate o uscite fuori split, warm-up da dicembre 2020 senza valutazioni INSUFFICIENT_HISTORY.

| Esperimento | Fingerprint | Pre-registrazione sha256 |
|---|---|---|
| eurusd-val-001 | `e24d9a12…` | `21364ee1…` |
| gbpusd-val-001 | `0ee7227d…` | `b7c70826…` |
| usdjpy-val-001 | `535c8b71…` | `c73a9264…` |

## 5. Conclusione

- La baseline Donchian H1 **non ha un vantaggio** netto sui tre cambi maggiori: respinta su EURUSD e GBPUSD, inconcludente su USDJPY, negativa nell'aggregato con IC tutto sotto zero.
- Nessuna coppia è **CANDIDATE_FOR_DEMO**; l'holdout resta sigillato.
- USDJPY non va riesaminato sulla validazione: ogni nuova idea su USDJPY è una nuova ipotesi da pre-registrare e provare prima sullo sviluppo.

## 6. Lezioni

1. **Costi rispetto a R**: con R = 1,5 ATR su H1 i costi valgono circa 0,05 R per trade; il segnale lordo è circa zero, quindi i costi lo rendono negativo. Orizzonti più lunghi riducono il peso dei costi (motivazione di S2).
2. **Correlazione tra coppie**: oltre il 90% dei trade è aperto insieme a un altro, spesso con la stessa esposizione al dollaro. Tre coppie USD non sono tre prove indipendenti.
3. **Selezione a posteriori**: "trade isolato" sembra un filtro ma dipende dalla durata del trade; le variabili note solo dopo l'uscita non si usano per scegliere regole.
4. **Dati**: Dukascopy da un PC domestico richiede giorni (60–110 giorni di file all'ora) e una coda unica; i riavvii del PC sono il rischio principale.

## 7. Prossimi passi proposti (ognuno richiede il tuo ok)

1. Report e documenti M2 nel repository (PR).
2. M4 ricerca: TOML e codice per S0, S1, S2, S3, S7 (congelate l'11/11), con la deroga E8 per S2 nella specifica; prima solo sviluppo.
3. M3 (MT5 demo) come sola infrastruttura, se e quando vorrai.
