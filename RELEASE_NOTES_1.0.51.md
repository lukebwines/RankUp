# RankUP 1.0.51 — protezioni procedimentali

**Aggiornamento dell'add-on Home Assistant.** Le modifiche non importano, sostituiscono, cancellano o ricostruiscono i bandi esistenti. Prima dell'aggiornamento di produzione è comunque raccomandato un backup Home Assistant verificato.

## Nuove regole

- **Parità:** la vecchia scelta di un vincitore non risolve implicitamente una parità. La decisione documentata della Commissione è registrata dall'Admin tramite *Gestisci parità*, collegando l'ordine alla versione delle schede di valutazione. La rettifica delle schede rende necessaria una nuova verifica.
- **Regressioni:** la riapertura delle graduatorie CdS distingue *tecnica* e *sostanziale*. La seconda richiede già alla riapertura gli estremi del documento della Commissione. Se una correzione inizialmente tecnica modifica il merito, la nuova finalizzazione è bloccata in assenza di riferimento documentale integrativo registrato dall'Admin.
- **Approvazione generale:** verifica nuovamente i dati congelati, le schede, la risoluzione delle parità e le impronte SHA-256 dei dati e dei PDF definitivi. In presenza di anomalie, l'approvazione è bloccata senza modificare gli atti.
- **Tracciabilità:** storico amministrativo e operazioni critiche mantengono il comportamento transazionale già presente; aggiunto evento append-only per riferimenti documentali integrativi. Non equivale a una certificazione esterna di conformità amministrativa.
- **Segnalazioni:** riutilizza la sezione esistente. Gli eventi automatici «RankUP · Sistema» sono riservati all'Admin. Gravità **CRITICO**, **ATTENZIONE** e **INFORMATIVO**, con icone e colori distinguibili anche senza vedere il colore. Le parità irrisolte vengono segnalate e le segnalazioni correnti possono essere prese in carico.
- **Contatori:** il menu Segnalazioni mostra il numero di pratiche aperte o in carico (in rosso se vi sono critici) e il selettore dei bandi ne mostra il numero per ciascun bando. Le pratiche risolte e quelle relative a bandi archiviati non entrano nel contatore; lo storico rimane accessibile.

## Controlli di rilascio

La pipeline GitHub Actions esegue i test `pytest`, la verifica di sintassi JavaScript e la validazione dello ZIP sorgenti. I test utilizzano esclusivamente dati sintetici e non modificano il database di produzione.

## Aggiornamento Home Assistant

Una volta che la versione **1.0.51** è pubblicata su `main`, aprire **Impostazioni → Componenti aggiuntivi → RankUp** e cercare l'aggiornamento. Se non compare, aggiornare la lista dei repository dell'Add-on Store. **Eseguire prima un backup**, poi aggiornare e verificare le funzioni e la presenza dei dati. Non è necessario importare nuovamente PICA.

## Limiti e responsabilità

Il controllo automatico non sostituisce l'esame degli atti e delle schede ufficiali da parte della Commissione o dell'Admin. I test automatizzati non costituiscono un collaudo documentale del procedimento concreto.
