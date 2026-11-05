# RankUp

Gestione candidature e graduatorie per bandi di docenza.

## Stato
Prototipo tecnico v0.6.0 (non ancora idoneo alla gestione in produzione di dati personali).

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

### Scheda professionalizzante a pagina unica (v0.4.2)

Il generatore per gli SSD professionalizzanti usa ora un layout A4 ricostruito **senza riutilizzare il PDF compilato** dell'esempio. Le sei aree di valutazione e i massimali riproducono la struttura del modello fornito: **15 + 10 + 15 + 7 + 3 + 10 = 60 punti**. Sono previsti i menu del modello per i cinque anni di attività professionale, calcolo per annualità e totale tramite AcroForm JavaScript, note, esito, requisiti e commissione nella medesima pagina. I punteggi iniziano da zero.

Per l'A.A. **2026-2027** si utilizzano i cinque anni 2025-2021 risultanti dal modello fornito. Per anni accademici diversi, in assenza di un modello approvato, gli anni riportano **[DA VER.]**. I requisiti sono letti dal file delle commissioni; se non acquisiti, il PDF indica **[DA VERIFICARE]**. Non vengono mai introdotti estremi di bando/protocollo o nominativi dall'esempio compilato.

Le schede generate prima dell'aggiornamento devono essere **rigenerate**. Il calcolo interattivo va provato in un lettore che supporta JavaScript PDF (es. Adobe Acrobat), perché alcuni visualizzatori non eseguono le formule. Rimane un **facsimile tecnico da validare** prima dell'invio alle commissioni. La scheda ordinaria non è stata modificata.

### Scheda non professionalizzante, inclusi gli SSD NN (v0.4.3)
La scheda ordinaria è stata ricostruita su **una sola pagina A4** usando come riferimento il modello PDF non professionalizzante fornito. Mantiene le sei aree e i relativi massimali **15 + 10 + 15 + 10 + 3 + 7 = 60**, la tabella verde e azzurra, i campi punteggio `P01`-`P15` inizializzati a zero, un unico punteggio `P13` per le cinque tipologie di pubblicazioni, esito, note e commissione. Requisiti mancanti: **[DA VERIFICARE]**. Le firme vengono apposte fuori da RankUp.

**NN** è mantenuto nel dato originale PICA ma utilizza **automaticamente il modello non professionalizzante**, senza intervento dell'operatore. Il menu di selezione è disabilitato per NN per impedire un'attribuzione professionalizzante impropria. La decisione non modifica il valore dell'SSD né altre candidature. I campi effettivamente vuoti o gli altri codici non determinati restano da verificare.

I PDF già scaricati non sono sostituiti automaticamente: **rigenerare lo ZIP** dopo l'aggiornamento. La scheda resta un facsimile tecnico da validare e i totali calcolati via JavaScript richiedono lettori PDF compatibili.

### Correzione associazione commissioni e menu PDF (v0.4.4)

- L'associazione commissioni ora confronta il **CdS e la sede**, riconoscendo l'eventuale suffisso PICA `(ABILITANTE ALLA PROFESSIONE ...)` quando la commissione riporta il nome breve. Non vengono effettuati confronti approssimativi né trasferimenti automatici tra sedi.
- La pagina **Commissioni** mostra un prospetto di associazione, distinguendo abbinate, mancanti e ambigue. Anche **Schede PDF** mostra i conteggi prima della generazione ZIP.
- Per il file PICA e l'Excel commissioni forniti del bando 26-02, il confronto locale rileva **16 CdS/sedi associati su 17**: manca nell'Excel la commissione per **IGIENE DENTALE / VERONA**. Non sono stati inventati nominativi e nessuna commissione esistente viene sovrascritta.
- I menu professionali di attività e tutoraggio mostrano le **etichette descrittive** del modello, anziché duplicare il valore numerico per opzioni che condividono gli stessi punti (es. 0,5). I valori esportati restano numerici per il totale calcolato.
- Nessuna reimportazione PICA/commissioni è richiesta per gli altri CdS. Gli ZIP generati in precedenza vanno rigenerati. I PDF restano facsimili da collaudare in Adobe Acrobat Reader.

### Commissioni modificabili e riferimenti del bando (v0.5.0)

Nella pagina **Commissioni** RankUp consente ora di:
- inserire manualmente una commissione scegliendo CdS/sede dagli incarichi PICA, anche direttamente dall'elenco delle commissioni mancanti;
- modificare presidente, componenti, requisiti e anno accademico di una commissione già registrata, oppure correggerne l'associazione CdS/sede attraverso la selezione guidata;
- scaricare un **format Excel .xlsx** con le stesse sette colonne accettate dall'importatore: CORSO DI LAUREA, SEDE, PRESIDENTE, COMPONENTI COMMISSIONE, BANDO, REQUISITI, ANNO;
- scegliere tra formato **vuoto**, formato **precompilato con CdS/sedi PICA** (senza nominativi inventati) oppure formato **con le commissioni già registrate**;
- importare soltanto le commissioni nuove oppure, con scelta esplicita, aggiornare le commissioni esistenti. Le righe assenti dall'Excel non vengono eliminate e i campi vuoti non cancellano quelli precedenti durante gli aggiornamenti massivi.

Tutte le variazioni sono registrate nello storico SQLite del singolo bando. Le modifiche non si propagano ad altri bandi. Dopo una modifica rilevante occorre rigenerare le schede PDF già scaricate.

Nella pagina **Bandi** è presente anche il pannello *Riferimenti del bando*, con campi editabili per **repertorio avviso, protocollo, data dell'avviso e anno accademico**. I campi sono vuoti finché l'operatore non acquisisce i dati ufficiali; **nessuno degli estremi presenti nei PDF di esempio viene copiato automaticamente**. Una volta completati repertorio, protocollo e data, i riferimenti vengono utilizzati nella nuova generazione delle schede PDF, senza eliminare i requisiti specifici salvati nell'anagrafica delle commissioni.

La versione 0.5.0 è un prototipo sperimentale: richiede ancora collaudo completo su Home Assistant e validazione dei modelli PDF prima dell'impiego in procedimenti ufficiali.

### RankUp 0.6.0 — Valutazioni PDF, icone e modalità scura

La sezione **Valutazioni** del bando attivo consente un'importazione controllata in due fasi: **Analizza PDF senza acquisire** e **Conferma acquisizione**. Si possono caricare fino a 120 file PDF, anche da ZIP, entro 40 MB per batch. Il programma riconosce la candidatura tramite ID RankUp e dati PICA, valida il modello, ricalcola punteggi e totale, verifica i massimali ed evidenzia anomalie, duplicati e rettifiche. Non esegue JavaScript PDF e non si basa sul totale visualizzato dal lettore.

La prima fase non registra punteggi nel database: i PDF restano in una sessione temporanea fino a 60 minuti. Dopo conferma, le schede regolari sono acquisite e conservate; quelle anomale sono archiviate come **Da verificare**, senza attribuire punteggi; i duplicati sono ignorati. Una nuova scheda per una candidatura già valutata richiede conferma esplicita di rettifica, conserva la versione precedente e non sovrascrive i documenti originali.

Il prospetto per CdS/sede considera soltanto le candidature attive. Il logo RankUp è visibile in Dashboard, con favicon e icone SVG locali per la navigazione. Il tema scuro è selezionabile in alto e ricordato dal browser. Nel pannello Home Assistant è utilizzata l'icona del podio.

**Stato sperimentale:** collaudare su Raspberry/HAOS, verificare i PDF compilati e salvati in Adobe Acrobat Reader ed eseguire un backup prima dell'aggiornamento. La generazione di verbali e graduatorie resta una fase successiva.
