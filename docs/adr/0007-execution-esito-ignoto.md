# ADR-0007 — Outbox degli intenti e nessun retry su esito ignoto

Stato: Proposta (implementazione M3) · Data: 2026-10-20

## Contesto
Nel vecchio FxAgent `_place_with_retry` ritentava fino a 3 volte anche quando
`order_send` restituiva `None` (esito ignoto), con rischio di ordini duplicati.
MT5 non garantisce un invio exactly-once.

## Decisione
- Ogni ordine nasce come `OrderIntent` persistito in SQLite **prima** dell'invio,
  con `client_order_id` corto nel campo `comment` e `magic` per strategia.
- Stati: `PENDING → SUBMITTING → {FILLED, PARTIAL, REJECTED, UNKNOWN}`,
  `UNKNOWN → RECONCILED_{FILLED|ABSENT}`.
- Esito ignoto o crash durante `SUBMITTING` → `UNKNOWN`, nuove entrate sul
  simbolo sospese, riconciliazione su posizioni, ordini e deal.
- Mai reinviare lo stesso intento. Se risulta assente, l'intento si chiude;
  un'eventuale nuova entrata richiede una nuova valutazione non scaduta.
- Un solo intento per `(event_id, account)`.

## Conseguenze
- Nessun duplicato per costruzione; qualche entrata persa è accettata.
- Richiede un broker simulato capace di perdere risposte per i test.
