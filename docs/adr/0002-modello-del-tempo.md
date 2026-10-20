# ADR-0002 — Modello del tempo e disponibilità point-in-time

Stato: Proposta · Data: 2026-10-20

## Contesto
Lookahead e timestamp ambigui sono la prima causa di backtest falsi. Nel vecchio
FxAgent l'offset del server MT5 era una configurazione statica
(`MT5_SERVER_TIMEZONE`, default `UTC`), mentre i server tipici usano UTC+2/+3 con DST.

## Decisione
- Solo datetime UTC timezone-aware; naive rifiutati dai modelli.
- Ogni barra ha `open_time` (inclusivo), `close_time` (esclusivo) e
  `available_at >= close_time`.
- Uno snapshot a `as_of` contiene solo barre con `available_at <= as_of`; le
  barre in formazione non entrano mai nel core.
- Candidati con `available_at` ed `expires_at`.
- Dati ex post (outcome, fill, costi) in strutture separate, mai input di `evaluate`.
- M3: l'offset del server MT5 viene rilevato confrontando il tempo dei tick con
  l'orologio UTC sincronizzato e verificato contro il calendario; un'incoerenza
  sospende le entrate. La configurazione può solo dichiarare l'offset atteso.

## Conseguenze
- Il test di "perturbazione del futuro" diventa possibile e obbligatorio.
- `publication_delay` per i dati storici è un parametro registrato nel run.
