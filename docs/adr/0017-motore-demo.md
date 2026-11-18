# ADR-0017: Motore sul conto demo (M3)

Stato: Proposta (18 novembre 2026)

## Contesto

Giacomo ha scelto di far girare il motore sul conto demo MT5 di Naga con ordini
automatici: baseline Donchian H1 sulle tre coppie, 0,01 lotti, limiti −5 R al
giorno e −10 R a settimana, comandi Telegram solo `/stop` e `/stato`, prova di
4 settimane (`/mnt/project-files/m3/piano-demo-naga.md`). Nessuna strategia ha
superato la validazione: lo scopo è misurare l'esecuzione vera, non guadagnare.

## Decisione

1. **Solo demo, per costruzione.** Il pacchetto `fxnext.execution` non ha una
   modalità reale. `demo_guard` gira prima di ogni ordine e di ogni chiusura e
   richiede `ACCOUNT_TRADE_MODE_DEMO`, server e login in allowlist e valuta del
   conto attesa. Il primo conto visto viene legato al database: un cambio di conto
   sospende le entrate. Su un conto non demo il motore non tocca nulla, nemmeno le
   proprie posizioni.
2. **Stessa strategia della ricerca.** Candele M15 BID dal terminale →
   `Dataset` in memoria → lo stesso `SnapshotBuilder` e lo stesso `evaluate()`
   della ricerca. Le candele del broker sono la fonte del segnale in demo (non
   Dukascopy): il confronto tra le due va nel report di fine prova.
3. **Un solo processo, un ciclo ogni 5 s** con parti isolate (comandi, conto,
   orario server, riconciliazione, protezione, limiti, segnali): un'eccezione in
   una parte non salta le altre. La protezione ha un proprio heartbeat ma non un
   thread separato, perché il pacchetto `MetaTrader5` non è sicuro tra thread;
   stop e target sono comunque sul server del broker.
4. **Outbox e esito ignoto (ADR-0007).** Intento persistito prima dell'invio;
   `SUBMITTING` al riavvio diventa `UNKNOWN`; un `UNKNOWN` blocca il simbolo, si
   risolve solo cercando posizione o deal con il nostro `comment`, non si reinvia
   mai. Un intento per evento e conto (vincolo SQLite).
5. **Uscita a tempo** dopo 120 slot H1 come nel simulatore; stop e target
   arrotondati solo verso l'entrata.
6. **Deviazioni dal backtest dichiarate:** nessuna entrata nei 15 minuti attorno
   alle 17:00 di New York, spread massimo per coppia, tick non più vecchio di 60 s,
   una posizione per coppia e al massimo 3 in tutto. I segnali scartati per questi
   motivi restano registrati (`BLOCKED`) e contano nel report.
7. **Comandi tracciati.** Dashboard, Telegram e terminale scrivono solo una riga
   nella coda `commands`; il motore la applica e registra l'esito. Da Telegram si
   può solo fermare (e chiedere lo stato): riattivare richiede la dashboard o il
   terminale. Nessun comando apre o chiude trade.
8. **Magic proprio** (default 26111801), mai 260828 del vecchio FxAgent: le
   posizioni con altro magic sono "estranee", contate e mai toccate.

## Conseguenze

- I test T17 coprono il motore contro `SimulatedBroker` (risposta persa, errore
  prima dell'esecuzione, crash tra persistenza e invio, rifiuto, fill parziale,
  disconnessione, cambio conto, conto reale, posizione orfana, stop mancante,
  limiti, kill switch, uscita a tempo) e l'adattatore MT5 con un modulo finto.
- L'adattatore reale si prova solo sul PC: prima in sola lettura
  (`demo check`, `demo run --dry-run`), poi con ordini dopo l'ok di Giacomo.
- Il motore fermo non può avvisare da solo: `demo watchdog` va pianificato
  nell'Utilità di pianificazione di Windows.
