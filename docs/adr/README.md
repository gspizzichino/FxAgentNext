# Registro delle decisioni architetturali (ADR)

Formato: contesto, decisione, conseguenze. Stato `Proposta` finché M0 non è
approvata, poi `Accettata`. Una decisione si cambia solo con un nuovo ADR che
sostituisce il precedente.

| ADR | Titolo | Stato |
|---|---|---|
| [0001](0001-modular-monolith.md) | Modular monolith con core puro e regola di dipendenza | Proposta |
| [0002](0002-modello-del-tempo.md) | Modello del tempo e disponibilità point-in-time | Proposta |
| [0003](0003-unica-implementazione-strategia.md) | Una sola implementazione della strategia e contratto `evaluate` | Proposta |
| [0004](0004-storage.md) | Parquet/DuckDB per dataset e audit, SQLite per lo stato operativo | Proposta |
| [0005](0005-base-di-prezzo.md) | Base di prezzo esplicita e nessuna quota fabbricata | Proposta |
| [0006](0006-baseline-donchian.md) | Baseline Donchian breakout H1 | Proposta |
| [0007](0007-execution-esito-ignoto.md) | Outbox degli intenti e nessun retry su esito ignoto | Proposta (implementazione M3) |
| [0008](0008-isolamento-ai.md) | AI di ricerca isolata dall'operatività | Proposta (implementazione M4) |
| [0009](0009-allineamento-timeframe.md) | Timeframe superiori derivati da M15 e allineati alla chiusura di New York | Proposta |
| [0010](0010-identita-e-versioning.md) | Identità basate su hash e versioning dei contratti | Proposta |
| [0011](0011-valutazione-guidata-dal-calendario.md) | Valutazione guidata dal calendario, non dalla presenza dei dati | Proposta |
