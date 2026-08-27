# Deploy gratuito su Oracle Cloud (Always Free)

Percorso scelto: **VM sempre accesa + polling + SQLite sul disco della VM**.
Niente webhook, niente dominio, niente porte aperte. Costo: 0 €/mese.

> Il README parla di Railway: quel percorso oggi non è più gratuito
> (5 $ di credito prova, poi ~5 $/mese). Render è gratis ma si spegne dopo
> 15 min e non ha disco persistente: le partite andrebbero perse.

---

## 01 · BotFather (3 min)

1. `@BotFather` → `/newbot`
2. Nome visualizzato (es. `Calcetto Padova`), username che finisce in `bot`
3. Salva il token `8123456789:AAF…` — è la password del bot, non finisce mai su GitHub
4. `/setcommands` → incolla:

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

## 02 · Prova sul PC (5 min, PowerShell)

```powershell
cd C:\PRJ\Claude\Progetti\BotCalcetto
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:TELEGRAM_TOKEN = "8123456789:AAF…"
.\.venv\Scripts\python.exe bot.py
```

Niente `Activate.ps1`: per impostazione predefinita PowerShell blocca gli script
(«L'esecuzione di script è disabilitata nel sistema in uso»). Chiamare
direttamente `.venv\Scripts\python.exe` aggira il problema. Per sistemare la
policy una volta per tutte, senza privilegi di amministratore:
`Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.

Su Windows serve anche `tzdata` (ora è in `requirements.txt`): il sistema non
ha il database dei fusi orari, quindi senza quel pacchetto `zoneinfo` fallisce
con `ZoneInfoNotFoundError: 'No time zone found with key Europe/Rome'`.

Deve stampare `Avvio in modalità polling`. `/start` in privato al bot.
**Un token, un'istanza**: se gira anche sulla VM, Telegram risponde
`Conflict: terminated by other getUpdates`.

## 03 · GitHub (5 min)

```bash
git init -b main
git add .
git commit -m "bot calcetto"
git remote add origin https://github.com/Pertezen/calcetto-bot.git
git push -u origin main
```

`.gitignore` esclude già `.env` e `*.db`. Repo pubblico = clone sulla VM in una
riga (non ci sono segreti nel codice). Se lo vuoi privato, usa `scp` (passo 06).

## 04 · Account Oracle Cloud (15 min)

<https://www.oracle.com/cloud/free> — carta richiesta solo per verifica identità
(preautorizzazione ~1 €, nessun addebito; sforando, le risorse vengono fermate,
non fatturate).

Due scelte irreversibili:
- **Home Region**: Italy Central (Milan), Germany Central (Frankfurt) o
  Netherlands Northwest (Amsterdam). Da qui dipende la disponibilità ARM.
- Email dell'account = amministratore del tenancy.

Always Free nel 2026: **2 OCPU / 12 GB ARM** (dimezzati rispetto a 4/24),
2 micro-VM AMD, 200 GB disco. Per il bot bastano 1 OCPU / 6 GB.

## 05 · Creare la VM (10 min)

Compute → Instances → Create instance:

| Campo | Valore |
|---|---|
| Name | `botcalcetto` |
| Image | Canonical Ubuntu 24.04 (aarch64 se ARM) |
| Shape | `VM.Standard.A1.Flex` — 1 OCPU / 6 GB, etichetta *Always Free eligible* |
| Networking | subnet **pubblica**, *Assign a public IPv4 address* attivo |
| SSH keys | *Generate a key pair for me* → **Save private key** |

⚠️ La chiave privata si scarica **solo in quel momento**. Salvala in
`C:\Users\<utente>\.ssh\botcalcetto.key`.

**«Out of host capacity»** (frequente sulle ARM gratuite):
1. cambia Availability Domain (AD-1/2/3)
2. riprova più tardi
3. oppure shape `VM.Standard.E2.1.Micro` (AMD, 1 GB, quasi sempre libera,
   immagine Ubuntu x86_64) — per questo bot basta.

Stato **Running** → copia il **Public IP**.

## 06 · Installare il bot (10 min)

```powershell
# solo la prima volta, su Windows
icacls C:\Users\<utente>\.ssh\botcalcetto.key /inheritance:r /grant:r "$($env:USERNAME):(R)"
ssh -i C:\Users\<utente>\.ssh\botcalcetto.key ubuntu@<IP>
```

Sulla VM, in due comandi:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/Pertezen/calcetto-bot.git ~/botcalcetto
cd ~/botcalcetto && bash setup_vm.sh
```

`setup_vm.sh` fa tutto il resto: dipendenze di sistema, ambiente virtuale,
`.env` (chiede il token e lo scrive con permessi 600), servizio systemd,
backup notturno, e alla fine verifica che il bot sia davvero attivo.
È idempotente: si può rilanciare senza rompere niente.

I passi 06 e 07 qui sotto descrivono cosa fa, per quando serve metterci le mani.

`WEBHOOK_URL` **non va messa**: è la sua assenza che tiene il bot in polling.
`DB_PATH` assoluto, così il database non dipende dalla cartella corrente.

Alternativa a git clone (repo privato), da Windows:

```powershell
scp -i C:\Users\<utente>\.ssh\botcalcetto.key -r C:\PRJ\Claude\Progetti\BotCalcetto\* ubuntu@<IP>:~/botcalcetto/
```

Prova a mano:

```bash
cd ~/botcalcetto && set -a; . ./.env; set +a && .venv/bin/python bot.py
```

## 07 · systemd — riparte da solo (5 min)

```bash
sudo tee /etc/systemd/system/botcalcetto.service > /dev/null <<'EOF'
[Unit]
Description=Bot Calcetto Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/botcalcetto
EnvironmentFile=/home/ubuntu/botcalcetto/.env
ExecStart=/home/ubuntu/botcalcetto/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now botcalcetto
```

Comandi quotidiani:

```bash
systemctl status botcalcetto        # sta girando?
journalctl -u botcalcetto -f        # log in tempo reale
sudo systemctl restart botcalcetto  # dopo ogni modifica a codice o .env
```

`EnvironmentFile` legge coppie `CHIAVE=valore` secche: niente `export`,
niente virgolette attorno al token.

Aggiornare il bot:

```bash
cd ~/botcalcetto && git pull && sudo systemctl restart botcalcetto
```

Backup notturno del database (7 copie a rotazione):

```bash
mkdir -p ~/backup
(crontab -l 2>/dev/null; echo "30 4 * * * cp /home/ubuntu/botcalcetto/calcetto.db /home/ubuntu/backup/calcetto-\$(date +\%u).db") | crontab -
```

## 08 · Nel gruppo (2 min)

1. Gruppo → Aggiungi membri → username del bot
2. `/start` nel gruppo
3. Facoltativo: amministratore col solo permesso di **fissare i messaggi**
4. `/nuova_partita venerdì 19:00 | Campetto Centro | 10`

La privacy di gruppo non va toccata: il bot vede solo i comandi `/` e i bottoni.

---

## Se qualcosa non va

| Sintomo | Causa quasi sempre | Cosa fare |
|---|---|---|
| Non risponde | servizio morto / token errato | `systemctl status botcalcetto`, `journalctl -u botcalcetto -n 50` |
| `Conflict: terminated by other getUpdates` | due istanze con lo stesso token | tienine accesa una sola |
| `Manca la variabile TELEGRAM_TOKEN` | systemd non legge il `.env` | percorso in `EnvironmentFile`, niente virgolette/`export` |
| Partite sparite | `DB_PATH` relativo | percorso assoluto + restart |
| «Non trovo la partita» | ID di un altro gruppo | normale: ogni gruppo vede le sue |
| SSH Permission denied | utente o permessi chiave | `ubuntu@` (Ubuntu) o `opc@` (Oracle Linux); rilancia `icacls` |
| Out of host capacity | ARM esaurite | altro AD, più tardi, o E2.1.Micro |
| Orari sfasati di un'ora | manca `TIMEZONE` | `TIMEZONE=Europe/Rome` + restart |

Promemoria: Oracle può considerare abbandonati gli account **inattivi da 30+
giorni** — entra in console ogni tanto. E tieni la shape entro 2 OCPU / 12 GB.
