# ADR-0018: AI in ombra (M7)

Stato: Proposta (18 novembre 2026)

## Contesto

Un modello di linguaggio di oggi ha letto il web: su qualsiasi comunicato del
passato "sa" cosa è successo dopo, quindi un backtest dei suoi giudizi non è
onesto (ipotesi H5, `m6/shortlist-ipotesi.md`). L'unica prova pulita è in avanti.
Giacomo ha approvato la proposta `m7/proposta-ai-in-ombra.md` con quattro scelte:
fonti solo ufficiali (banche centrali, calendario, COT), Claude Opus 5.5 con tetto
di 10 $ al mese, un giudizio ogni mattina più uno dopo ogni decisione, criteri di
valutazione congelati (`docs/M7-valutazione-congelata.md`, sha256 `2dc06e84…5e26`).

## Decisione

1. **Nessun effetto sulle decisioni.** Il pacchetto `fxnext.ai` scrive solo il
   proprio archivio `data/ai/runs/`. import-linter vieta a motore, strategia,
   ricerca, stato operativo e archivio news di importarlo, e a `fxnext.ai` di
   importare motore, strategia e ricerca. La dashboard legge i file senza
   importare il pacchetto. Rimane valido ADR-0008: l'AI non scrive configurazioni
   né ordini.
2. **Punto nel tempo.** Il contesto usa solo record dell'archivio news ricevuti
   entro l'ora del giudizio, con lo sha256 dei byte letti. Ogni fonte ha un id
   (S1, S2…) e ogni giudizio cita gli id usati.
3. **Schema chiuso.** Risposta in JSON vincolato (structured outputs) e validata
   di nuovo dal codice: tono −2..+2, sorpresa solo dopo una decisione, rischio
   24 h, direzione a 5 giorni −1/0/+1, fiducia 0..1, motivazione in italiano. La
   variazione di tono la calcola il codice, non il modello.
4. **Serie congelata.** Prompt e schema hanno uno sha256 fissato da un test: un
   prompt nuovo è una versione nuova con una serie sua. Nessun fallback
   automatico verso un altro modello: un rifiuto è un'esecuzione fallita.
5. **Append-only, anche i fallimenti.** Una riga per esecuzione: `ok`, `failed`
   (giorno vuoto, con il motivo) o `skipped` (tetto di spesa). Nulla si riscrive.
6. **Tetto di spesa.** Prima di ogni chiamata si stima il costo massimo; se la
   spesa del mese più la stima supera il tetto, la chiamata non parte.
7. **Fonti delle banche centrali dal PC.** I feed di Fed, BCE, BoE e BoJ e i testi
   delle pagine di politica monetaria entrano nell'archivio news (`cb-feed-*`,
   `cb-text`), solo dagli host ufficiali.

## Conseguenze

- Lettura descrittiva a 6 mesi, verdetto a 12 con i criteri congelati. L'esito più
  probabile a 12 mesi è INCONCLUSIVE (circa 30 decisioni l'anno).
- Costo atteso circa 3–5 $ al mese; la chiave API sta solo nell'ambiente del PC.
- Gli URL dei feed vanno verificati sul PC alla prima esecuzione: dal cloud quei
  siti rispondono 403.
