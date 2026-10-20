# ADR-0001 — Modular monolith con core puro e regola di dipendenza

Stato: Proposta · Data: 2026-10-20

## Contesto
Il vecchio FxAgent distribuiva la logica tra worker, Position Manager in un
processo separato con IPC ZeroMQ volatile e una dashboard Streamlit che avviava
il worker. La specifica richiede un core deterministico senza dipendenze da
broker, database, UI o LLM e preferisce un modular monolith.

## Decisione
Un solo package `fxnext`. `fxnext.domain` e `fxnext.strategy` sono puri: niente
I/O, orologio di sistema, rete, DB, broker, LLM, UI. Tutti gli altri moduli
dipendono verso il core, mai il contrario. La regola è verificata in CI con
import-linter. Ogni comando è un processo CLI; nessun servizio o coda.

## Conseguenze
- Test del core velocissimi e senza fixture di infrastruttura.
- L'isolamento è logico: va difeso dai test di architettura.
- In M3 la protezione delle posizioni è un loop separato nello stesso processo
  (o un secondo comando) che usa il broker come verità, senza IPC di messaggi.
