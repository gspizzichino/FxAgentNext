# M7: AI in ombra (guida per il PC)

Ogni mattina, dal lunedì al venerdì, l'AI scrive un giudizio datato su EUR, GBP, JPY
e USD. Dopo ogni decisione di Fed, BCE, BoE o BoJ ne scrive un altro sulla valuta
interessata. Il motore demo non lo legge mai (ADR-0018). Lo vedi nella pagina
**Oggi** della dashboard e su Telegram.

## Una volta sola

1. Installa il pacchetto opzionale: `uv sync --extra ai` (in un ambiente che non sia
   bloccato dal download in corso).
2. Crea una chiave su console.anthropic.com e imposta un tetto di spesa anche lì.
   Mettila nelle variabili d'ambiente dell'utente Windows come `ANTHROPIC_API_KEY`.
   Mai in un file del repository.
3. Verifica i feed delle banche centrali:
   `uv run fxnext news-archive --source centralbanks`. Ogni banca deve rispondere
   `saved N new` o `unchanged`. Un `error: HTTP 404` vuol dire che l'indirizzo del
   feed in `src/fxnext/news/centralbanks.py` va corretto.
4. Guarda cosa leggerebbe il modello, senza chiamarlo e senza spendere:
   `uv run fxnext ai preview`.

## Attività pianificate (Utilità di pianificazione)

| Quando | Comando |
|---|---|
| ogni 30 minuti (già attivo) | `fxnext news-archive`: ora include anche le banche centrali |
| lun–ven alle 07:30 | `fxnext ai daily` |
| ogni 30 minuti, dopo l'archivio | `fxnext ai events` (chiama il modello solo se c'è una decisione nuova) |

Usa gli stessi `--news` della dashboard e `--out data/ai`; la dashboard prende
`--ai data/ai`.

## Controllo

- `uv run fxnext ai status`: ultime esecuzioni e spesa del mese.
- Tetto: 10 $ al mese (`--cap-usd`). Oltre il tetto l'esecuzione viene registrata
  come `skipped` e non chiama il modello.
- Una giornata fallita resta registrata come `failed` con il motivo. Non si riscrive.

## Valutazione

I criteri sono congelati in `docs/M7-valutazione-congelata.md` (sha256
`2dc06e844d9d94f4cdfb971622684adbb16111e66dc4120aa7fa954431175e26`). Lettura
descrittiva a 6 mesi, verdetto a 12 mesi. L'esito più probabile è INCONCLUSIVE.
