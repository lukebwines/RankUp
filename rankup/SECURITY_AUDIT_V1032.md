# RankUP v1.0.32 — Registro sicurezza e operazioni

## Autenticazione e ruolo Admin
L'account **Admin di RankUP** rimane quello configurato nelle opzioni dell'add-on Home Assistant.
**Non** viene creato alcun nuovo amministratore e non si modifica automaticamente la sua password. L'account di accesso a Home Assistant resta distinto.
La migrazione delle credenziali Admin a un archivio con hash, con gestione dei backup storici e recupero sicuro, è un intervento separato (issue #77) e **non è compresa in questa versione**.

## Registro tecnico
- Nuovo DB **`/data/rankup_audit.sqlite3`**, separato da `/data/rankup.sqlite3`, per evitare scritture indirette durante la fotografia dei bandi archiviati.
- Esiti login riuscito, fallito, limitato dal rate limit; logout; cambio password operatore; reset e ripristino password a cura dell'Admin; cambio politica di scadenza.
- Mutazioni HTTP e download dei documenti protetti: metodo, modello di percorso API, bando riconoscibile e risultato HTTP. Questi sono **eventi di richiesta**, non costituiscono certificazione di avvenuto completamento delle lavorazioni asincrone.
- Gli storici amministrativi transazionali già presenti nel DB dei bandi restano la fonte principale per esiti, versioni, approvazioni e rettifiche. L'interfaccia Admin offre una vista separata `Storico amministrativo`.
- Dati esclusi dal registro tecnico: password, hash credenziali, cookie, token, nomi/codici fiscali dei candidati, corpi delle richieste, PDF e file PICA.
- L'origine è limitata alla **rete del peer ASGI** /24 o /64, non a un IP personale affidabile; l'infrastruttura proxy può occultare l'origine effettiva.
- Consultazione solo Admin mediante API server-side e `Amministrazione → Registro attività`, con filtri e paginazione fino a 100 eventi per pagina.
- **Conservazione:** periodo di retention da formalizzare **[DA VERIFICARE]** con l'organizzazione/titolare prima di abilitare cancellazioni automatiche. SQLite non viene alleggerito eliminando gli storici amministrativi necessari. Monitorare lo spazio dell'add-on.

## Password operatori
- La password temporanea che l'Admin assegna a un nuovo utente deve essere modificata **al primo accesso**.
- Il cambio personale richiede la password corrente e una nuova password di almeno 12 caratteri. Dopo il salvataggio tutte le sessioni dell'operatore vengono revocate.
- Una reimpostazione da parte dell'Admin impone il cambio al successivo login; l'Admin conserva il controllo di attivazione/disattivazione e reset.
- Gli account **preesistenti** non vengono automaticamente resettati al passaggio a v1.0.32: `password_modificata_il` resta sconosciuta (NULL) quando mancano documenti/registrazioni verificabili.
- Politica password modificabile solo da Admin: **0 giorni (predefinito)** o **180 giorni**, abilitabile solo se formalmente richiesto. L'abilitazione a 180 giorni imporrà un cambio a chi non ha una data verificata.
- Il login operatori può cambiare la propria password; **non** può consultare registro, politiche, cronologia di altri account o credenziali Admin.

## Backup e sicurezza
- Il backup obbligatorio precedente all'azzeramento ora include una copia SQLite coerente del DB tecnico `rankup_audit.sqlite3` oltre a quello operativo.
- Il registro non viene cancellato dall'azzeramento. Le vecchie informazioni nelle tabelle evento amministrativo possono essere archiviate/eliminate secondo la procedura esistente, con backup obbligatorio.
- Questa versione **non introduce backup manuali indipendenti**: il Centro backup Admin è pianificato nell'issue #75.
- Prima di aggiornare un add-on con dati reali verificare un backup Home Assistant e collaudare il cambio password su un operatore di prova.
- **MFA Cloudflare Access e configurazione tunnel** devono essere verificati sull'istanza: non è possibile ricavarne la protezione effettiva dal solo repository.
