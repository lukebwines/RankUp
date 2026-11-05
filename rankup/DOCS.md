# RankUp — istruzioni del prototipo 0.1.0

**Stato: sperimentale. Non caricare candidature reali finché non saranno completati ruoli, audit, backup/ripristino e verifica di sicurezza.**

## Installazione
1. In Home Assistant, aprire **Impostazioni > Componenti aggiuntivi / App > Negozio / Installa app**.
2. Nel menu del negozio aprire **Repository** e aggiungere https://github.com/lukebwines/RankUp.
3. Cercare RankUp, installarlo e avviarlo.
4. Aprire la scheda di RankUp tramite **Apri interfaccia web**.
5. Se il repository o l'app non compare, aggiornare il negozio e verificare i log del Supervisor.

Il Dockerfile costruisce il container sul dispositivo: la prima installazione richiede la disponibilità delle dipendenze Python.

## Uso iniziale
1. Dal menu **Bandi**, selezionare un'esportazione PICA di formato .xls o .xlsx.
2. Premere **Analizza file**. Il codice bando deve comparire dalla prima riga (esempio di formato: "univr 26-02 - Esportazione dati").
3. Controllare il riepilogo di CdS, incarichi e candidature e confermare l'importazione.
4. Selezionare il bando attivo dal menu laterale.
5. Aprire **Incarichi** e **Candidature** per verificare l'associazione.
6. Aprire **Commissioni** e importare il file .xlsx della stessa procedura. RankUp verifica il riferimento al bando e impedisce composizioni contrastanti dello stesso CdS.

## Limitazioni
- La prima importazione PICA è definitiva: nessuna seconda importazione incrementale o sovrascrittura automatica.
- Le commissioni già importate non sono ancora modificabili dall'interfaccia.
- I documenti PDF, i verbali e le graduatorie non sono ancora generabili.
- I ruoli amministratore/operatore non sono ancora disponibili. La prima milestone usa l'accesso tramite Home Assistant Ingress, senza ulteriore profilazione interna.
- Non viene svolto alcun controllo preliminare dei requisiti.

## Conservazione
Il database e gli originali dei file importati si trovano nel volume persistente /data dell'add-on. Nessun file contenente candidature o valutazioni viene inserito nel repository GitHub.

## Errori frequenti
- **Codice bando non riconosciuto:** verificare il testo nella prima riga del file PICA.
- **Bando già importato:** l'importazione non sovrascrive i dati esistenti.
- **Commissioni non associate:** verificare la corrispondenza del codice bando.
- **Errore di lettura .xls:** assicurarsi che il file sia un'autentica esportazione Excel PICA e non un CSV rinominato.
