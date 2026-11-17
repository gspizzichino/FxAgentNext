"""Pre-registered hypotheses S0-S3 and S7 (frozen 2026-11-11, docs/M4-ipotesi.md).

These hypotheses read bars built directly from the M1 BID+ASK price path, the
same content-addressed paths used by the M2 development reports. They do not go
through the audited M1 replay engine, which knows only the baseline strategy:
the guarantees here are the path hash, look-ahead tests and the reconciliation
of every candidate with its outcome.
"""
