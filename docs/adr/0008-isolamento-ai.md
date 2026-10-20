# ADR-0008 — AI di ricerca isolata dall'operatività

Stato: Proposta (implementazione M4) · Data: 2026-10-20

## Contesto
Nel vecchio FxAgent un dibattito Bull/Bear su LLM era nel percorso che
selezionava o scartava i setup, rendendo le decisioni non riproducibili.

## Decisione
Nessun LLM nel percorso decisionale, né in replay né in runtime. L'agente di
ricerca è asincrono, legge risultati in sola lettura e produce
`ResearchProposal` in uno schema chiuso, convertibile in una nuova versione di
strategia e in una specifica di esperimento. Non ha accesso in scrittura a
config attive, rischio o stato operativo. Ogni proposta passa dalla stessa
validazione e dall'approvazione umana. Un'eventuale influenza operativa futura
richiede un esperimento che dimostri beneficio incrementale rispetto al sistema senza AI.

## Conseguenze
- Il sistema funziona identico con l'AI spenta.
- La qualità dell'AI si misura come produttività di ricerca, non come P&L.
