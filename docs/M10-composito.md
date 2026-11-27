# M10: filtro storico `l1-composito-001` (momentum economico + value, 1976–2003)

Testo congelato: `docs/M10-prereg-l1-composito-001-congelato.md` (sha256 `027e3322…`, congelato
il 26/11/2026 con l'ok di Giacomo). TOML: `prereg/l1-composito-001.toml`. Codice:
`src/fxnext/research/composite/`. Test: `tests/test_t20_l1_composite.py`.

## Comandi (dal PC, FRED e Bundesbank sono bloccati dal cloud)

```bash
fxnext l1-download                       # data/l1, solo osservazioni fino al 31/12/2003
fxnext l1-check prereg/l1-composito-001.toml   # copertura, nessun rendimento
fxnext l1-run   prereg/l1-composito-001.toml   # una sola volta: un secondo run è rifiutato
```

`l1-download` si può ripetere: salta le serie già scaricate o inesistenti (404 = NOT_FOUND) e
riprende dopo un blocco per frequenza di FRED.

## Scelte implementative (non decise nel testo congelato, fissate qui prima dei dati)

1. **Nessun dato dopo il 2003 sul disco.** Le richieste a FRED hanno `coed=2003-12-31`; un file
   con osservazioni successive è CORRUPT al download e rifiutato al caricamento.
2. **Marco tedesco.** FRED non ha il DEM giornaliero. Fonte: Bundesbank `BBEX3.D.USD.DEM.AA.AC.000`
   (fixing di Francoforte, circa le 13 di Francoforte, contro mezzogiorno di New York per le altre
   valute). Fino a dicembre 1998 USD per EUR-equivalente = 1,95583 / (DEM per USD); da gennaio
   1999 `DEXUSEU`.
3. **Fine mese** = ultima quotazione disponibile del mese.
4. **Serie macro.** Per ogni paese e variabile ci sono candidati FRED in ordine fisso (mensile
   prima del trimestrale); si usa quello con la prima osservazione più vecchia, a parità il primo
   della lista. La scelta dipende solo dalle date, non dai valori, ed è riportata da `l1-check`.
5. **Ritardi.** Dato mensile del mese m noto a fine m+2; dato trimestrale del trimestre che inizia
   in m noto a fine m+5 (fine del trimestre successivo).
6. **Variabile disponibile** per una valuta solo se tutte e tre le crescite (12, 36, 60 mesi) si
   possono calcolare. La crescita è fra l'ultima osservazione nota e quella di 12/36/60 mesi prima.
   La disoccupazione entra come variazione log del tasso, con segno negativo.
7. **Ranghi** centrati e scalati in [−1, 1] (pareggi al rango medio), così variabili con un numero
   diverso di valute pesano uguale.
8. **Value**: cambio reale = log(USD per valuta) + log CPI estero − log CPI USA, CPI con ritardo;
   segnale = −(variazione a 60 mesi).
9. **Tassi**: rendimento in eccesso con il differenziale a 3 mesi del mese t−1 (noto a fine t, come
   M5), diviso 12. EUR usa il tasso tedesco a 3 mesi (coerente con i dati macro tedeschi).
   Sostituti decisi: CD a 3 mesi per lo yen fino a marzo 2002, call money svizzero fino a giugno
   1999.
10. **Valute ammesse in un mese**: segnale A, segnale B, cambio a inizio e fine mese, tassi della
    valuta e USA. Il test parte dal primo mese con almeno 7 valute; se parte dopo dicembre 1985
    `l1-run` si ferma. Dopo l'inizio i mesi con meno di 7 valute restano nel test (con almeno 2) e
    sono elencati nel controllo.
11. **Costi Naga** (profilo `naga-demo-2026-11-18`): ricarico per valuta alle quotazioni di
    riferimento del profilo, applicato ogni mese al peso assoluto; spread = mediana storica
    2015–2023 dove esiste (EURUSD, GBPUSD, USDJPY), altrimenti mediana delle letture live; si paga
    metà spread sul cambio di peso (pesi non aggiornati per la deriva del mese). DEM usa i costi EUR.
12. **Benchmark casuale**: 1.000 portafogli con ranghi casuali sulle stesse valute ammesse ogni mese,
    stessi pesi e costi ×1; criterio = media netta sopra il 95° percentile delle medie.
13. **Metà del periodo**: i mesi di tenuta divisi in due parti uguali (la seconda ha un mese in più se
    il numero è dispari).
14. **t di Newey-West** con 6 ritardi, pesi di Bartlett, sulla serie mensile netta.
15. **Certificati (27/11/2026).** Sul PC di Giacomo il primo download della Bundesbank è fallito
    con `CERTIFICATE_VERIFY_FAILED` (Python non completa la catena che il browser accetta).
    `l1-download` verifica ora TLS con il certificate store del sistema operativo (`truststore`).
    La verifica resta attiva. Le serie già scaricate (OK o NOT_FOUND) sono saltate, le FAILED
    vengono ritentate.
