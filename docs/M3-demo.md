# M3: il motore sul conto demo Naga

Piano e decisioni: `/mnt/project-files/m3/piano-demo-naga.md` (18 novembre 2026).
Architettura e scelte: [ADR-0017](adr/0017-motore-demo.md).

## Cosa fa

Ogni ora, appena chiude una candela H1, valuta EURUSD, GBPUSD e USDJPY con la
baseline Donchian 20 / 1,5 ATR / 2 R (stesso codice della ricerca, candele del
terminale Naga) e, se c'è un segnale e nulla lo blocca, apre 0,01 lotti con stop
e target sul server del broker. Chiude a tempo dopo 120 ore di mercato. Ordini
**solo sul conto demo**: prima di ogni ordine controlla che il terminale riporti
un conto demo con server e login in elenco.

## Sul PC (MSYS2 bash, cartella `~/FxAgentNext`)

```bash
git fetch origin && git checkout <branch o main dopo il merge>
uv sync
uv pip install MetaTrader5           # solo sul PC Windows
uv run fxnext demo init-config       # scrive demo.toml (ignorato da git)
# in demo.toml: [account] logins = [il tuo login demo]
```

Il terminale MT5 Naga deve essere aperto, connesso al conto demo e con
"Algo Trading" attivo.

1. **Controllo in sola lettura** (nessun ordine):
   `uv run fxnext demo check` stampa conto, esito del controllo demo, simboli,
   spread, orario del server, posizioni estranee e la valutazione dell'ultima ora.
2. **Uno o due giorni in sola lettura**: `uv run fxnext demo run --dry-run`.
   Il motore gira per intero ma non invia nulla (database separato
   `data/ops/demo-sola-lettura.sqlite`).
3. **Avvio con ordini**, solo dopo l'ok di Giacomo: `uv run fxnext demo run`.

Avvisi Telegram con le variabili già usate da `notify-test`
(`FXNEXT_TELEGRAM_TOKEN`, `FXNEXT_TELEGRAM_CHAT_ID`). Da Telegram: `/stop`
ferma le nuove entrate, `/stato` risponde con lo stato.

Dashboard: `uv run fxnext dashboard --host 192.168.1.99` → pagine *Oggi (demo)*,
*Posizioni e trade*, *Segnali e decisioni*. Da *Oggi* si attiva o toglie il kill
switch e si riprende dopo il limite settimanale o una sospensione.

Comandi da terminale: `demo status`, `demo stop`, `demo resume`,
`demo resume-week`, `demo clear-suspension`.

Sorveglianza: pianificare ogni 10 minuti `uv run fxnext demo watchdog`
(Utilità di pianificazione di Windows): se il motore non gira da più di 10
minuti manda un avviso Telegram.

## Quando le entrate si fermano

| Motivo | Si riprende |
|---|---|
| Kill switch (dashboard, Telegram `/stop`, `demo stop`) | dashboard o `demo resume` |
| Limite giornaliero −5 R (trade chiusi dalle 17:00 di New York) | da solo, al giorno di trading successivo |
| Limite settimanale −10 R (da domenica 17:00 New York) | dashboard o `demo resume-week` |
| Sospensione: conto non demo o cambiato, orario server cambiato, posizione senza stop, posizione sparita | dopo verifica, dashboard o `demo clear-suspension` |
| MT5 non raggiungibile | da solo, alla riconnessione |
| Ordine con esito ignoto su una coppia | da solo, quando la riconciliazione decide |

In ogni caso le posizioni aperte restano protette da stop e target sul broker.

## Report dopo 4 settimane

Dal database `data/ops/demo.sqlite`: slippage in entrata e in uscita, spread
pagato, rifiuti e esiti ignoti, swap addebitati, R per trade contro il
simulatore sulle stesse ore, segnali scartati dai controlli e motivo.
