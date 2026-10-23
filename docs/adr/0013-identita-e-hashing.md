# ADR-0013 — Identità separate e schema di hash `fxnext-hash-v1`

Stato: Proposta · Data: 2026-10-23 · Sostituisce: ADR-0010

## Contesto
ADR-0010 usava un unico `evaluation_id`, non distingueva evento e candidato e
arrotondava i float alla precisione dello strumento, con il rischio di rendere
uguali dati diversi.

## Decisione
- `content_hash(tag, obj) = sha256("fxnext-hash-v1\0" + tag + "\0" + canon(obj))`,
  con `tag` = tipo + versione di schema del tipo (separazione di dominio).
- `canon`: JSON compatto, chiavi ordinate per byte UTF-8, tutti i campi presenti,
  float come `float.hex` (64 bit esatti, `-0.0` → `0.0`, NaN/inf rifiutati),
  datetime come ns UTC interi, frozenset ordinati, stringhe NFC.
- Identità distinte: `request_id` (domanda), `event_id` (evento strutturale, con
  `discriminator` obbligatorio e solo parametri di rilevazione),
  `candidate_id` (evento + config + livelli), `snapshot_hash` /
  `evaluation_hash` / `dataset_id` (contenuto), `run_fingerprint` (input
  completi, incluso `code_version`), `output_hash` (catena incrementale).
- Identificativi sha256 completi; stesso identificativo con contenuto diverso
  → `IntegrityError` e arresto del run.
- Determinismo cross-platform ottenuto vincolando il calcolo hashato a
  operazioni IEEE correttamente arrotondate in ordine fissato (niente libm,
  riduzioni NumPy, parallelismo, float32), non arrotondando.
- Ogni modifica a canonicalizzazione o campi di un tipo incrementa la versione
  dello schema o del tipo; i dati esistenti non vengono reinterpretati.

## Conseguenze
- Vettori di test dello schema committati; regressione per hash su Linux e Windows.
- Gli indicatori futuri che richiedono funzioni trascendenti dovranno essere
  esclusi dagli hash o specificati con un'implementazione deterministica propria.
