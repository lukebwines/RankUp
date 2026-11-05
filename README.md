# RankUp

Gestione candidature e graduatorie per bandi di docenza.

## Stato
Prototipo tecnico v0.4.1 (non ancora idoneo alla gestione in produzione di dati personali).

### Funzioni implementate
- Add-on Home Assistant OS con accesso tramite Ingress e login amministratore configurabile.
- Porta host opzionale per Cloudflare Tunnel, disabilitata per impostazione predefinita.
- Dashboard chiara con menu laterale blu notte e bando attivo sempre evidenziato.
- Importazione PICA in formato Excel .xls/.xlsx con anteprima e conferma.
- Riconoscimento del codice bando dalla prima riga (es. "univr 26-02 - Esportazione dati").
- Ricostruzione di incarichi e candidature senza un secondo elenco degli incarichi.
- Importazione delle commissioni da Excel .xlsx con controllo di coerenza del codice bando e delle composizioni per CdS.
- Lettura SSD da PICA anche per bandi precedentemente importati (migrazione dal file originale).
- Configurazione manuale della classificazione SSD con riferimento documentale.
- Generazione facsimile di schede PDF AcroForm in due strutture distinte e ZIP per CdS o intero bando.
- Conservazione dei dati in SQLite nello spazio persistente /data.
- Pagine Bandi, Candidature, Valutazioni, Verbali, Graduatorie, Archivio (alcune ancora segnaposto).

### Non ancora implementato
Modifica delle commissioni già importate, importazione di modelli PDF personalizzati e lettura delle schede compilate, verbali, graduatorie, account operatore/ruoli distinti, export ZIP e cancellazione. Il controllo requisiti è stato rinviato su richiesta.

## Installazione su Home Assistant
Aggiungere questo repository in **Impostazioni > Componenti aggiuntivi > Negozio > Repository**; nella scheda **Configurazione** impostare `admin_username` e `admin_password` (obbligatoria), quindi avviare RankUp e aprire l'interfaccia Ingress. Per il tunnel, nella scheda **Rete** si può assegnare la porta host a `8095/tcp`, disabilitata inizialmente. La build richiede accesso Internet per le dipendenze Python. Per i dettagli vedere `rankup/DOCS.md`.

**Sicurezza:** usare solo dati fittizi in questa fase. Il repository non deve contenere file PICA, schede PDF o dati personali. Non esporre la porta interna dell'add-on direttamente su Internet.

## Sviluppo
Vedere i file in rankup/app e il piano di sviluppo nel file ROADMAP.md.

### Schede PDF nella v0.3.0
Dal menu **Schede PDF**, classificare manualmente ciascun SSD sulla base di documenti verificati; quindi selezionare tutti i CdS o un singolo corso/sede e scaricare lo ZIP. La generazione è bloccata in mancanza di classificazione o commissione. **Sono facsimili tecnici da verificare**: non utilizzare per valutazioni amministrative ufficiali finché non saranno approvati criteri, massimali, anni e struttura delle schede.

### Correzione SSD 0.3.1
Il PICA contiene due colonne distinte: **ssd** per l'incarico e **Settore scientifico disciplinare** nella sezione delle dichiarazioni del candidato. RankUp usa esclusivamente l'SSD dell'incarico. Dopo l'aggiornamento il sistema riconcilia una sola volta tutti gli SSD dei bandi esistenti dal file PICA originale presente in /data/sources, mantenendo un registro delle rettifiche. Non reimportare o eliminare i bandi. Il valore **NN** non identifica uno specifico SSD: viene segnalato per verifica e non è classificabile automaticamente.

### Anagrafica completa degli incarichi (v0.4.0)

Il PICA fornisce già tutte le informazioni della copertura. RankUp salva come campi distinti **Anno del Corso (I)**, **Tipo Corso (K)**, **Tipo CDS (L)**, **Ciclo (P)**, **INIZIO (Q)**, **FINE (R)**, **Codice Partizione Studenti (S)**, **Partizione Studenti (T)**, **Peso Copertura (U)**, **Ore (V)** e **Stato coperture (BU)**. Le date sono disponibili sia nel valore originale dell'Excel sia nella rappresentazione ISO ricavata dal seriale. **Ore non viene dedotto dal Peso Copertura.**

Per i bandi già importati (come il 26-02), una migrazione controllata recupera i dati esclusivamente dal file PICA originale conservato nell'add-on, dopo confronto dell'impronta SHA-256 e verifica della corrispondenza univoca di tutti gli incarichi. Gli altri dati non vengono modificati. Lo storico di migrazione resta in SQLite. La nuova interfaccia **Incarichi** mostra il dettaglio di ciascuna copertura.

In questa milestone non vengono modificati i modelli PDF né generate informazioni mancanti nella fonte.

### Scelta del modello PDF (v0.4.1)
Gli SSD PICA effettivamente identificati usano **Non professionalizzante** come modello operativo predefinito. Non è necessario classificarli o salvarli singolarmente. Nella sezione **Schede PDF**, scegliere **Professionalizzante** solo per i settori interessati: la selezione viene salvata immediatamente nel database e riutilizzata in altri bandi. Non occorre indicare un riferimento documentale per il salvataggio. Le modifiche sono visibili come **Scelta salvata**, mentre gli SSD senza interventi manuali restano **Predefinito**. I codici privi di SSD effettivo (per esempio `NN`) restano in verifica e non possono essere utilizzati per la generazione senza un dato corretto. Le schede PDF restano facsimili da validare prima dell'uso amministrativo.
