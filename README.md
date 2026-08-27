# ⚽ Bot Calcetto

Bot Telegram per organizzare le partite dentro un gruppo. Chiunque crea una
partita, gli altri si iscrivono con un bottone, l'organizzatore fa le squadre.

---

## Cosa fa

| Comando | Chi | Cosa |
|---|---|---|
| `/nuova_partita venerdì 19:00 \| Campetto Centro \| 10` | chiunque | crea la partita, genera l'ID `CALC-XXXX` |
| `/iscrivimi ID` | chiunque | si iscrive (o va in riserva se è pieno) |
| `/esco ID` | iscritti | si ritira; la prima riserva entra da sola |
| `/lista ID` | chiunque | ristampa la scheda della partita |
| `/partite` | chiunque | tutte le partite aperte del gruppo |
| `/squadre ID` | organizzatore | divide i titolari in Bianchi e Rossi |
| `/modifica ID ora\|posto\|posti valore` | organizzatore | cambia i dati |
| `/annulla ID` | organizzatore | annulla la partita, la scheda resta come promemoria |
| `/elimina ID` | organizzatore | la cancella del tutto: partita, iscritti e scheda |

Sotto ogni partita ci sono i bottoni **✅ Ci sono / 🚪 Mi ritiro / 🎽 Fai le
squadre**: nella pratica i comandi non li userà quasi nessuno, si preme e basta.
La scheda si aggiorna da sola a ogni iscrizione.

Se nel gruppo c'è una sola partita aperta, l'ID puoi anche non scriverlo.

**Formati data accettati:** `19:00` · `domani 21:30` · `venerdì 19:00` ·
`30/08 19:00` · `30/08/2026 19:00`

**Pulizia automatica:** 3 ore dopo l'orario di inizio la partita sparisce da
sola — riga nel database e scheda nel gruppo. Nessun comando da dare, nessuna
lista che si allunga all'infinito. Si cambia con la variabile
`PURGE_AFTER_HOURS`.

**Extra rispetto alla spec:** lista riserve automatica. Se la partita è piena
l'undicesimo finisce in riserva, e se qualcuno molla entra al suo posto con un
avviso nel gruppo. È il caso che succede ogni settimana.

---

## Cosa devi fare tu

### 1 · Creare il bot su Telegram — 3 minuti

1. Apri Telegram e scrivi a **@BotFather**
2. `/newbot`
3. Nome visualizzato: es. `Calcetto Padova`
4. Username: deve finire in `bot`, es. `calcetto_padova_bot`
5. BotFather ti dà un **token** tipo `8123456789:AAF...` → tienilo da parte

Non serve toccare le impostazioni privacy: il bot legge solo i comandi con `/`
e i bottoni, non i messaggi del gruppo.

### 2 · Provarlo sul tuo PC — 5 minuti

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN="il-token-di-botfather"
python bot.py
```

Parte in polling (il PC deve restare acceso, ma solo per questa prova).
Scrivi al bot in privato `/start` per vedere se risponde.

### 3 · Metterlo in GitHub — 3 minuti

```bash
git init
git add .
git commit -m "bot calcetto"
git remote add origin https://github.com/Pertezen/calcetto-bot.git
git push -u origin main
```

Il `.gitignore` esclude già il database e il file `.env`. **Il token non deve
mai finire nel repo.**

### 4 · Deploy su Railway — 10 minuti

1. Vai su [railway.app](https://railway.app) e accedi con GitHub
2. **New Project → Deploy from GitHub repo** → scegli `calcetto-bot`
3. Aspetta il primo build (fallirà: manca il token, è normale)
4. Tab **Variables**, aggiungi:
   - `TELEGRAM_TOKEN` = il token di BotFather
   - `DB_PATH` = `/data/calcetto.db`
   - `TIMEZONE` = `Europe/Rome`
5. Tab **Settings → Networking → Generate Domain**. Copia l'URL che ti dà
   (tipo `https://calcetto-bot-production.up.railway.app`)
6. Torna in **Variables** e aggiungi:
   - `WEBHOOK_URL` = quell'URL, **senza slash finale**
7. Tab **Settings → Volumes → Add Volume**, mount path `/data`
8. Redeploy

Appena `WEBHOOK_URL` è impostata il bot passa da solo in modalità webhook: da
qui in poi il tuo PC può stare spento.

> Il volume al punto 7 è importante: senza, Railway azzera il disco a ogni
> deploy e perderesti le partite in corso.

### 5 · Metterlo nel gruppo — 1 minuto

1. Apri il gruppo del calcetto → **Aggiungi membri** → cerca il tuo bot
2. Scrivi `/start` per vedere l'aiuto
3. Facoltativo: rendilo **amministratore** con il solo permesso di *pin dei
   messaggi*, così fissa in alto la partita del momento

### 6 · Menù comandi (facoltativo, ma fa la differenza)

Da @BotFather: `/setcommands` → scegli il bot → incolla:

```
nuova_partita - Crea una partita
partite - Le partite aperte
lista - Chi gioca
iscrivimi - Ti iscrivi
esco - Ti ritiri
squadre - Dividi le squadre
modifica - Cambia ora, posto o posti
annulla - Annulla la partita
elimina - Cancella la partita del tutto
```

Così i comandi compaiono nel menù ⌘ della chat e nessuno deve ricordarseli.

---

## Se qualcosa non va

| Sintomo | Causa quasi sempre |
|---|---|
| Il bot non risponde nel gruppo | `WEBHOOK_URL` ha lo slash finale, o non è l'URL pubblico di Railway |
| Funziona in locale ma non su Railway | manca una variabile — controlla i log nel tab Deployments |
| Le partite spariscono dopo un deploy | manca il volume su `/data`, o `DB_PATH` non punta lì |
| «Non trovo la partita» | l'ID appartiene a un altro gruppo: ogni gruppo vede solo le sue |

Log in tempo reale: Railway → il servizio → **Deployments** → **View Logs**.

---

## Struttura

```
core.py      logica e database — nessuna dipendenza da Telegram, testabile da solo
bot.py       comandi, bottoni, rendering dei messaggi
```

Il database è SQLite. Se un giorno il bot gira su decine di gruppi si migra a
Postgres cambiando solo `core.py` — `bot.py` non se ne accorge.
