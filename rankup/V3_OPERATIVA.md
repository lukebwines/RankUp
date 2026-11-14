# RankUP V3 · Dashboard operativa e controlli (v1.0.31)

## Dashboard A+B
La Dashboard conserva gli indicatori navigabili già esistenti e aggiunge:
- «Attività che richiedono attenzione», massimo cinque iniziali, con vista completa filtrabile per CdS, priorità e ambito; il filtro «Assegnate a me» è riferito alle **segnalazioni con assegnatario effettivamente registrato**, non alle attività automatiche.
- Accesso diretto a Commissioni, Valutazioni, Verbali, Graduatorie e Segnalazioni per il bando scelto.
- «Verifica CdS»: richiama i controlli esistenti della graduatoria senza finalizzarla.
- Avanzamento per fase: **rapporto fra CdS/sedi verificabili**; non è una stima percentuale di avanzamento amministrativo.
- Ultime procedure, attività registrate, segnalazioni aperte, scadenze solo se presenti nei Riferimenti del bando, accessi rapidi.
- Tema scuro, responsive, nessuna modifica automatica dei dati.

## Import PICA
- Il codice è letto dalla prima riga del file PICA; il nome del file non modifica il codice.
- Se esiste una procedura **in preparazione dello stesso A.A. con codice diverso**, e il codice PICA non corrisponde già a una procedura preparata, il sistema richiede conferma esplicita dell'Admin per importare come **nuovo bando distinto**. Non sposta commissioni, atti, PDF o incarichi da un codice all'altro.
- Un file PICA con **impronta SHA-256 identica** a un file già importato in altro bando viene rifiutato.
- Una sovrapposizione elevata degli **ID PICA** tra bandi viene segnalata come **indizio da verificare**, senza dedurre che sia un duplicato. Solo il confronto dei dati ufficiali consente di stabilirlo.
- Se il codice è già stato importato, il blocco preesistente rimane attivo. Le verifiche essenziali vengono ripetute al commit in transazione.

## Diagnostica e registro (solo Admin)
- «Diagnostica e registro» nel menu: `PRAGMA integrity_check`, `foreign_key_check`, controlli relazionali e duplicati equivalenti, divergenze fra anagrafica commissioni e composizioni nei bandi, ultime operazioni già registrate e disponibilità dei backup di migrazione.
- Le divergenze di composizione **non sono per forza errori**: le nomine possono differire legittimamente tra bandi. Non esistono correzioni automatiche.
- Le rettifiche dell'anagrafica sono presenti nel registro generale anche quando non sono collegate a un bando.

## Backup e ripristino
- Gli snapshot automatici prima di una **migrazione di schema** e il backup della procedura di azzeramento erano già presenti e restano operativi.
- `scripts/audit_database.py` controlla un DB in sola lettura.
- `scripts/verify_restore.py` crea una **prova di copia e riapertura SQLite in cartella temporanea** e verifica facoltativamente gli originali PICA. Prevede un limite di dimensione e un controllo di spazio libero.
- Il test dello script **non sostituisce il ripristino completo del backup Home Assistant**, che comprende add-on, configurazioni, file allegati e strumenti esterni. Questa verifica richiede un'istanza separata e una copia reale del backup.

## Sicurezza e collaudo
- I rapporti non presentano nominativi dei candidati, non modificano il DB e distinguono le evidenze dagli avvisi.
- Admin e operatori vedono solo azioni consentite, controllate anche dalle API.
- Aggiornare un'installazione con dati reali soltanto dopo un backup verificato e un test dell'add-on su una copia.
