# Ipotesi S0–S3 e S7: implementazione

Stato: **bozza per la PR**, 2026-11-17. Nessun run su dati reali.

Le regole vengono dal testo congelato l'11/11/2026
(`prereg-S0-S3-S7-congelato-2026-11-11.pdf`, sha256 `589bd00a…`). I file TOML in
`prereg/` lo traducono senza cambiarne le regole. Da qui in poi una modifica a
un TOML è una nuova pre-registrazione con un nuovo `experiment_id`.

## Come gira

```powershell
uv run fxnext hypothesis prereg/s0-vr-dev-001.toml
uv run fxnext hypothesis prereg/s7-intraday-dev-001.toml
uv run fxnext hypothesis prereg/s1-failure-dev-001.toml
uv run fxnext hypothesis prereg/s3-sungap-dev-001.toml
uv run fxnext hypothesis prereg/s2-dtrend-dev-001.toml
```

Il risultato va in `research/<fingerprint>/`: `report.md`, `report.json`,
`outcomes.parquet` (un esito per candidato, variante e stream) e, per S3,
`weekends.parquet` con tutti i weekend e il motivo delle esclusioni.

- **Split**: solo sviluppo. Il formato rifiuta qualsiasi altro split: la
  validazione di un'ipotesi richiede una nuova pre-registrazione e la tua
  approvazione, ipotesi per ipotesi.
- **Dati**: gli stessi percorsi M1 BID+ASK dei report `eurusd-dev-001`,
  `gbpusd-dev-001` e `usdjpy-dev-001`, indicati per hash completo.
- **Fasi descrittive**: S7 e S3 eseguono prima la fase 1. Le varianti che non la
  superano non girano; il report le elenca come non eseguite.

## Architettura

Il motore M1 con audit conosce solo la baseline Donchian e il formato dei suoi
contratti non ha barre D1 né orari di Londra. Le ipotesi quindi costruiscono le
barre direttamente dal percorso M1 (`src/fxnext/research/hyp/`) e usano il
simulatore M2 senza modifiche alle regole di esecuzione e costo. Le garanzie al
posto dell'audit del motore sono:

- l'hash del percorso e della pre-registrazione nel fingerprint;
- un test che altera i prezzi dopo un istante e verifica che i segnali prima di
  quell'istante non cambino (S1, S2, S7);
- la stessa riconciliazione della M2: eseguiti + controfattuali + non entrati =
  candidati, altrimenti nessun report.

Aggiunte al simulatore, che non toccano la baseline: un'uscita programmata
(`exit_at_ns`, semantica TIME: chiusura dell'ultimo minuto prima dell'istante) e
un'unità di rischio esplicita per il test dell'ingresso a tempo puro.

## Letture del testo congelato da confermare

Dove il testo non fissa un dettaglio, ho scelto la lettura qui sotto prima di
qualsiasi risultato. Ognuna è nel codice e nei test.

1. **Barre con buchi** (S0, S7): una barra o una sessione con anche un solo
   minuto mancante è esclusa. Le strategie S1 e S2 chiedono invece, come la
   baseline, che ogni barra della finestra abbia dati.
2. **Finestra di rollover** (S0): l'ora 17:00–18:00 di New York. Nella riga
   "tutte le ore" le somme di q rendimenti la saltano invece di fermarsi, se no
   VR(24) su H1 non si potrebbe mai calcolare. Per sessione VR(24) su H1 non sta
   dentro nessuna sessione e risulta "non calcolabile".
3. **Test dell'ingresso**: N = 10 barre del timeframe del segnale (10 giorni per
   S2, 10 ore per S3). Il "1 R simmetrico" tiene l'uscita a tempo del sistema.
   Si misura sul flusso segnale, al lordo, su tutti i candidati.
4. **Regola del 52% di S1**: si applica per variante. Una variante sotto il 52%
   non può essere la migliore; se nessuna lo supera, S1 si chiude.
5. **Riconciliazione di S1 con la baseline**: il testo dice M15, ma la baseline
   è su H1. Il report dà il conteggio per ogni variante sul suo timeframe, con la
   regola della baseline (chiusura oltre il canale) applicata alle stesse barre.
6. **Un livello, un segnale** (S1): il livello è identificato dal valore del
   massimo (o minimo) e dalla barra che lo ha fatto.
7. **random_time di S1**: lo stop casuale è la mediana di R/ATR dei candidati
   della variante (solo geometria, nessun esito).
8. **Una posizione per coppia nei benchmark**: ogni ripetizione applica la
   regola per coppia e poi mette insieme le tre coppie.
9. **Uscita a canale di S2**: alla chiusura del giorno sotto il minimo di M
   giorni si esce al primo minuto del giorno dopo. Il segnale opposto che chiude
   la posizione non la inverte: il suo ingresso cade nello stesso minuto
   dell'uscita e finisce tra i controfattuali.
10. **S3, prezzi C e O**: C è il mid dell'ultimo minuto con tick nei 30 minuti
    prima della chiusura del venerdì; O il mid del primo minuto con tick nei 30
    minuti dopo la riapertura. Se manca uno dei due il weekend è escluso e
    contato. La fase 1 decide per ogni soglia k sul gruppo delle tre coppie.
11. **S3, benchmark**: il testo esclude random_direction e chiede il confronto
    con il random walk della fase 1. La correzione max-of-K non ha quindi una
    distribuzione casuale e risulta "n/d".
12. **S7, passaggio alla fase 2**: A gira se passa la gamba di casa, B se passa
    New York, C se passano entrambe (2 coppie su 3 con segno previsto e IC che
    esclude lo zero).
13. **S7, regola 5** ("nessun ingresso nei giorni con buchi nella sessione"): si
    applica come esclusione per qualità dei dati, anche se il buco si vede solo a
    sessione finita.
14. **Altopiano con meno di 8 varianti**: per S7 (3 varianti) servono
    ceil(5/8 × 3) = 2 varianti positive.
15. **Tassi per lo swap di S2**: confrontati con le serie ufficiali il
    2026-11-17, prima del run di S2 (sezione 16 della specifica M2).
16. **VR su D1** (correzione del 2026-11-17 dopo il primo run di S0, che non
    cambia regole né soglie): il weekend spezzava ogni sequenza a quattro
    rendimenti, quindi VR(5) e VR(20) non erano mai calcolabili. Ora il rendimento
    che attraversa il weekend resta escluso, ma le somme di q giorni lo saltano,
    come l'ora di rollover nelle barre intraday. Le celle M15 e H1 non cambiano.

## Cosa decide la soglia

Per ogni ipotesi con trade il report calcola i controlli della soglia per
proporre la validazione: netto ×1 > 0, sopra il 95° percentile del massimo
casuale su K varianti, positiva su almeno 2 coppie su 3, altopiano di 5 varianti
su 8 con netto positivo, e per S1 il test dell'ingresso. Esito
`PROPOSE_VALIDATION` o `CLOSE_ON_DEV`. Per S2 si giudica solo la variante
principale 55/2/20. S2 e S3 sono esplorative: il loro esito massimo in
validazione sarebbe INCONCLUSIVE con segno positivo. La correzione vale dentro
ogni ipotesi, non tra le ipotesi.

## Tempi stimati

Su un anno sintetico: S0, S3 e S7 in pochi secondi, S2 circa 10 secondi, S1
circa 3,5 minuti (le varianti M15 pesano di più per il benchmark random_time).
Sui sei anni reali S1 dovrebbe stare intorno ai 20–25 minuti.
