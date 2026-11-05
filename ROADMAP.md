# Roadmap RankUp

## Milestone 1 — base tecnica (in corso)
- [x] Add-on Ingress su Home Assistant OS
- [x] Dashboard e selettore bando attivo
- [x] Anteprima importazione PICA, lettura codice bando da prima riga
- [x] Importazione candidati e ricostruzione incarichi da file PICA
- [x] Importazione iniziale commissioni dal file .xlsx per bando
- [ ] Test reale su HAOS Raspberry Pi 5

## Milestone 2 — Commissioni e PDF
- [ ] Gestione anagrafica annuale e propagazione selettiva degli aggiornamenti delle commissioni
- [x] Rilevazione SSD PICA e classificazione verificata non professionalizzante/professionalizzante
- [x] Generazione di facsimili compilabili ZIP per singolo CdS o bando
- [ ] Caricamento e configurazione modelli PDF compilabili (SSD professionalizzanti e altri)
- [x] Scheda PDF per candidatura e incarico, con punteggi inizializzati a zero (facsimile da validare)
- [x] ZIP schede per CdS e per intero bando
- [ ] Acquisizione PDF restituiti, controlli e sostituzioni

## Milestone 3 — Elaborazione
- [ ] Monitoraggio schede per CdS/sede
- [ ] Verbale e graduatoria per CdS con nominativi in chiaro
- [ ] Conferma manuale firme
- [ ] Graduatoria generale parziale con nominativi visibili solo per vincitori
- [ ] Approvazione manuale, rettifiche e storico

## Milestone 4 — Sicurezza e archiviazione
- [ ] Ruoli amministratore/operatore
- [ ] ZIP autosufficiente con ripristino
- [ ] Eliminazione solo dopo ZIP verificato/scaricato
- [ ] Audit, controllo concorrenza, backup/ripristino
- [ ] Hardening e verifica conformità ai requisiti dell'Ateneo

Fuori dalla prima versione: verifica preliminare dei requisiti di accesso, elenco incarichi senza candidature.

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
- [ ] Verbali e graduatorie.
- [ ] Anagrafica annuale commissioni e propagazione manuale per bando.

- [x] 0.6.1: Download dell'originale PDF dal registro delle valutazioni, comprese le anomalie.
