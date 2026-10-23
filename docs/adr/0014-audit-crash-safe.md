# ADR-0014 — Protocollo crash-safe per audit Parquet e checkpoint

Stato: Proposta · Data: 2026-10-23 · Precisa: ADR-0004

## Contesto
Parquet non supporta append in place; un crash può lasciare file parziali o
segmenti scritti ma non registrati. Serve una ripresa che dia lo stesso
`output_hash` del run ininterrotto, senza duplicati, e un comportamento definito
per due replay sulla stessa destinazione.

## Decisione
- Destinazione `runs/<run_fingerprint>/` con `LOCK`, `manifest.json`,
  `segments/`, `quarantine/`.
- Lock esclusivo del sistema operativo (`fcntl.flock` / `msvcrt.locking`,
  stdlib), rilasciato alla morte del processo; il secondo processo fallisce
  subito con `DestinationLocked`.
- Segmenti mensili per `as_of`: scrittura su `.tmp`, `fsync`, `sha256`,
  `os.replace` sul nome finale, `fsync` della directory dove possibile.
- **Commit** = `os.replace` atomico di `manifest.json` che elenca segmenti con
  `seq`, checksum, conteggi, intervalli e `chain_head`. Ciò che non è nel
  manifest non esiste per i lettori.
- Recupero: verifica di checksum e catena dei segmenti elencati (errore →
  arresto, nessuna riparazione automatica), quarantena di `.tmp` e segmenti non
  elencati, ripresa dal primo slot dopo `last_committed_as_of` con la catena ripristinata.
- Lettori senza lock, solo dal manifest. Destinazione `COMPLETED` in sola lettura.

## Conseguenze
- Nessun nuovo componente infrastrutturale.
- Su Windows manca il `fsync` della directory: un crash del sistema operativo può
  perdere l'ultimo commit, che verrà ricalcolato alla ripresa senza effetti sugli hash.
