# RankUp — configurazione e accesso (v0.2.0)

**Prototipo sperimentale. Non caricare dati personali reali o esporre pubblicamente RankUp finché non saranno completati collaudi, backup, audit, ruoli operatore e verifica di sicurezza.**

## 1. Credenziali amministratore (Configurazione)
Nella scheda **Configurazione** dell'app RankUp su Home Assistant impostare:
- `admin_username`: nome utente amministratore, inizialmente `admin`, modificabile.
- `admin_password`: password scelta dall'utente, obbligatoria da 12 a 128 caratteri. Non è presente alcuna password predefinita.

La password viene letta dal file delle opzioni del Supervisor, `/data/options.json`, all'avvio. RankUp usa un verificatore derivato dalla password e non inserisce credenziali nel repository. Per modificare la password, aggiornare le opzioni e riavviare l'app. Il riavvio invalida tutte le sessioni esistenti.

**Nota:** in questa versione esiste solo l'account amministratore; gli account operatore e i permessi separati sono pianificati ma non ancora implementati.

## 2. Accesso da Home Assistant Ingress
- Ingress rimane attivo e usa la porta **interna** `8095`.
- Aprire RankUp dal menu laterale di Home Assistant.
- La schermata di login RankUp richiede le credenziali configurate sopra.

## 3. Porta per Cloudflare Tunnel
Nella scheda **Rete** dell'app impostare la **porta host** associata a `8095/tcp`.

La porta esterna è **disabilitata per impostazione predefinita**. La porta host scelta è modificabile senza cambiare la porta interna `8095`. Verificare che la porta host non sia già usata da un'altra app.

Per un tunnel Cloudflare che raggiunge il Raspberry tramite rete locale, configurare l'origin service su:
`http://<indirizzo-IP-del-raspberry>:<porta-host-scelta>`

Il DNS e l'hostname pubblico sono configurati **su Cloudflare**, non in RankUp. Usare HTTPS sul lato pubblico e preferire anche Cloudflare Access per una seconda barriera di autenticazione. **Non configurare un inoltro diretto della porta sul router.**

## 4. Controlli di sicurezza introdotti
- L'API di RankUp richiede una sessione autenticata.
- Sessioni casuali revocabili con cookie HttpOnly e SameSite=Lax, durata massima 8 ore e invalidazione al riavvio.
- Protezione con intestazione dedicata per chiamate che modificano dati.
- Limite dei tentativi di login falliti.
- Nessuna password di esempio salvata nel repository.

**Limitazioni di sicurezza:** questa non è ancora una release validata per uso in produzione o dati riservati. È necessario effettuare collaudi funzionali e di sicurezza sul Raspberry prima di attivare la porta esterna.

## 5. Primo test
1. Aggiornare il repository dell'app Home Assistant e installare la versione 0.2.0.
2. Nella scheda Configurazione impostare username/password e salvare.
3. Avviare RankUp, aprire Ingress e verificare login/logout.
4. Provare con i file PICA/commissioni di esempio, non con dati reali.
5. Solo dopo i controlli di sicurezza, valutare la porta host e Cloudflare Tunnel.

## Schede PDF (v0.3.0, facsimile tecnico)
La prima importazione PICA acquisisce la colonna **Settore scientifico disciplinare**. Per i bandi precedenti la versione 0.3.0, RankUp recupera automaticamente l'SSD dal file PICA originale memorizzato nell'add-on, senza reimportare le candidature.

Nel menu **Schede PDF** classificare ogni SSD, indicando se è professionalizzante oppure non professionalizzante e un **riferimento documentale verificato**. Nessun SSD viene classificato per supposizione. Le impostazioni si riutilizzano fra bandi.

Dopo aver importato le commissioni, selezionare un CdS, eventualmente una sede, e premere **Genera e scarica ZIP**. La selezione generale produce uno ZIP con cartelle per CdS/sede. La procedura si arresta se mancano tipologie SSD o commissioni.

**Importante:** le due schede attualmente prodotte sono **facsimili tecnici**, ricostruiti come PDF editabili. Il calcolo del totale dipende dal lettore PDF (preferibilmente Adobe Acrobat); RankUp dovrà ricalcolarlo autonomamente quando sarà implementata la lettura delle schede restituite. Prima di un uso ufficiale occorre validare criteri, massimali, anni di attività professionale e layout sulla base della documentazione approvata, non soltanto degli esempi.

Il caricamento di modelli PDF neutralizzati da parte dell'amministratore, con mapping dei campi e versionamento, sarà sviluppato nella prossima fase; i PDF già compilati non devono essere caricati come template neutri.

## Correzione SSD per bandi già importati (v0.3.1)
Il campo `ssd` nell'esportazione PICA si riferisce all'incarico. Il campo `Settore scientifico disciplinare`, nella sezione professionale del candidato, non va usato per individuare il settore della copertura.

All'avvio, RankUp esamina il file PICA originale conservato in `/data/sources/<codice-bando>/` e corregge anche gli SSD non vuoti precedentemente letti dalla colonna errata, senza alterare candidature, commissioni o classificazioni manuali. La procedura è transazionale, eseguita una sola volta per bando e registra i valori antecedenti e successivi. Se il file originale è assente o la corrispondenza degli incarichi non è certa, la correzione non viene eseguita.

Nella pagina Schede PDF viene indicato il numero di incarichi corretti. **Non eliminare e non reimportare** il bando esistente. Verificare il valore `NN` presente nel file PICA: non corrisponde a un SSD specifico e richiede un accertamento prima della generazione delle schede per gli incarichi coinvolti.

## Metadati completi degli incarichi (v0.4.0)

Aggiornare RankUp alla 0.4.0 dopo un backup dell'add-on. **Non reimportare il bando già esistente.** Durante l'avvio, RankUp rilegge una sola volta il file PICA originale archiviato in `/data/sources/<codice-bando>/pica_originale.xls` oppure `.xlsx` e controlla che la sua impronta SHA-256 coincida con quella registrata al momento dell'importazione.

La migrazione confronta gli identificativi interni di **tutti** gli incarichi ricostruiti: se il file, la corrispondenza o i valori già salvati non sono coerenti, non effettua modifiche e la sezione **Incarichi** segnala che l'acquisizione non è verificata. Se va a buon fine, integra esclusivamente i nuovi campi, registra il numero di incarichi aggiornati e conserva uno storico delle modifiche.

Nel menu **Incarichi** aprire **Mostra tutti i dati dell'incarico**. Sono disponibili Anno del Corso, Tipo Corso, Tipo CDS, Ciclo, date INIZIO/FINE, codice e descrizione della partizione studenti, Peso Copertura, Ore, Stato coperture, codici e SSD. Per INIZIO/FINE si mostrano le date leggibili e il **valore originale Excel**. La colonna Ore vuota rimane vuota e viene indicata come non presente: RankUp non la calcola dal Peso Copertura.

Per il file PICA 26-02 verificato: 42 incarichi; i metadati ripetuti nelle 93 candidature sono coerenti. Non ci sono ore nella colonna V. Il codice partizione (S) e la descrizione (T) sono compilati solo per parte degli incarichi. Questi conteggi descrivono **soltanto quel file PICA**, non i bandi futuri.

Il recupero e le verifiche vanno collaudati sull'installazione HAOS prima di utilizzare i dati per generare atti amministrativi.

## 0.4.1 — Modello non professionalizzante predefinito
Per ciascun SSD effettivamente identificato, RankUp presenta e usa automaticamente il modello non professionalizzante. **Non bisogna premere Salva né inserire un riferimento** per lasciare questa scelta. Se un SSD è professionalizzante, cambiare il menu della riga su Professionalizzante: RankUp registra immediatamente la selezione e la mantiene al riavvio. Per ripristinare il modello ordinario basta selezionarlo nuovamente. Le scelte manuali hanno effetto sui bandi che condividono lo stesso SSD.

La classificazione non viene dichiarata come fatto accademico ufficiale: è una scelta dell'operatore per individuare il modello PDF da applicare. Gli incarichi con SSD non determinato (`NN` o vuoto) rimangono bloccati e vanno verificati individualmente.

## Scheda professionalizzante A4 — v0.4.2

1. Aggiornare il componente, verificare che in basso nell'interfaccia compaia **0.4.2**.
2. Nel bando, aprire **Schede PDF** e generare lo ZIP di **un solo CdS** con incarichi professionalizzanti.
3. Aprire la scheda con **Adobe Acrobat Reader** o un altro visualizzatore che esegua gli script dei moduli PDF.
4. Verificare la struttura **su una pagina** e le sei aree: titoli e aggiornamento (15), attività professionale (10), attività didattica (15), attività scientifica (7), questionario studenti (3), esperienze e competenze (10).
5. Provare i due menu per una delle cinque annualità, controllare il subtotale annuale e il totale, salvare e riaprire. Confermare che esito e note rimangano modificabili.
6. Controllare la corrispondenza dei dati anagrafici, requisiti e commissione con le fonti del bando. Non confermare dati **[DA VERIFICARE]** senza la relativa fonte ufficiale.

Gli anni 2025-2021 sono previsti **solo** per l'A.A. 2026-2027 perché presenti nel modello fornito. Per altri periodi è necessario acquisire il modello di riferimento e riconfigurare il quinquennio. Il PDF restituito è ancora un facsimile tecnico: il layout e le regole di conteggio devono essere validati prima dell'uso amministrativo. La firma digitale e la protocollazione avvengono successivamente, all'esterno di RankUp.

L'esportazione ZIP produce nuovi PDF: **i file scaricati prima della 0.4.2 non vengono cancellati dal computer**. Sostituirli manualmente e usare soltanto la versione corretta. I template ufficiali editabili caricabili senza modifiche al codice restano una funzione da implementare.

## v0.4.3 — Scheda ordinaria e SSD NN
La generazione per tutti gli SSD non professionalizzanti utilizza ora un nuovo modello editabile di una pagina, basato sul PDF ordinario originale. I massimali per area sono 15 (titoli/aggiornamento), 10 (attività professionale), 15 (didattica), 10 (scientifica), 3 (valutazione didattica), 7 (esperienze/competenze). La sezione scientifica descrive **cinque tipi di pubblicazioni** con un unico punteggio P13, come il modulo di riferimento. I punteggi restano inizializzati a zero; l'esito e le note sono editabili.

La classificazione **NN** in PICA è mantenuta come dato di origine, ma per decisione operativa viene usato il **modello ordinario**. Non occorre selezionare o salvare nulla per NN, né viene consentita una selezione professionalizzante su tale codice. Una cella realmente vuota è ancora segnalata per verifica.

Per collaudare: aggiornare RankUp alla versione 0.4.3, generare lo ZIP di un CdS con incarico ordinario o NN, aprire il PDF in Adobe Acrobat Reader, verificare una sola pagina, 15 campi P01-P15, totale iniziale zero e massimo 60, salvataggio note/esito e calcolo del totale dopo l'assegnazione di alcuni punteggi. Confrontare l'impaginazione con il modello ufficiale prima dell'uso amministrativo. La commissione firmerà successivamente, fuori dall'app.

## Correzione associazioni commissioni (v0.4.4)

Con il bando aperto, andare su **Commissioni**. Il riquadro *Associazione commissioni a CdS e sede* elenca i CdS/sedi che hanno un incarico PICA, distinguendo quelli associati da quelli mancanti o ambigui. Il programma riconosce automaticamente il suffisso descrittivo *ABILITANTE ALLA PROFESSIONE...* presente nell'esportazione PICA quando non è presente nel file delle commissioni. La **sede deve sempre coincidere**: nessuna commissione viene mutuata da un'altra sede sulla sola base del nome del CdS.

La generazione ZIP per **tutto il bando** si interrompe se vi sono CdS/sedi senza commissione certa. Nel frattempo è possibile selezionare un **singolo CdS** con commissione abbinata, evitando di generare schede con nominativi errati. Le 16 righe dell'Excel originale risultano sufficienti per 16 delle 17 combinazioni CdS/sedi del file PICA 26-02. Manca una riga ufficiale per *Igiene Dentale / Verona*; non attribuirle altri nominativi. Non ricaricare l'Excel già importato, perché l'integrazione differenziale delle commissioni è una funzione ancora da implementare.

Nel PDF professionalizzante, i menu delle attività professionali e aggiuntive ora espongono le denominazioni delle scelte, anche quando hanno valori identici (per esempio le due opzioni da 0,5 punti). Per verificare: aprire una nuova scheda dopo l'aggiornamento in Adobe Acrobat Reader, espandere il menu, selezionare una voce e controllare che il **totale resti numerico**. Il PDF precedente non viene sovrascritto sul computer.
