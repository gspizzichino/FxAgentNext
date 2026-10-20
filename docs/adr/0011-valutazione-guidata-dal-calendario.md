# ADR-0011 — Valutazione guidata dal calendario, non dalla presenza dei dati

Stato: Proposta · Data: 2026-10-20

## Contesto
Un loop che itera sulle barre presenti salta in silenzio i buchi nei dati.

## Decisione
Il replay genera gli istanti di valutazione dal calendario FX (ogni chiusura
attesa della barra del timeframe di segnale). Ogni istante produce una
valutazione: se la barra attesa manca o la finestra contiene un gap inatteso,
l'esito è un'astensione `DATA_GAP`. Fuori sessione non si valuta e `explain`
risponde con il motivo di calendario.

## Conseguenze
- Copertura dei dati misurabile come rapporto valutazioni con dati / valutazioni attese.
- Il calendario e le chiusure attese diventano dati versionati.
