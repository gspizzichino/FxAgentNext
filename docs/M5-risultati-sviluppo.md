# Risultati sviluppo 2004–2013: g10-cm-dev-002 (carry e momentum G10)

Run del 2026-11-18 sul PC di Giacomo, codice `c39268f`, fingerprint
`ed382adbecb719826b49032c2b33a2dc842b0f0eb5441fc5e0d94f0445ada94e`
(report completo: `~/FxAgentNext-hyp/research/ed382adb…/report.md`).

## Esito: CLOSE_ON_DEV

La famiglia si chiude su sviluppo, come prevede il testo congelato. Niente validazione.

Migliore variante: C2 (carry, 2 valute lunghe e 2 corte).

| Criterio | Soglia | Risultato | Passa |
|---|---|---|---|
| Media netta della migliore | > 0 | +0,115% al mese | sì |
| Sopra il 95° percentile del massimo casuale su 7 varianti | > +0,211% al mese | +0,115% | no |
| Varianti con media netta > 0 | almeno 4 su 7 | 3 (C2, C3, CM3) | no |
| Sottoperiodi positivi della migliore | almeno 2 su 3 | 1 (solo 2004–2007) | no |

## Cosa dicono i numeri

- **Il carry rende fino al 2007 e poi crolla.** Per C2:
  - nel 2004–2007 fa +0,608% al mese;
  - nel solo 2008 perde il 38,7%;
  - il drawdown massimo è del 46%;
  - l'IC 95% della media va da −0,577% a +0,730% al mese.
- **Il momentum non funziona.** M6 e M12 sono negativi anche al lordo: −0,077% e −0,149% al mese.
- **Il riferimento fa meglio di tutte le varianti.** Stare lunghi su tutte le valute contro USD dà +0,135% al mese netto (Sharpe 0,18). La migliore variante, C2, dà +0,115% (Sharpe 0,12).
- **I costi.**
  - Lo spread pesa poco: su C2 circa 0,007% al mese.
  - Il ricarico dell'1% annuo sulle posizioni pesa circa 0,155% al mese, più della metà del lordo di C2 (+0,277%).
  - Il ricarico è nel testo congelato. Toglierlo adesso vorrebbe dire adattare le regole al risultato.

## Limite del test, da ricordare

Lo stress ×1,5 e ×2 moltiplica solo commissione e slippage, che qui sono piccoli. Per questo C2 resta a +0,115% in tutte e tre le colonne. Se si riusa questo schema, lo stress va esteso anche a spread e ricarico.

## Contesto

- Prima del run, il controllo dei dati ha trovato spread a zero:
  - USDSEK in 36 ribilanciamenti su 120;
  - USDJPY in 22 su 120.
- Per questo è stata congelata la 002, prima di vedere qualsiasi segnale o rendimento. La 001 non è mai stata eseguita.

*I numeri vengono dal report del run letto sul PC. I dati 2014–2026, cioè validazione e holdout, non sono stati usati.*
