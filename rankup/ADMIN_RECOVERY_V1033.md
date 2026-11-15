# RankUP v1.0.33 — Password Admin e recupero tramite Home Assistant

## Cosa cambia
RankUP continua ad avere **un solo account Admin**, distinto dall'account Home Assistant.
A partire dalla v1.0.33, username e password hash dell'Admin sono conservati in
`/data/rankup.sqlite3` nella tabella protetta `rankup_admin_auth`.
Le password in chiaro e i codici di recupero **non** vengono salvati nel DB.
L'Admin può cambiare password in **Gestione utenti → Amministratore → Modifica password Admin**.

### Aggiornamento dalla v1.0.32
Al primo avvio, RankUP legge la precedente `admin_username` e `admin_password` dalle
opzioni dell'add-on **solo se** non esiste ancora un account Admin protetto.
L'applicazione crea un hash PBKDF2-HMAC-SHA256 con salt casuale e lo conserva.
Dopo questa migrazione, cambiare `admin_password` in Home Assistant **non cambia
la password di RankUP** e non permette di riottenere l'accesso.
La migrazione non modifica bandi, candidature, schede, commissioni e graduatorie.

Dopo aver verificato il login alla nuova versione:
1. In Home Assistant aprire **RankUP → Configurazione**.
2. **Svuotare/rimuovere `admin_password`**, ormai superflua, quindi salvare.
   L'opzione è ora facoltativa.
3. Riavviare e verificare il login con la password corrente di RankUP.

La rimozione della voce non elimina i segreti già salvati nei **backup storici**
di Home Assistant. Valutare la loro protezione e la rotazione di credenziali
eventualmente riutilizzate su altri servizi. Il backup tecnico del DB contiene
un hash della password, non il suo testo in chiaro.

## Recupero in caso di perdita della password Admin
L'accesso alle opzioni e ai log dell'add-on è il canale di recupero di emergenza:
chi non è autorizzato a gestire l'add-on in Home Assistant non deve avervi accesso.

1. Accedere a Home Assistant come amministratore di sistema.
2. Aprire **Impostazioni → Componenti aggiuntivi → RankUP → Configurazione**.
3. Impostare `admin_recovery_request` a un intero **maggiore dell'ultimo
   utilizzato**, per esempio da 0 a 1, poi a 2 per richieste successive.
4. Salvare e **riavviare RankUP**.
5. Aprire il **Registro/Log** dell'add-on. Comparirà una sola volta una riga
   che inizia con `RANKUP RECUPERO ADMIN` e contiene un codice casuale.
6. Entro **10 minuti**, nella schermata login di RankUP scegliere
   **Password Admin dimenticata?**, incollare il codice e impostare
   una nuova password (almeno 12 caratteri).
7. Il codice viene consumato una sola volta. Tutte le vecchie sessioni Admin
   vengono revocate; effettuare un nuovo login.

Un codice scaduto o invalidato dopo **5 tentativi errati** non può essere riusato.
Per richiederne un altro aumentare `admin_recovery_request` e riavviare.
Abbassare o reimpostare il valore a 0 **non** produce nuovi codici.
La password corrente non è necessaria per il recupero, ma il controllo della
configurazione e dei log di Home Assistant è indispensabile.

**Attenzione ai log:** durante quei 10 minuti il codice è un segreto temporaneo.
Limitare l'accesso ai log dell'add-on e a eventuali raccolte centralizzate;
non copiarlo in ticket, email o chat. Un codice rimasto nei log dopo la scadenza
non è più valido. Lo storico di sicurezza RankUP registra la riuscita o
il rifiuto del recupero senza registrare il codice.

## Primo avvio privo di password
Se non esiste ancora una credenziale Admin protetta e `admin_password` nelle
opzioni è vuota, RankUP non assegna una password predefinita. Si può usare
il percorso di recupero sopra descritto da Home Assistant per inizializzare
l'account. Nessun utente operativo può eseguire il recupero al posto
dell'amministratore di Home Assistant senza accedere a quel canale riservato.

## Sicurezza, test, manutenzione
- Le API di gestione corrente sono riservate all'Admin.
- Il recupero è un endpoint pubblico esclusivamente per consentire l'accesso
  in assenza di una sessione; richiede un codice casuale di alta entropia
  emesso tramite il canale Home Assistant.
- Credenziali e codice non compaiono nelle risposte di lista utenti né
  nel registro tecnico; dopo il recupero le sessioni precedenti sono revocate.
- L'accesso esterno tramite Cloudflare deve essere protetto **a parte**.
- La procedura di migrazione include il backup SQLite pre-schema; la verifica
  su un'installazione con dati reali e la gestione dei backup storici restano
  responsabilità dell'amministratore del sistema.
- Verificare periodicamente che la durata dei log Home Assistant e i permessi
  di accesso siano adeguati alle politiche dell'organizzazione.
