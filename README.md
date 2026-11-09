# RankUp

Gestione candidature e graduatorie per bandi di docenza.

## Stato
Prototipo tecnico v0.8.0 (non ancora idoneo alla gestione in produzione di dati personali).

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

**Aggiornamento 0.6.1:** aggiunto il download autenticato degli originali PDF anche per le schede da verificare. Sono parte dello storico e rimangono disponibili per un controllo o per richiedere una nuova scheda alla commissione.

**Rilascio 0.6.2:** confermato il controllo delle schede a punteggio zero senza esito o note. La suite automatica sul codice funzionale ha superato tutti i 18 test. Resta necessario il collaudo sul Raspberry e nei lettori PDF reali.

### v0.6.3 — Nomi delle schede PDF nello ZIP
Il PDF di ciascuna candidatura ora include nel nome **ID PICA, cognome e nome**, seguiti dal codice tecnico dell'incarico: `IDPICA_COGNOME_NOME_chiaveincarico.pdf`. La struttura delle cartelle dello ZIP resta CdS/sede. Nomi e cognomi sono troncati a 26 caratteri ciascuno per evitare nomi troppo lunghi; gli accenti vengono normalizzati per compatibilità tra sistemi. La chiave tecnica rimane per distinguere più candidature della stessa persona a incarichi diversi. La reimportazione verifica l'identificativo interno alla scheda, non il nome del file. Le schede già scaricate non vengono rinominate: occorre rigenerare lo ZIP.

### v0.6.4 — Diagnostica ZIP schede PDF
Il caricamento di un archivio ZIP non deve confondere il numero di PDF effettivamente presenti con il numero di candidature attive PICA. L'anteprima mostra tre conteggi distinti: **PDF trovati**, **PDF analizzati**, **PDF classificati** (somma di regolari, rettifiche, da verificare e duplicati). In presenza di un'incongruenza disabilita la conferma. La sezione di avanzamento CdS/sede continua a mostrare separatamente le sole candidature attive attese.
La verifica sull'archivio ricevuto per Osteopatia / Verona ha individuato 21 file PDF distinti, tutti editabili, con punteggio iniziale zero, esito non selezionato e note vuote: senza valutazione della commissione devono risultare «Da verificare» e non essere acquisiti con punteggio zero.
Test sintetico con ZIP contenente 21 PDF: controllo automatico superato. Rimane da verificare il dato «15» visualizzato sul sistema Home Assistant, che potrebbe riferirsi a un contatore diverso.

### RankUp 0.7.0 — Valutazioni separate per CdS e recupero manuale

**Valutazioni** è organizzata per CdS e sede: i menu in alto consentono di scegliere un corso e una sede, mentre l'avanzamento e lo storico PDF mostrano solo le schede del filtro selezionato. Ogni riga CdS/sede nel prospetto apre il relativo dettaglio. La vista complessiva raggruppa lo storico per CdS/sede e mantiene visibili anche le schede senza candidatura associata. Se si carica un PDF riconosciuto per un CdS diverso da quello selezionato, l'importazione viene bloccata e non lo scarta silenziosamente.

Le schede vengono distinte in **idoneo**, **non idoneo** ed **escluso** solo sulla base dell'esito espresso dalla commissione. Le decisioni negative sono acquisibili automaticamente quando la scheda contiene un esito ammesso e una nota motivazionale: il punteggio resta `NULL`, non viene assegnato uno zero artificiale e la candidatura risulta conclusa. Per gli idonei occorre un esito esplicito e un punteggio verificabile.

Dallo **Storico delle schede** è disponibile il comando **Acquisisci manualmente** soltanto per i PDF `Da verificare`. È possibile trascrivere il punteggio per gli idonei o l'esito con la motivazione della commissione per non idonei ed esclusi. La trascrizione richiede l'identificazione della candidatura PICA, la motivazione tecnica, l'attestazione dell'operatore di aver verificato il PDF, la conferma esplicita e il PDF originale integro. Le rettifiche richiedono una conferma aggiuntiva e conservano il precedente valore; le variazioni manuali vengono registrate in `valutazioni_pdf_eventi`. Se un PDF non è riconosciuto automaticamente, l'operatore deve selezionare esplicitamente CdS/sede e candidato PICA, senza deduzioni dal nome del file.

Gli ZIP generati da Finder (macOS) possono contenere `__MACOSX` e `.DS_Store`: sono riconosciuti come metadati e ignorati, mentre il caricamento continua a richiedere tutti i PDF effettivi.

**Protezione dei dati:** l'aggiornamento usa una migrazione SQLite non distruttiva e non sovrascrive i PDF originali. Le valutazioni precedenti prive di un esito esplicito non vengono considerate idonee in via presuntiva. Eseguire un backup dell'add-on prima dell'aggiornamento. La suite automatica supera 24 test, ma resta necessario il collaudo in Home Assistant con PDF reali e la validazione amministrativa dei modelli.

### RankUp 0.7.1 — Correzione valutazioni storiche senza esito

Nelle versioni precedenti alcuni PDF con punteggio numerico valido ma senza esito della commissione potevano risultare `Acquisita`. Dalla 0.7.1 la migrazione SQLite, **non distruttiva e tracciata**, riclassifica soltanto queste acquisizioni precedenti come `Da verificare`, disattivandole dai conteggi delle valutazioni concluse. Conserva inalterati originali PDF, punteggi registrati, versioni, timestamp e riferimenti PICA; registra l'operazione nel log degli eventi. Il riesame non si ripete ai successivi riavvii.

Lo **Storico delle schede** mostra comunque il punteggio originariamente memorizzato con l'avvertenza `(storico, da convalidare)`, anziché nasconderlo per l'esito assente. Una scheda con 15 punti e campo esito `-- Seleziona --` non viene interpretata come idoneità: è necessario che la commissione esprima la propria decisione. L'acquisizione manuale di un PDF editabile con esito e nota entrambi vuoti viene bloccata; occorre ricevere una scheda o un documento della commissione effettivamente completo.

Collaudo CI con caso sintetico di 15 punti senza esito: 25 test superati. **Eseguire backup prima di installare la 0.7.1** e verificare il riesame sul sistema Home Assistant.

### RankUp 0.7.2 — Acquisizione manuale riservata all'Admin

L'azione `Acquisisci manualmente` è disponibile soltanto per la sessione avente ruolo `admin`. Il server verifica il ruolo autenticato prima di accettare qualunque trascrizione o modifica: una chiamata HTTP diretta di un utente con ruolo diverso riceve `403` e non altera il database. L'interfaccia non presenta il pulsante agli utenti non amministratori. Il ruolo è fornito dall'endpoint autenticazione, non dal client.

**Stato dei ruoli:** attualmente RankUp ha un unico account (l'amministratore configurato da Home Assistant); la gestione degli ulteriori utenti non è ancora implementata. Le verifiche di ruolo permetteranno di mantenere questa limitazione quando saranno introdotti altri profili. Consultare le valutazioni e i PDF non equivale ad avere il diritto di acquisirli manualmente.

Questa versione include inoltre il riepilogo dei punti provvisori nei PDF con esito mancante (senza considerarli idonei) e la riclassificazione delle vecchie acquisizioni prive di decisione. Fare un backup prima dell'aggiornamento e collaudare in HAOS.

### RankUp 0.7.3 — Riferimenti dell'avviso nella procedura attiva

Il modulo **Riferimenti del bando** è stato spostato dalla pagina di gestione generale **Bandi** a una pagina autonoma sotto **Procedimento del bando attivo**, sopra **Incarichi**. Nella pagina Bandi rimangono elenco delle procedure e importazione del file PICA.

Il modulo continua a leggere e salvare i quattro dati verificati dell'avviso (repertorio, protocollo, data e anno accademico) sul codice del bando attualmente selezionato. Gli endpoint `/api/bandi/{code}/riferimenti` e la struttura del database non sono stati modificati. I dati esistenti restano associati al rispettivo bando, senza recuperare valori da facsimile né introdurre valori predefiniti.

### RankUp 0.7.4 — Ripristino del comando di acquisizione manuale all'Admin

Nello storico Valutazioni, ogni documento in stato **Da verificare** mostra all'amministratore il pulsante **Acquisisci manualmente**, anche quando il PDF non riporta un esito della commissione. L'interfaccia mostra in quel caso un'avvertenza, ma non nasconde il comando.

La modifica riguarda la visibilità e l'apertura del modulo: restano invariati il controllo del ruolo `admin` nel backend e il divieto di confermare un giudizio senza un esito o una motivazione documentati dalla commissione. Il totale numerico provvisorio, quando leggibile, resta visibile come **da convalidare** e non prova l'idoneità.

La suite di test comprende un controllo di regressione specifico per il pulsante nelle schede prive di esito. Fare un backup prima dell'aggiornamento in Home Assistant.

### RankUp 0.8.0 — Account operativi e gestione multiutente

L'amministratore definito nelle opzioni di Home Assistant conserva la propria password, le proprie autorizzazioni e la politica di almeno 12 caratteri già prevista. Una nuova voce **Amministrazione → Gestione utenti** è disponibile esclusivamente per l'Admin. Da qui può creare account con username univoco (3–50 caratteri) e **password iniziale di almeno 8 caratteri**, consultarne lo stato, disattivarli o riattivarli e reimpostarne la password.

Gli utenti operativi hanno accesso a Dashboard, Bandi, importazione PICA, Riferimenti del bando attivo, Incarichi, Candidature, Commissioni, Schede PDF, Valutazioni con importazione automatica, Verbali e Graduatorie quando disponibili. **Non possono eseguire Acquisisci manualmente**. Per evitare di assegnare privilegi agli stessi utenti, **Gestione utenti rimane riservata all'amministratore**. I permessi sono verificati nel server e non si basano unicamente sulla visibilità dei pulsanti.

Le credenziali degli utenti vengono salvate nella tabella SQLite persistente `rankup_utenti` in `/data/rankup.sqlite3`; le password sono memorizzate solo come hash PBKDF2-HMAC-SHA256 con salt casuale individuale. I token di sessione (scadenza 8 ore) restano in memoria, non nel database. Disattivazione e reset della password revocano subito tutte le sessioni di quell'utente. Le azioni di amministrazione degli account sono registrate in `rankup_utenti_eventi`, senza registrare credenziali o hash nello storico.

L'operatore effettivamente autenticato è utilizzato come autore nelle operazioni di bando e valutazione tracciate. Il nome dell'account e il ruolo sono visibili nel menu laterale. Le credenziali vanno comunicate tramite un canale riservato: RankUp non dispone di invio email automatico.

**Aggiornamento:** eseguire un backup dell'add-on prima dell'installazione; la migrazione aggiunge soltanto due tabelle, senza cambiare quelle di bandi, candidature, commissioni o valutazioni. I nuovi account restano disponibili dopo il riavvio. Le sessioni invece vanno riaperte. Eseguire il collaudo della login attraverso Home Assistant e l'eventuale accesso esterno prima di distribuire credenziali a utenti reali.

### RankUP 0.9.10 — Configurazioni permanenti e archivi operativi

Le associazioni **SSD → modello scheda** sono una configurazione globale, indipendente dal bando: le classificazioni `ordinario`, `professionalizzante` e `inglese`, con i relativi riferimenti documentali, vengono **conservate dopo Azzeramento dati**. La gestione globale è nella voce **Amministrazione → Modelli per SSD**, utilizzabile anche senza bandi importati.

Le **commissioni** hanno due livelli separati: le composizioni documentate sono conservate in un'anagrafica permanente (`commissioni_catalogo`, senza vincoli verso i bandi), mentre le associazioni operative a uno specifico bando e i suoi eventi restano dati del procedimento. Al primo aggiornamento, RankUP acquisisce nell'anagrafica le commissioni già presenti senza cancellarle o modificarle. Inserimenti e aggiornamenti successivi nei singoli bandi conservano nel catalogo anche le composizioni precedenti. Dopo l'azzeramento l'anagrafica sopravvive; in **Amministrazione → Anagrafica commissioni** si possono consultare le composizioni e selezionarle per una nuova procedura, con conferma documentale esplicita. L'applicazione **non assegna automaticamente una nomina storica a un nuovo bando** e blocca il recupero con anno accademico noto e discordante rispetto al bando.

L'azzeramento include un backup SQLite e dei documenti PICA/PDF, elimina solo dati per bando, conserva account, classificazioni SSD e catalogo commissioni. L'anteprima indica il numero di SSD e commissioni conservati. Gli originali Excel relativi ai precedenti bandi restano nel backup; la loro registrazione e conservazione documentale ufficiale deve essere gestita separatamente secondo le regole dell'ente.

### RankUP 0.9.10 — Download PDF affidabile e regressione controllata degli stati

- I PDF di verbali e graduatorie si scaricano con una richiesta autenticata gestita dall'interfaccia; in caso di errore HTTP o file non valido, l'Admin vede la causa invece di un mancato download silenzioso.
- **Verbali CdS:** l'Admin può usare **Regredisci stato** solo su graduatorie **definitive non approvate**. La motivazione è obbligatoria. Un'eventuale bozza generale che include il CdS deve essere annullata prima. RankUP archivia una copia integra del PDF unico precedente, comprensiva delle schede acquisite, e la mantiene scaricabile dalle versioni storiche; le valutazioni vengono sbloccate solo dopo il successo dell'archiviazione.
- **Graduatoria generale approvata:** l'Admin può registrare una regressione motivata **solo sulla base di un provvedimento formale identificato**, il cui riferimento va inserito senza valori fittizi o segnaposto. Le graduatorie CdS coinvolte tornano definitive, non immediatamente modificabili. La procedura di approvazione originaria, i suoi estremi e il PDF rimangono nello storico come documento precedente, contrassegnato nell'interfaccia come approvazione regredita.
- Per riaprire un CdS già approvato: **prima** regressione dell'approvazione generale; **poi** regressione del singolo CdS da definitiva a lavorabile. Nessuna regressione è consentita agli operatori non Admin.
- Le regressioni registrano autore, momento, motivazione e riferimento nel registro eventi, senza modificare le schede originali, gli esiti o i PDF storici. Le nuove tabelle sono incluse nel backup e nell'azzeramento controllato.
- **Attenzione amministrativa:** il comando informatico non determina autonomamente annullamento, revoca o rettifica degli effetti giuridici di un provvedimento approvato. È necessario verificare prima gli atti formali e le competenze dell'amministrazione.

**Installazione:** effettuare un backup dell'add-on e degli originali PDF/PICA; aggiornare solo dopo i test automatici e un collaudo su Home Assistant, inclusi Ingress e Cloudflare Tunnel.

### RankUP 0.9.11 — Apertura PDF nel browser

Oltre ai pulsanti di scaricamento, verbali CdS (correnti e storici) e graduatorie generali hanno ora il comando **Apri PDF nel browser**. Il comando è un normale collegamento HTML aperto direttamente dal clic dell'utente: evita le limitazioni di Chrome sui download avviati programmaticamente dopo una richiesta asincrona. Il server restituisce lo stesso PDF con `Content-Disposition: inline`, mantenendo autenticazione e controlli di integrità. Il PDF può essere salvato dal visualizzatore integrato di Chrome. La modalità ordinaria di download continua a funzionare; il messaggio distingue la corretta ricezione del PDF dal salvataggio effettivamente confermato dal browser.

### RankUP 0.9.12 — Generazione del verbale già definitiva

Il comando **Genera verbale definitivo con schede** sostituisce il precedente percorso separato “Genera verbale” → “Rendi definitiva”. È riservato all'Admin: richiede una conferma esplicita, verifica valutazioni e parità, produce il PDF con le schede allegate senza campi compilabili e **registra lo stato definitivo nella stessa transazione SQLite**. Se manca o è illeggibile un originale o l'appiattimento non riesce, non viene chiusa la graduatoria.

La prima generazione conserva i byte del PDF definitivo e la relativa impronta SHA-256 in `graduatorie_cds_documenti`, collegata alla graduatoria. I download e le visualizzazioni successive restituiscono la stessa copia, non una nuova composizione. Le schede originali restano inalterate e recuperabili secondo le regole precedenti.

Prima della generazione il verbale completo non può essere scaricato (HTTP 409). Dopo la generazione, **Scarica verbale definitivo con schede** e **Apri PDF nel browser** sono disponibili; la versione precedente può essere conservata con **Regredisci stato** (motivazione obbligatoria). Alla nuova generazione dopo la regressione viene creata un'altra copia definitiva e la precedente resta nello storico.

**Migrazione non distruttiva:** i verbali già definitivi nelle versioni precedenti, che non dispongono ancora di una copia PDF memorizzata, rimangono consultabili grazie alla ricostruzione dalla loro istantanea e dagli originali con integrità verificata; anche in questo caso i campi compilabili vengono appiattiti.

L'appiattimento elimina i campi PDF editabili, ma **non sostituisce una firma digitale** né rende materialmente impossibile alterare un PDF con strumenti di editing avanzati. Per attestare provenienza e integrità nei procedimenti amministrativi resta necessaria la verifica della firma digitale o dell'impronta archiviata, secondo le procedure dell'ente.

### RankUP 0.9.13 — Valutazioni organizzate per schede e incarichi PICA

La pagina **Valutazioni** rimane centrata sulle **schede delle candidature**, ma le raggruppa per **CdS → sede → incarico PICA** (insegnamento, modulo, SSD, identificativo dell'incarico). Anche gli incarichi senza PDF compaiono con il numero di candidature previste, per evitare di confondere la quantità di file storici con il numero di incarichi.

- **Schede correnti** (vista predefinita): mostra soltanto le valutazioni **acquisite, attive e con esito validato**, che possono concorrere al verbale. Le anomalie tuttora da esaminare sono evidenziate separatamente nel relativo incarico e non sono conteggiate come schede valide; i PDF non associati sono visibili in una sezione distinta.
- **Tutte le versioni**: conserva la consultazione delle schede precedenti, delle acquisizioni rifiutate, dei PDF da verificare e delle analisi IA, sempre raggruppati per incarico. Una versione sostituita è etichettata come storica, non presentata come errore corrente.
- I pulsanti PDF, verifica occupazionale IA e acquisizione manuale riservata all'Admin restano utilizzabili. I filtri CdS e sede e il riepilogo avanzamento restano disponibili.
- Il collegamento ai documenti usa `incarico_id` e `candidatura_id` già registrati nel database; non fa deduzioni dal nome del file o dal candidato.
- **Nessuna migrazione dei dati**, nessuna modifica ai punteggi, agli esiti, ai PDF originali o alle graduatorie.

Per gli **SSD nuovi non ancora associati** resta la regola preesistente: il modello iniziale è **Non professionalizzante** (`ordinario`); l'Admin può modificarlo nella configurazione globale, che persiste dopo l'azzeramento degli archivi. Nessun blocco automatico aggiuntivo è stato introdotto.

### RankUP 0.9.14 — Stati dei verbali più riconoscibili

Nella pagina **Verbali** le schede CdS in **In lavorazione** sono segnalate in giallo/ambra, inclusi sfondo e bordo della scheda. Le schede **Pronto** rimangono blu, quelle **Definitiva** hanno un badge verde con coccarda grafica, quelle **Atti approvati** un badge verde istituzionale distinto. I colori sono leggibili in modalità chiara e scura, mentre le etichette di testo garantiscono l'identificazione anche senza percezione cromatica.

Sono state modificate esclusivamente classi CSS e rendering delle etichette: nessuno stato, dato, PDF o flusso di approvazione viene alterato.

### RankUP 0.9.15 — Chiusura del bando con ZIP integrale e storico permanente

La voce **Archivio** non è più un segnaposto: l'Admin può verificare lo stato dei CdS/sedi del bando e, **solo dopo che tutti i verbali sono definitivi e inclusi in approvazioni degli atti concluse**, usare **Chiudi bando e genera archivio ZIP**. La chiusura non è consentita se mancano ambiti, ci sono bozze ancora aperte, o se una graduatoria/approvazione è ancora da finalizzare o è stata regredita. I CdS vengono individuati da tutti gli incarichi PICA realmente importati.

Lo ZIP generato contiene, per il bando selezionato:

- originali PICA e tutti i documenti originali conservati nella cartella del bando, comprese le varie versioni delle schede di valutazione ricevute;
- verbali definitivi CdS con schede non compilabili e graduatorie generali con estremi degli atti approvati;
- esportazione leggibile in JSON dei dati, degli esiti, delle graduatorie, delle commissioni collegate, delle revisioni e dei registri eventi, comprese le versioni PDF conservate in BLOB e le configurazioni SSD effettivamente riferite al bando;
- `manifest.json` contenente impronte SHA-256 di ogni file, dimensioni, numero di CdS e approvazioni, oltre a `00_riepilogo.json`.

Lo ZIP viene **verificato prima di essere registrato** e conservato in `/data/archivi_bandi` con permessi ristretti, insieme al relativo hash nel registro permanente `bandi_archivi`. Dopo la chiusura, le API che possono modificare il bando vengono bloccate. Per riaprire la procedura l'Admin deve indicare motivazione e riferimento a un provvedimento formale; lo ZIP precedente resta intatto e un'eventuale successiva chiusura crea una **nuova versione**, mai sovrascrive la precedente.

Gli ZIP finali e il registro sono conservati anche dalla procedura **Azzeramento archivi**: la cancellazione dei dati operativi non tocca questa cartella. La riapertura di un bando già eliminato dall'azzeramento non è automatica: occorre un'apposita procedura di reimport/ripristino verificata; lo ZIP è comunque scaricabile.

**Protezione documentale:** il pacchetto contiene dati personali e **non è cifrato**; va conservato tramite i sistemi autorizzati dell'ente. Lo ZIP locale è una copia di lavoro verificabile, ma non equivale a conservazione digitale a norma, protocollo o firma digitale. Non sono generati dati amministrativi mancanti; tutti i riferimenti agli atti derivano dalle registrazioni esistenti in RankUP.

### RankUP 0.9.16 — Ricerca storica delle schede per codice fiscale

La pagina **Archivio → Ricerca storica candidati** interroga contemporaneamente tutte le candidature dei bandi presenti in RankUP, compresi i bandi chiusi e archiviati, con ricerca per codice fiscale esatto (quando disponibile), cognome, nome e ID PICA. Mostra per ciascuna candidatura il bando e il suo stato, CdS/sede, incarico, esito, punteggio e **tutte le versioni delle schede**, con accesso diretto al PDF originale. Le candidature senza scheda sono comunque identificabili.

La relazione documentale **non richiede di alterare i PDF**: `valutazioni_pdf.candidatura_id` identifica `candidature.id`, che conserva `codice_fiscale` importato da PICA. Il codice fiscale non è una chiave univoca nel database (la stessa persona può presentare più domande/incarichi), ma consente di confrontare candidature fra bandi quando il dato è presente e affidabile. Non vengono collegati automaticamente omonimi; se PICA non contiene un CF il sistema non inventa il dato.

**Sicurezza:** funzione riservata all'Admin, ricerca tramite POST anziché URL, risposte con codice fiscale mascherato e senza esportazione indiscriminata. Per il trattamento, consultazione e conservazione dei dati applicare le policy dell'Ateneo. La funzione è di sola lettura; non tocca punteggi, esiti o PDF.

**Archivio in produzione:** i bandi chiusi restano nel database operativo; il loro ZIP è una copia integrale separata. La ricerca attraversa i bandi ancora registrati nel database e **non indicizza automaticamente gli ZIP dopo un azzeramento o una cancellazione**. L'azzeramento rimane una funzione straordinaria per la pulizia dell'ambiente di prova prima dell'avvio in produzione, non una procedura da ripetere fra bandi.
