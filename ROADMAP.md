# Roadmap RankUP

> **Stato di riferimento del repository: v0.9.25 (`rankup/config.yaml`).** Le caselle indicano disponibilità nel codice, **non** collaudo in produzione né installazione sul Raspberry. Le note delle versioni precedenti sono mantenute più sotto come cronologia.

## Come leggere lo stato

- **[x] Implementato:** la funzione o il controllo è presente nel repository.
- **Test automatici:** verificano casi sintetici e regressioni su GitHub Actions; non certificano l'adeguatezza amministrativa dei risultati.
- **[ ] Da verificare / collaudare:** prova manuale, requisito incompleto oppure funzionalità futura.
- **Collaudo HAOS e documentale:** richiede verifica su un'installazione Home Assistant e, quando pertinenti, confronto delle schede con i modelli e gli atti ufficiali.

## Stato attuale — repository 0.9.25

| Area | Realizzato nel codice | Resta da verificare o completare |
| --- | --- | --- |
| PICA / incarichi | Import Excel, ricostruzione incarichi, filtri e riferimenti del bando; controlli XLSX | Prova con le fonti ufficiali e dati di un bando reale su HAOS |
| Interfaccia Valutazioni | Fase 1: KPI, filtri, import PDF, avanzamento e gruppi CdS accessibili senza cambiamenti alle API | Verificare visivamente su HAOS e decidere eventuale Fase 2 master-detail |
| Interfaccia altre sezioni | Fase 1 globale: design system, guida fasi, tabelle/form accessibili, ricerca incarichi/candidature, riepiloghi e stati operativi | Collaudo visuale e funzionale in Home Assistant e attraverso tunnel; eventuale Fase 2 avanzata |
| Rinunce e ritiri | Admin registra stato e riferimento documentale per singola candidatura; le schede non sono più richieste per posizioni non attive | Scorrimenti dopo approvazione restano esterni a RankUP |
| Parità | Alert in Valutazioni e comando contestuale «Gestisci parità» nei Verbali; precedenze della Commissione tracciate | Collaudo interattivo e applicazione dei criteri effettivi delle singole procedure |
| Prospetto interno | PDF con tutti i nominativi ricavato dallo snapshot approvato, accesso Admin e audit | Verifica documentale dell'uso e della distribuzione interna |
| Commissioni | Gestione per CdS/sede, catalogo globale conservato e importazione incrementale | Correttezza delle associazioni e delle nuove designazioni, senza propagazione implicita |
| Schede PDF | Tre modelli generati, massimali centralizzati, anteprima, acquisizione/nuove versioni e audit | Collaudo interattivo dei PDF su Acrobat/macOS e verifica amministrativa dei modelli |
| Graduatorie / verbali | Graduatorie CdS e generale, approvazioni, regressioni e archivio delle versioni | Controllo umano degli atti generati e gestione delle firme esterne al programma |
| Sicurezza | Account Admin/operatori, autorizzazioni, controlli ZIP/XLSX e operazioni concorrenti | Verifiche di sicurezza e conformità all'ambiente d'Ateneo |
| Persistenza | SQLite con connessioni chiuse, migrazione schema v1 e snapshot prima dell'upgrade | Collaudo aggiornamento e ripristino di emergenza sul Raspberry |
| Archiviazione bandi | ZIP finale con `manifest.json`, impronte SHA-256; PDF definitivo salvato obbligatorio e recupero degli storici documentato | **Ripristino completo automatizzato dello ZIP non implementato** |
| Home Assistant | Add-on, Ingress, endpoint salute e configurazione watchdog | Verifica reale del watchdog e del recupero dopo errori |
| Dipendenze | Versioni delle otto dipendenze dirette fissate | Lock completo con dipendenze transitive/hash e aggiornamenti di sicurezza |
| ZIP del **codice sorgente** | Test automatico di contenuto, integrità, percorsi e compatibilità dei nomi | Nessuna garanzia di avvio su Windows/macOS: il runtime previsto resta l'add-on Linux/HAOS |

**Collaudo CI:** verificare il risultato del workflow associato alla revisione corrente prima di distribuire questa versione.

## Priorità operative ancora aperte

- [ ] Collaudare la versione corrente su HAOS Raspberry Pi 5, con backup completo precedente l'aggiornamento.
- [ ] Verificare la correttezza delle schede PDF nei lettori effettivamente usati dalle commissioni e confrontare i modelli con i documenti approvati.
- [ ] Provare backup e recupero dati in un ambiente separato; definire una procedura di ripristino dello ZIP di un bando.
- [ ] Verificare permessi, accesso remoto, gestione credenziali e requisiti applicabili in Ateneo.
- [ ] Completare il lock delle dipendenze transitive per ottenere build pienamente riproducibili.
- [ ] Collaudare l'eventuale aggiornamento selettivo delle commissioni nei singoli bandi, sempre con conferma esplicita.

## Milestone iniziali — stato consolidato

### 1. Base tecnica
- [x] Add-on Home Assistant con Ingress e dashboard/selezione bando attivo.
- [x] Anteprima e importazione PICA con codice bando e incarichi.
- [x] Importazione delle commissioni dal formato XLSX.
- [ ] Collaudo integrale della versione attuale su HAOS.

### 2. Commissioni e schede
- [x] Catalogo commissioni e gestione delle composizioni per CdS/sede.
- [x] SSD PICA e scelta del modello ordinario, professionalizzante o Inglese.
- [x] Generazione e scaricamento schede compilabili, anche in ZIP per CdS/bando.
- [x] Acquisizione e sostituzione delle schede restituite con storico.
- [ ] Configurazione/caricamento di modelli PDF esterni versionati senza modifica del codice.

### 3. Valutazioni e graduatorie
- [x] Monitoraggio delle valutazioni per CdS/sede.
- [x] Verbali e graduatorie CdS; graduatoria generale e versioni approvate.
- [x] Rettifiche/regressioni di stato e tracciamento delle modifiche.
- [ ] Validazione amministrativa dei documenti e gestione delle firme fuori dall'applicazione.

### 4. Sicurezza, backup e portabilità
- [x] Accessi Admin/operatori, audit e controlli di concorrenza.
- [x] Archivio finale di bando verificato con manifest e impronte dei file.
- [x] Snapshot SQLite preliminare all'upgrade di schema.
- [x] Controllo CI della struttura dello ZIP sorgenti (distinto dallo ZIP dei bandi).
- [ ] Ripristino completo automatizzato da ZIP finale e verifica pratica di disaster recovery.
- [ ] Verifica di conformità e idoneità all'uso con dati personali in ambiente Ateneo.

## Cronologia delle versioni (storico)

Le sezioni seguenti documentano decisioni e attività al momento delle singole release. Un'attività allora ancora aperta può essere stata completata in una release successiva: **per lo stato attuale fa fede la tabella sopra**.

## Fix 0.3.1 — SSD PICA
- [x] Mappare la colonna `ssd` dell'incarico, senza confonderla con il settore dichiarato dal candidato.
- [x] Migrazione una tantum e audit degli SSD dei bandi precedenti.
- [x] Segnalare `NN` come SSD non specificato e impedire la classificazione automatica.
- [ ] Verifica funzionale su Home Assistant e conferma dei risultati del bando esistente.

## Milestone v0.4.0 — Anagrafica incarichi
- [x] Acquisire campi PICA I, K, L, P, Q, R, S, T, U, V, BU.
- [x] Salvare separatamente codice partizione, descrizione, Peso Copertura e Ore.
- [x] Preservare date originali e visualizzare date normalizzate.
- [x] Migrazione una tantum dei bandi esistenti dal PICA originale verificato, senza reimportazioni.
- [x] Audit delle integrazioni e indicazione dello stato nell'interfaccia Incarichi.
- [x] Test sintetici per importazione, coerenza campi e migrazione.
- [ ] Collaudo dell'aggiornamento reale su Home Assistant OS.

## 0.4.1 — Selezione modelli
- [x] Tutti gli SSD identificati usano in via predefinita il modello non professionalizzante.
- [x] Selezione manuale del modello professionalizzante con salvataggio immediato.
- [x] Nessuna fonte documentale obbligatoria per salvare la scelta operativa del modello.
- [x] Supporto nei percorsi API per SSD contenenti `/`.
- [x] Segnalazione distinta per SSD `NN` o non indicati.
- [ ] Collaudo del salvataggio sul Raspberry Pi e conferma dell'esito del CI.

## v0.4.2 — Valutazione professionalizzante
- [x] Modello professionale A4 su una sola pagina (non più due).
- [x] Riprodotte sei aree e massimali 15/10/15/7/3/10 (60 punti).
- [x] Menu originali per ruolo e attività integrativa per cinque annualità; subtotal annuale e totale PDF.
- [x] Requisiti dalla fonte commissioni o **[DA VERIFICARE]**; nessun dato personale del facsimile incorporato.
- [x] Sezioni note, esito e composizione commissione nella stessa pagina.
- [x] Test strutturali PDF aggiunti.
- [ ] Verifica funzionale interattiva in Adobe Acrobat Reader e revisione formale da parte dell'operatore.
- [ ] Modelli PDF caricabili e versionati, senza intervento sul codice.
- [ ] Revisione del modello **non professionalizzante** secondo il suo originale ufficiale.

## 0.4.3 — Scheda ordinaria e NN
- [x] Sostituito il precedente elenco generico con un modello ordinario A4 su una pagina.
- [x] Sei aree con massimali 15/10/15/10/3/7.
- [x] Campi P01-P15 e calcolo PDF del totale; punteggi iniziali 0.
- [x] Attività scientifica con cinque descrizioni e un punteggio unico P13.
- [x] SSD NN usa automaticamente il modello ordinario, senza classificazione o modifica del dato PICA.
- [x] Test di regressione aggiunti per NN, struttura ordinaria e scheda professionalizzante.
- [ ] Verifica funzionale su Home Assistant OS e prova del calcolo in Adobe Acrobat Reader.

## 0.4.4 — Commissioni e menu PDF
- [x] Associazione per CdS PICA normalizzato e sede coincidente.
- [x] Eliminazione della sola dicitura PICA abilitante nel confronto, non nei dati originali.
- [x] Bloccare associazioni ambigue o prive di commissione, senza inventare nominativi.
- [x] Riepilogo associazioni nella pagina Commissioni e nella pagina Schede PDF.
- [x] Etichette distinte nei menu con valori uguali 0,5/1,5 della scheda professionalizzante.
- [x] Test sintetici del confronto multi-sede, collisioni e struttura PDF.
- [ ] Test end-to-end su Home Assistant OS e verifica in Adobe Acrobat Reader.
- [ ] Integrazione puntuale delle commissioni mancanti nei singoli bandi.

## RankUp 0.5.0 — Commissioni e riferimenti del bando
- [x] Inserimento manuale delle commissioni con CdS/sede guidati da PICA.
- [x] Modifica delle commissioni registrate e controllo di modifiche concorrenti.
- [x] Audit delle variazioni senza modifiche agli altri bandi.
- [x] Download del format Excel vuoto, con CdS/sedi PICA o con commissioni già registrate.
- [x] Importazione incrementale delle nuove commissioni e aggiornamento massivo esplicito senza eliminare righe assenti o cancellare campi vuoti.
- [x] Repertorio, protocollo, data dell'avviso e anno accademico modificabili a livello di bando.
- [x] Collegamento dei riferimenti completi alle nuove schede PDF.
- [x] Test sintetici API, persistenza, importazione, audit e produzione PDF aggiunti.
- [ ] Verifica di GitHub Actions e collaudo manuale su HAOS con database esistente.
- [ ] Avviso persistente di invalidazione dei PDF già generati dopo variazioni.

## Milestone 0.6.0
- [x] Anteprima massiva PDF/ZIP e conferma distinta dall'acquisizione.
- [x] Riconoscimento candidato/assegnazione e controllo modello.
- [x] Calcolo indipendente e validazione massimali.
- [x] Conservazione delle schede anomale senza acquisirne i punteggi.
- [x] Duplicati e nuove versioni con rettifica esplicita.
- [x] Avanzamento CdS/sede riferito alle sole candidature attive.
- [x] Icona Dashboard, favicon, icone menu e tema scuro persistente.
- [x] Test sintetici di importazione e rettifica aggiunti.
- [ ] Collaudo completo Home Assistant OS e Adobe Acrobat.
- [x] Verbali e graduatorie (completati in versioni successive).
- [ ] Propagazione selettiva delle variazioni delle commissioni da collaudare.

- [x] 0.6.1: Download dell'originale PDF dal registro delle valutazioni, comprese le anomalie.

- [x] 0.6.2: Protezione contro i segnaposto del menu esito nelle valutazioni con totale zero, 18 test automatici superati.

- [x] 0.6.3: Nomi delle schede PDF con ID PICA + cognome + nome + chiave incarico, test di unicità e lunghezza.

- [x] v0.6.4: Diagnostica esplicita ZIP PDF trovati/analizzati/classificati, conferma bloccata se divergenti e test sintetico con 21 PDF.

## Versione 0.7.0 — Completato sul repository
- [x] Suddivisione Valutazioni per CdS e sede, con riepilogo e storico filtrati.
- [x] Distinzione fra esito idoneo, non idoneo ed escluso senza deduzioni dal punteggio.
- [x] Acquisizione automatica delle decisioni negative motivate senza punteggio fittizio.
- [x] Acquisizione manuale di schede da verificare con anagrafica PICA e verifica documentale.
- [x] Registro eventi e versione precedente conservata in caso di rettifica.
- [x] Esclusione documenti appartenenti a CdS/sedi diversi dal filtro selezionato.
- [x] Supporto agli ZIP contenenti metadati Finder/macOS.
- [x] 24 test automatici superati.
- [ ] Collaudo interattivo su Home Assistant OS e con schede realmente compilate.
- [x] Produzione di graduatorie e verbali sviluppata in release successive; resta necessaria la verifica dei criteri ufficiali.
- [x] Catalogo permanente delle commissioni introdotto in release successive; non implica validità automatica per nuovi bandi.

- [x] v0.7.1: Riclassifica tracciata delle acquisizioni storiche prive di decisione, totale preservato e visualizzato come non convalidato.
- [x] v0.7.1: Protezione contro l'inserimento manuale di idoneità/esclusioni senza esito né nota sul PDF editabile.
- [x] v0.7.1: Test di migrazione idempotente (caso sintetico 15 punti senza esito), 25 test automatici superati.
- [ ] Collaudo su dati già presenti in Home Assistant dopo backup.

- [x] v0.7.2: Acquisizione manuale esclusivamente Admin, con controllo della sessione nel backend e pulsante nascosto agli altri ruoli.
- [x] v0.7.2: Test di rifiuto HTTP 403 per utente non Admin autenticato senza modifica della valutazione.
- [x] Anagrafica utenti e ruoli multipli, realizzata nella release 0.8.0.

- [x] v0.7.3: Riferimenti dell'avviso trasferiti alla pagina autonoma della procedura attiva; elenco bandi riservato a importazione ed elenco PICA.
- [x] v0.7.3: Test del menu e dell'isolamento dei riferimenti per codice bando.

- [x] v0.7.4: Ripristinato per Admin il comando `Acquisisci manualmente` in tutte le schede `Da verificare`, incluse quelle con esito assente. Controlli al salvataggio e autorizzazione backend invariati.
- [x] v0.7.4: Test di regressione per presenza del comando nella vista Admin.

## Versione 0.8.0 — Gestione multiutente
- [x] Account operativi persistenti creati e amministrati dall'Admin di Home Assistant.
- [x] Password utente minima 8 caratteri (Admin preesistente minimo 12).
- [x] Salt individuali e PBKDF2-HMAC-SHA256 per le password degli account operativi.
- [x] Gestione Admin per elenco utenti, creazione, disattivazione/riattivazione e reset password.
- [x] Accesso operativo ai moduli RankUp, esclusi acquisizione manuale e gestione account.
- [x] Permessi e revoca sessioni verificati lato server.
- [x] Audit con l'identità dell'operatore autenticato.
- [x] Test di autenticazione, ruoli, persistenza, revoca e accesso alle funzioni: 36 superati.
- [ ] Collaudo interattivo con due browser/sessioni su Home Assistant OS.
