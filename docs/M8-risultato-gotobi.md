# Risultato e1-gotobi-001 (27/11/2026): INCONCLUSIVE

Unico run, lanciato il 27/11/2026 ~20:13 UTC su richiesta di Giacomo, sul PC (`~/FxAgentNext-hyp`,
`research/a04cb7433e716aad/report.json`). Testo congelato sha256 `64bf779a…`; TOML con i due path
sha256 `69384ee3…` (uguale nel report). Non si ripete e non ha varianti (§8 del testo).

## Esito

| Condizione per PASSED | Valore | Passa? |
|---|---|---|
| Media netta Naga > 0 | **+0,93 bp** per operazione | sì |
| IC 95% tutto sopra zero | [−0,57; +2,52] | **no** |
| Sopra il 95° percentile dei giorni casuali | 0,93 > 0,38 | sì |
| Media > 0 con costi ×1,5 | +0,22 bp | sì |
| Gotobi meno controllo > 0 | +1,44 bp, IC [−0,30; +3,22] | sì |

Quattro condizioni su cinque: per il testo congelato è **INCONCLUSIVE**.

## Numeri

- 693 giorni gotobi validi (2008–2014 e 2024–ottobre 2026), vinti 53,8%, deviazione 20,8 bp.
- Giorni di controllo: 1.690, media −0,52 bp nella stessa finestra.
- Costi ×2: −0,49 bp. Solo spread Dukascopy (senza minimo Naga): +1,02 bp.
- Per anno (bp netti): 2008 +1,6 · 2009 −4,5 · 2010 +2,2 · 2011 +5,9 · 2012 0,0 · 2013 +2,1 ·
  2014 −1,1 · 2024 +1,1 · 2025 +0,4 · 2026 +1,6. Otto anni su dieci ≥ 0.
- Il dettaglio per giorno del mese è solo descrittivo: con poche operazioni per data (le date spostate
  per festività) è rumore.

## Come leggerlo

- L'effetto ha il segno e la dimensione attesi (+0,9 bp netti contro circa +1 bp stimato
  nell'esplorazione; +1,4 bp contro gli altri giorni), ma è troppo piccolo rispetto al rumore
  (20 bp per operazione) per essere confermato con 693 eventi. È esattamente lo scenario
  dichiarato in §10 (probabilità di PASSED circa 40%).
- Anche se fosse vero, varrebbe circa +0,65% l'anno sul nozionale, e con costi doppi sparisce.
- Per il protocollo: nessuna variante e nessun nuovo campione. I dati USDJPY intraday 2008–2014 e
  2024–2026 ora sono spesi.
