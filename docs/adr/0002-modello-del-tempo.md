# ADR-0002 — Modello del tempo e disponibilità point-in-time

Stato: Proposta · Data: 2026-10-20 · Rivisto: 2026-10-23 (contracts-v0.1)

## Contesto
Lookahead e timestamp ambigui sono la prima causa di backtest falsi. Nel vecchio
FxAgent l'offset del server MT5 era una configurazione statica
(`MT5_SERVER_TIMEZONE`, default `UTC`), mentre i server tipici usano UTC+2/+3 con DST.
La v0 definiva `available_at` solo per la singola `Bar`, non per la forma
colonnare né per le barre derivate.

## Decisione
- Solo datetime UTC timezone-aware; naive rifiutati dai modelli.
- Ogni osservazione, anche nella forma colonnare `BarSeries`, ha `open_time`
  (inclusivo), `close_time` (esclusivo), `available_at >= close_time` e
  `availability ∈ {OBSERVED, MODELLED}`.
- Storico: `available_at = close_time + publication_delay` (`MODELLED`, parametro
  del run). Runtime o registrazioni: istante di ricezione (`OBSERVED`).
- Barre derivate: `available_at = max` delle sorgenti; `OBSERVED` solo se tutte
  le sorgenti lo sono; `VALID` solo se tutte le sorgenti sono `VALID` e disponibili.
- Uno snapshot a `as_of` contiene solo barre con `available_at <= as_of`; il
  costruttore di `MarketSnapshot` riverifica l'invariante e fallisce se violata.
- Candidati con `available_at` ed `expires_at`.
- Dati ex post (outcome, fill, costi, causa di un dato mancante) mai input di `evaluate`.
- M3: l'offset del server MT5 viene rilevato confrontando il tempo dei tick con
  l'orologio UTC sincronizzato e verificato contro il calendario; un'incoerenza
  sospende le entrate. La configurazione può solo dichiarare l'offset atteso.

## Conseguenze
- Il test di "perturbazione del futuro" è possibile e obbligatorio.
- Revisioni di barre già osservate non sono supportate in v0.1 (conflitto).
