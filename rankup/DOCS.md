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
