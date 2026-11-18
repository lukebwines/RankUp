# CO Veneto — audit del tracciato e della ricevuta (v1.0.45)

**Fonte:** il PDF e l'XML della stessa comunicazione forniti dall'utente.
Le informazioni di esempio NON vengono copiate nei nuovi rapporti.

| Dato | Fonte RankUP | Stato |
| --- | --- | --- |
| Datore/legale rappresentante/sede legale | Configurazione Admin | Obbligatori e da verificare |
| Sede di lavoro | Configurazione Admin | **[DA VERIFICARE]** per ogni sede: il valore globale potrebbe non valere per tutti i CdS |
| Anagrafica, nascita, cittadinanza | PICA e dati confermati | Codici da validare |
| Domicilio | Residenza PICA modificabile | Comune, CAP, via, codice catastale |
| Titolo | Livello 70 proposto e modificabile | [DA VERIFICARE] per titolo effettivo |
| Data inizio | INIZIO PICA | Confermare data effettiva del contratto |
| Data fine | 30/09 dell'anno conclusivo A.A. | Confermare eventuali eccezioni |
| Tipo contratto | B.03.00 | Come dichiarato dall'utente |
| Previdenza, PAT, ISTAT, CCNL, livello, tipo orario | Amministrazione | [DA VERIFICARE] per specifico incarico |
| Retribuzione lorda | Operatore, per singolo incarico | Obbligatoria |
| Tipo comunicazione | 01 | Solo nuovo rapporto |
| Codice comunicazione | 16 zeri | Codice definitivo assegnato dal portale |
| Data invio | Generazione locale | Formato XML senza suffisso di fuso orario |
| Protocollo | CO Veneto dopo l'invio | Non va inventato |

## Note

Nel **PDF della ricevuta** compare il campo Note con riferimenti a CdS, sede,
insegnamento, A.A. e bando. Nel **relativo XML restituito da CO Veneto**, il campo
NON è presente. Non è quindi tecnicamente dimostrato che il suo valore
possa essere importato nell'XML: non si aggiunge un nodo estraneo allo standard.

RankUP genera e precompila le Note dai dati verificati del procedimento,
le rende modificabili e copiabili e conserva la versione di ogni generazione
nel registro eventi. Se necessario, le Note vanno riportate nel portale con
la relativa funzione. Il semplice download ZIP **non attesta l'invio delle Note**.

## Altri campi della ricevuta PDF

Il codice fiscale del sottoscrittore dell'Accordo di servizio compare nel PDF
ma non nel corrispondente XML; non si copia dalla ricevuta di esempio.
Il portale può derivarlo dalle credenziali/accordo: **[DA VERIFICARE]**.

Le sezioni co-obbligato, proroga, trasformazione, cessazione, distacco e i dati
di soggiorno sono vuoti nel modello di assunzione fornito e non vanno
compilati con valori di fantasia. Per cittadinanza estera, i dati condizionati
non sono supportati: il generatore blocca la comunicazione e richiede verifica.

Per nuovi invii la validazione finale resta **Verifica file** di CO Veneto.
Il generatore non invia direttamente comunicazioni e lo ZIP contiene solo XML.


## Riscontro sul Manuale CO Veneto 4.0 (08/01/2024)

Fonte ufficiale Regione Veneto: Manuale Utente CO Veneto, versione 4.0,
pagina 28, sezione «Scheda Dati Invio» (figura 4.18):
https://www.cliclavoroveneto.it/normativa-e-manuali

L'«Osservazione importante» specifica che le Note **non rientrano nei
tracciati standard nazionali**, **non sono trasmesse agli altri enti nella
cooperazione applicativa** e sono una funzione dell'implementazione veneta,
riportata nella stampa PDF. La pagina 77, §12.1 «Formato del file», richiede
ZIP di XML conformi esattamente al tracciato pubblicato. Pertanto
**non c'è una modalità documentata per importare Note con XML massivo**.
La presenza delle Note in una ricevuta originata da inserimento a video
non dimostra che possano essere esportate dal nostro ZIP.
