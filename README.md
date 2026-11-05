# RankUp

Gestione candidature e graduatorie per bandi di docenza.

## Stato
Prototipo tecnico iniziale (non ancora idoneo alla gestione in produzione di dati personali).

### Funzioni implementate
- Add-on Home Assistant OS con accesso tramite Ingress.
- Dashboard chiara con menu laterale blu notte e bando attivo sempre evidenziato.
- Importazione PICA in formato Excel .xls/.xlsx con anteprima e conferma.
- Riconoscimento del codice bando dalla prima riga (es. "univr 26-02 - Esportazione dati").
- Ricostruzione di incarichi e candidature senza un secondo elenco degli incarichi.
- Conservazione dei dati in SQLite nello spazio persistente /data.
- Pagine Bandi, Candidature, Valutazioni, Verbali, Graduatorie, Archivio (alcune ancora segnaposto).

### Non ancora implementato
Importazione commissioni, generazione/lettura schede PDF, validazione delle commissioni, verbali, graduatorie, ruoli utente, export ZIP e cancellazione. Il controllo requisiti è stato rinviato su richiesta.

## Installazione su Home Assistant
Aggiungere questo repository in **Impostazioni > Componenti aggiuntivi > Negozio > Repository**, installare RankUp e aprire l'interfaccia Ingress. La build richiede accesso Internet per le dipendenze Python.

**Sicurezza:** usare solo dati fittizi in questa fase. Il repository non deve contenere file PICA, schede PDF o dati personali. Non esporre la porta interna dell'add-on direttamente su Internet.

## Sviluppo
Vedere i file in rankup/app e il piano di sviluppo nel file ROADMAP.md.
