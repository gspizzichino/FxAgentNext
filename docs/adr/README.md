# Registro delle decisioni architetturali (ADR)

Formato: contesto, decisione, conseguenze. Stato `Proposta` finché M0 non è
approvata, poi `Accettata`. Prima dell'accettazione un ADR può essere precisato in place ("rivista v0.1");
una decisione incompatibile viene sostituita da un nuovo ADR. Dopo
l'accettazione si cambia solo con un nuovo ADR.

| ADR | Titolo | Stato |
|---|---|---|
| [0001](0001-modular-monolith.md) | Modular monolith con core puro e regola di dipendenza | Proposta |
| [0002](0002-modello-del-tempo.md) | Modello del tempo e disponibilità point-in-time | Proposta (rivista v0.1) |
| [0003](0003-unica-implementazione-strategia.md) | Una sola implementazione della strategia e contratto `evaluate` | Proposta |
| [0004](0004-storage.md) | Parquet/DuckDB per dataset e audit, SQLite per lo stato operativo | Proposta |
| [0005](0005-base-di-prezzo.md) | Base di prezzo esplicita, stati di barra distinti, nessuna quota fabbricata | Proposta (rivista v0.1) |
| [0006](0006-baseline-donchian.md) | Baseline Donchian breakout H1 | Proposta (rivista v0.1) |
| [0007](0007-execution-esito-ignoto.md) | Outbox degli intenti e nessun retry su esito ignoto | Proposta (implementazione M3) |
| [0008](0008-isolamento-ai.md) | AI di ricerca isolata dall'operatività | Proposta (implementazione M4) |
| [0009](0009-allineamento-timeframe.md) | Timeframe superiori derivati da M15 e allineati alla chiusura di New York | Proposta |
| [0010](0010-identita-e-versioning.md) | Identità basate su hash e versioning dei contratti | Sostituita da 0013 |
| [0011](0011-valutazione-guidata-dal-calendario.md) | Valutazione guidata dal calendario, non dalla presenza dei dati | Proposta (rivista v0.1) |
| [0012](0012-immutabilita-profonda.md) | Immutabilità profonda di snapshot, serie, parametri e JSON | Proposta |
| [0013](0013-identita-e-hashing.md) | Identità separate e schema di hash `fxnext-hash-v1` | Proposta |
| [0014](0014-audit-crash-safe.md) | Protocollo crash-safe per audit Parquet e checkpoint | Proposta |
| [0015](0015-downloader-dukascopy.md) | Downloader Dukascopy con cache grezza immutabile | Proposta |
