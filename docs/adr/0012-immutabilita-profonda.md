# ADR-0012 — Immutabilità profonda di snapshot, serie, parametri e JSON

Stato: Proposta · Data: 2026-10-23

## Contesto
`frozen=True` di Pydantic impedisce di riassegnare i campi ma non protegge
array NumPy, dict o liste contenuti. Un alias scrivibile permetterebbe al core di
modificare uno snapshot o a un chiamante di cambiarlo dopo l'hashing.

## Decisione
- `BarSeries` copia ogni colonna in un `bytes` proprio e la espone con
  `np.frombuffer`: array read-only che non può essere reso scrivibile. Gli
  array in ingresso sono sempre copiati, mai adottati. Le viste restano read-only;
  il core usa `.copy()` esplicito per array di lavoro.
- Al posto dei `Mapping` mutabili: `FrozenMap` (copia privata, nessun metodo di
  scrittura, hashable, ordine canonico). Dati liberi: `FrozenJson` (dict →
  FrozenMap, list → tuple, niente NaN/inf).
- I campi dei `Model` ammettono solo tipi immutabili; un test di architettura lo verifica.
- Nei test, gli hash di snapshot, config e stato sono ricalcolati dopo ogni
  `evaluate` e devono coincidere.
- Nessuna nuova dipendenza.

## Conseguenze
- Una copia per colonna alla costruzione delle serie (costo accettato).
- La protezione vale contro errori e alias; codice deliberatamente ostile (es.
  `ctypes`) è fuori dal modello di minaccia.
