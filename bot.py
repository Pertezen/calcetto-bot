"""
Bot Telegram per organizzare i calcetti.
Avvio: python bot.py
"""

import asyncio
import difflib
import html
import logging
import os

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import core
from core import ParseError

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("calcetto")

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 8080))

# Quanto tempo dopo il fischio d'inizio la partita sparisce da sola,
# e ogni quanto il bot passa a controllare.
PURGE_AFTER_HOURS = int(os.environ.get("PURGE_AFTER_HOURS", 3))
PURGE_EVERY_SECONDS = 600

# Quanto resta a video un avviso scritto nel gruppo prima di autocancellarsi.
EPHEMERAL_SECONDS = 25

# Tutti i comandi che il bot conosce, per suggerire quello giusto a chi sbaglia.
COMANDI = [
    "start", "help", "aiuto", "nuova_partita", "nuova", "iscrivimi", "ci_sono",
    "esco", "cancella_iscrizione", "lista", "chi_gioca", "partite",
    "partite_aperte", "squadre", "modifica", "annulla", "cancella_partita",
    "elimina", "elimina_partita", "amico", "porto_un_amico", "togli_amico",
    "tolgo_un_amico",
]

HELP = """⚽ <b>Bot Calcetto</b>

<b>Creare una partita</b>
<code>/nuova_partita venerdì 19:00 | Campetto Centro | 10</code>
(data | posto | numero giocatori)

<b>Comandi</b>
/partite — le partite aperte
/lista <code>ID</code> — chi gioca
/iscrivimi <code>ID</code> — ti iscrivi
/esco <code>ID</code> — ti ritiri
/amico <code>ID</code> — porti un amico (quanti vuoi)
/togli_amico <code>ID</code> — ne togli uno
/squadre <code>ID</code> — divide le squadre (solo organizzatore)
/modifica <code>ID ora|posto|posti valore</code>
/annulla <code>ID</code> — annulla la partita, la scheda resta
/elimina <code>ID</code> — la cancella del tutto (solo organizzatore)

Le partite spariscono da sole 3 ore dopo l'orario di inizio.

Il modo più veloce resta comunque premere i bottoni sotto alla partita.
Se c'è una sola partita aperta, l'ID puoi anche non scriverlo.

<i>Errori e avvisi te li mando qui in privato, per non intasare il gruppo.</i>"""


# --------------------------------------------------------------------------
# Rendering della "card" partita
# --------------------------------------------------------------------------

def esc(text) -> str:
    return html.escape(str(text))


def render_card(match) -> tuple[str, InlineKeyboardMarkup | None]:
    signups = core.get_signups(match["id"])
    starters, reserves = core.split_roster(match, signups)
    max_players = match["max_players"]

    if match["status"] == "cancelled":
        return (
            f"❌ <b>Partita annullata</b>\n"
            f"<s>{esc(core.format_when(match['when_ts']))} — {esc(match['place'])}</s>\n"
            f"<code>{match['id']}</code>",
            None,
        )

    lines = [
        f"⚽ <b>Calcetto — {esc(core.format_when(match['when_ts']))}</b>",
        f"📍 {esc(match['place'])}",
        f"👥 <b>{len(starters)}/{max_players}</b> giocatori",
        "",
    ]

    if starters:
        for i, s in enumerate(starters, 1):
            lines.append(f"{i}. {esc(s['label'])}")
    else:
        lines.append("<i>Ancora nessuno. Rompi il ghiaccio.</i>")

    if reserves:
        lines.append("")
        lines.append("<b>Riserve</b>")
        for i, s in enumerate(reserves, 1):
            lines.append(f"{i}. {esc(s['label'])}")

    if match["teams"]:
        team_a, team_b = match["teams"].split("||")
        lines += [
            "",
            "⚪ <b>Bianchi</b>",
            *(f"· {esc(n)}" for n in team_a.split("|") if n),
            "",
            "🔴 <b>Rossi</b>",
            *(f"· {esc(n)}" for n in team_b.split("|") if n),
        ]

    missing = max_players - len(starters)
    if missing > 0 and not match["teams"]:
        lines += ["", f"<i>Mancano {missing} giocatori.</i>"]
    elif missing <= 0 and not match["teams"]:
        lines += ["", "<i>Siamo al completo. </i>"]

    lines += [
        "",
        f"<code>{match['id']}</code> · organizza {esc(match['organizer_name'])}",
    ]

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Ci sono", callback_data=f"join:{match['id']}"),
            InlineKeyboardButton("🚪 Mi ritiro", callback_data=f"leave:{match['id']}"),
        ],
        [
            InlineKeyboardButton("➕ Porto un amico", callback_data=f"guest:{match['id']}"),
            InlineKeyboardButton("➖ Tolgo un amico", callback_data=f"unguest:{match['id']}"),
        ],
        [
            InlineKeyboardButton("🎽 Fai le squadre", callback_data=f"teams:{match['id']}"),
        ],
    ])
    return "\n".join(lines), keyboard


async def refresh_card(context: ContextTypes.DEFAULT_TYPE, match):
    """Riscrive il messaggio della partita con i dati aggiornati."""
    if not match["card_msg_id"]:
        return
    text, keyboard = render_card(match)
    try:
        await context.bot.edit_message_text(
            chat_id=match["chat_id"],
            message_id=match["card_msg_id"],
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("Non riesco ad aggiornare la card %s: %s", match["id"], e)


async def retire_card(bot, match, nota: str):
    """
    Toglie dal gruppo la scheda di una partita che non esiste più.
    Telegram lascia cancellare i propri messaggi solo entro 48 ore: più in
    là si può ancora modificarli, e allora al posto della scheda resta
    una riga sola.
    """
    msg_id = match["card_msg_id"]
    if not msg_id:
        return
    try:
        await bot.unpin_chat_message(chat_id=match["chat_id"], message_id=msg_id)
    except Exception:
        pass  # non era fissata, o il bot non è admin
    try:
        await bot.delete_message(chat_id=match["chat_id"], message_id=msg_id)
        return
    except Exception:
        pass
    try:
        await bot.edit_message_text(
            chat_id=match["chat_id"],
            message_id=msg_id,
            text=nota,
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as e:
        log.warning("Scheda di %s non rimossa: %s", match["id"], e)


# --------------------------------------------------------------------------
# Helper
# --------------------------------------------------------------------------

async def _delete_later(bot, chat_id, message_id, seconds):
    """Cancella un messaggio dopo un po'. Se fallisce, pazienza."""
    await asyncio.sleep(seconds)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def notify(update, context, text, seconds=EPHEMERAL_SECONDS,
                 drop_command=True, keyboard=None):
    """
    Avviso destinato a una persona sola.

    Telegram non sa mostrare un messaggio a un solo membro di un gruppo, quindi:
    in chat privata risponde e basta; nel gruppo prova a scrivere in privato a
    chi ha lanciato il comando e, se quella persona non ha mai aperto una chat
    col bot, scrive nel gruppo un messaggio che si cancella da solo. Il comando
    che ha causato l'avviso sparisce, se il bot ha i permessi per farlo.
    """
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if drop_command and msg:
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=msg.message_id)
        except Exception as e:
            # Quasi sempre: il bot non è amministratore, o gli manca il permesso
            # di eliminare i messaggi. Lo scrivo nei log, così si capisce perché
            # il comando sbagliato è rimasto lì.
            log.warning(
                "Non riesco a cancellare il comando in %s: %s "
                "(serve il permesso «Elimina messaggi»)", chat.id, e
            )

    try:
        await context.bot.send_message(
            chat_id=user.id, text=text, parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return
    except (Forbidden, BadRequest):
        pass  # non ha mai avviato il bot in privato: si ripiega sul gruppo

    mention = f'<a href="tg://user?id={user.id}">{esc(display_name(user))}</a>'
    sent = await context.bot.send_message(
        chat_id=chat.id,
        text=f"{mention} {text}\n\n<i>Aprendo una chat privata col bot, "
             f"questi avvisi arrivano lì.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    asyncio.create_task(
        _delete_later(context.bot, chat.id, sent.message_id, seconds)
    )


async def resolve_match(update: Update, context, args):
    """
    Trova la partita dall'ID passato come argomento.
    Se non c'è ID e nel gruppo c'è una sola partita aperta, usa quella.
    """
    chat_id = update.effective_chat.id
    if args:
        match = core.get_match(args[0])
        if not match or match["chat_id"] != chat_id:
            await notify(
                update, context,
                f"Non trovo la partita {core.normalize_id(args[0])}."
            )
            return None
        return match

    aperte = core.open_matches(chat_id)
    if len(aperte) == 1:
        return aperte[0]
    if not aperte:
        await notify(
            update, context,
            "Non c'è nessuna partita aperta. Creane una con /nuova_partita."
        )
    else:
        await notify(
            update, context,
            "Ci sono più partite aperte, dimmi quale (es. <code>/lista CALC-1234</code>).\n"
            "Vedi la lista con /partite.",
        )
    return None


async def is_group_admin(context, chat_id, user_id) -> bool:
    """True se la persona è admin o creatore del gruppo."""
    try:
        membro = await context.bot.get_chat_member(chat_id, user_id)
        return getattr(membro, "status", "") in ("administrator", "creator")
    except Exception:
        return False


def display_name(user) -> str:
    return user.full_name or user.username or f"Giocatore {user.id}"


# --------------------------------------------------------------------------
# Comandi
# --------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await notify(update, context, HELP, seconds=60)


async def cmd_nuova(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]

    if len(parts) != 3:
        await notify(
            update, context,
            "Mi servono tre cose separate da <code>|</code>:\n\n"
            "<code>/nuova_partita venerdì 19:00 | Campetto Centro | 10</code>\n\n"
            "Per la data va bene anche <code>domani 21:00</code> o <code>30/08 19:00</code>.",
        )
        return

    when_text, place, players_text = parts
    try:
        when = core.parse_when(when_text)
        try:
            max_players = int("".join(c for c in players_text if c.isdigit()))
        except ValueError:
            raise ParseError("Non ho capito il numero di giocatori.")

        user = update.effective_user
        match_id = core.create_match(
            chat_id=update.effective_chat.id,
            organizer_id=user.id,
            organizer_name=display_name(user),
            when=when,
            place=place,
            max_players=max_players,
        )
    except ParseError as e:
        avevi = f"\n\nAvevi scritto: <code>{esc(raw)}</code>" if raw else ""
        await notify(update, context, f"{e}{avevi}")
        return

    match = core.get_match(match_id)
    text, keyboard = render_card(match)
    sent = await update.effective_message.reply_text(
        text, reply_markup=keyboard, parse_mode=ParseMode.HTML
    )
    core.set_card_msg(match_id, sent.message_id)

    try:
        await context.bot.pin_chat_message(
            chat_id=update.effective_chat.id,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception:
        pass  # il bot non è admin, pazienza


async def cmd_iscrivimi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = await resolve_match(update, context, context.args)
    if not match:
        return
    await do_join(update, context, match, update.effective_user)


async def cmd_esco(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = await resolve_match(update, context, context.args)
    if not match:
        return
    await do_leave(update, context, match, update.effective_user)


async def cmd_lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = await resolve_match(update, context, context.args)
    if not match:
        return
    text, keyboard = render_card(match)
    sent = await update.effective_message.reply_text(
        text, reply_markup=keyboard, parse_mode=ParseMode.HTML
    )
    core.set_card_msg(match["id"], sent.message_id)


async def cmd_partite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    aperte = core.open_matches(update.effective_chat.id)
    if not aperte:
        await notify(
            update, context,
            "Nessuna partita in programma. Creane una con /nuova_partita."
        )
        return

    lines = ["📅 <b>Partite aperte</b>", ""]
    for m in aperte:
        iscritti = len(core.get_signups(m["id"]))
        lines.append(
            f"<code>{m['id']}</code> — {esc(core.format_when(m['when_ts']))}\n"
            f"📍 {esc(m['place'])} · {min(iscritti, m['max_players'])}/{m['max_players']}"
        )
        lines.append("")
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def cmd_squadre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = await resolve_match(update, context, context.args)
    if not match:
        return
    await do_teams(update, context, match, update.effective_user)


async def cmd_modifica(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await notify(
            update, context,
            "Come si usa:\n"
            "<code>/modifica CALC-1234 ora venerdì 20:30</code>\n"
            "<code>/modifica CALC-1234 posto Campetto Nord</code>\n"
            "<code>/modifica CALC-1234 posti 12</code>",
        )
        return

    # Il primo argomento può essere l'ID oppure direttamente il campo
    if core.get_match(args[0]):
        match, field, value = core.get_match(args[0]), args[1].lower(), " ".join(args[2:])
    else:
        match = await resolve_match(update, context, [])
        if not match:
            return
        field, value = args[0].lower(), " ".join(args[1:])

    if match["chat_id"] != update.effective_chat.id:
        await notify(update, context, "Quella partita non è di questo gruppo.")
        return
    if update.effective_user.id != match["organizer_id"]:
        await notify(
            update, context,
            f"Solo {esc(match['organizer_name'])} può modificare questa partita.",
        )
        return
    if not value:
        await notify(update, context, "Manca il nuovo valore.")
        return

    try:
        if field in ("ora", "orario", "data", "quando"):
            core.update_match(match["id"], when_ts=core.parse_when(value).isoformat())
            conferma = f"🕒 Nuovo orario: {core.format_when(core.get_match(match['id'])['when_ts'])}"
        elif field in ("posto", "luogo", "campo", "dove"):
            core.update_match(match["id"], place=value)
            conferma = f"📍 Nuovo posto: {esc(value)}"
        elif field in ("posti", "giocatori", "numero"):
            n = int("".join(c for c in value if c.isdigit()))
            if not 2 <= n <= 40:
                raise ParseError("Il numero di giocatori deve stare tra 2 e 40.")
            core.update_match(match["id"], max_players=n)
            conferma = f"👥 Ora si gioca in {n}"
        else:
            await notify(
                update, context,
                "Posso cambiare <code>ora</code>, <code>posto</code> o <code>posti</code>.",
            )
            return
    except (ParseError, ValueError) as e:
        await notify(update, context, str(e) or "Valore non valido.")
        return

    updated = core.get_match(match["id"])
    await refresh_card(context, updated)
    await update.effective_message.reply_text(
        f"{conferma}\n<code>{match['id']}</code> aggiornata.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_annulla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = await resolve_match(update, context, context.args)
    if not match:
        return
    if update.effective_user.id != match["organizer_id"]:
        await notify(
            update, context,
            f"Solo {esc(match['organizer_name'])} può annullare questa partita.",
        )
        return

    core.update_match(match["id"], status="cancelled")
    updated = core.get_match(match["id"])
    await refresh_card(context, updated)

    iscritti = core.get_signups(match["id"])
    menzioni = ", ".join(esc(s["label"]) for s in iscritti) or "nessuno"
    await update.effective_message.reply_text(
        f"❌ Partita <code>{match['id']}</code> annullata.\nAvvisati: {menzioni}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_elimina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancellazione definitiva. L'ID va scritto: è un'operazione senza ritorno."""
    if not context.args:
        await notify(
            update, context,
            "Dimmi quale partita eliminare: <code>/elimina CALC-1234</code>\n\n"
            "Spariscono partita, iscritti e scheda, senza lasciare traccia. "
            "Se vuoi solo fermarla avvisando chi si era iscritto, usa /annulla.",
        )
        return

    match = core.get_match(context.args[0])
    if not match or match["chat_id"] != update.effective_chat.id:
        await notify(
            update, context,
            f"Non trovo la partita {core.normalize_id(context.args[0])}."
        )
        return
    if update.effective_user.id != match["organizer_id"]:
        await notify(
            update, context,
            f"Solo {esc(match['organizer_name'])} può eliminare questa partita.",
        )
        return

    iscritti = core.get_signups(match["id"])
    await retire_card(
        context.bot, match, f"🗑 Partita <code>{match['id']}</code> eliminata."
    )
    core.delete_match(match["id"])

    menzioni = ", ".join(esc(s["label"]) for s in iscritti) or "nessuno"
    await update.effective_message.reply_text(
        f"🗑 Partita <code>{match['id']}</code> eliminata.\n"
        f"Erano iscritti: {menzioni}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_sconosciuto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Comando che non esiste. Invece di far finta di niente, suggerisce quello
    giusto — e lo fa in privato, così il gruppo non se ne accorge.
    """
    primo = (update.effective_message.text or "").split()
    if not primo:
        return
    nome, _, destinatario = primo[0].lstrip("/").partition("@")
    if destinatario and destinatario.lower() != (context.bot.username or "").lower():
        return  # comando rivolto a un altro bot del gruppo
    nome = nome.lower()

    vicini = difflib.get_close_matches(nome, COMANDI, n=1, cutoff=0.55)
    if vicini:
        testo = (
            f"Non conosco <code>/{esc(nome)}</code>. "
            f"Forse volevi <code>/{vicini[0]}</code>?"
        )
    else:
        testo = (
            f"Non conosco <code>/{esc(nome)}</code>. "
            f"L'elenco dei comandi è in /start."
        )
    await notify(update, context, testo)


# --------------------------------------------------------------------------
# Pulizia automatica
# --------------------------------------------------------------------------

async def purge_expired(bot):
    """Elimina le partite iniziate da più di PURGE_AFTER_HOURS ore."""
    for match in core.expired_matches(PURGE_AFTER_HOURS):
        try:
            await retire_card(
                bot, match, f"⚽ Partita <code>{match['id']}</code> archiviata."
            )
        except Exception:
            log.warning("Scheda di %s non rimossa", match["id"], exc_info=True)
        core.delete_match(match["id"])
        log.info("Partita %s eliminata automaticamente", match["id"])


async def purge_loop(app):
    """Gira in sottofondo per tutta la vita del bot."""
    while True:
        try:
            await purge_expired(app.bot)
        except Exception:
            log.exception("Errore nella pulizia automatica")
        await asyncio.sleep(PURGE_EVERY_SECONDS)


async def on_startup(app):
    app.bot_data["purge_task"] = asyncio.create_task(purge_loop(app))
    log.info(
        "Pulizia automatica attiva: le partite spariscono %d ore dopo l'inizio",
        PURGE_AFTER_HOURS,
    )


async def on_shutdown(app):
    task = app.bot_data.get("purge_task")
    if task:
        task.cancel()


async def cmd_amico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = await resolve_match(update, context, context.args)
    if not match:
        return
    await do_amico(update, context, match, update.effective_user)


async def cmd_togli_amico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = await resolve_match(update, context, context.args)
    if not match:
        return
    await do_togli_amico(update, context, match, update.effective_user)


# --------------------------------------------------------------------------
# Azioni condivise fra comandi e bottoni
# --------------------------------------------------------------------------

async def do_join(update, context, match, user, query=None):
    if match["status"] != "open":
        return await _reply(update, context, query, "Questa partita è stata annullata.", private=True)

    esito = core.join(match["id"], user.id, display_name(user))
    updated = core.get_match(match["id"])
    await refresh_card(context, updated)

    if esito == "gia_iscritto":
        return await _reply(update, context, query, "Sei già iscritto 😉", alert=False, private=True)
    if esito == "riserva":
        return await _reply(
            update, context, query,
            "Sei in lista riserve — entri se qualcuno si ritira.",
        )
    return await _reply(update, context, query, "Dentro! ⚽")


async def do_leave(update, context, match, user, query=None):
    rimosso, promossi, ospiti_tolti = core.leave(match["id"], user.id)
    if not rimosso:
        return await _reply(update, context, query, "Non risultavi iscritto.", alert=False, private=True)

    updated = core.get_match(match["id"])
    await refresh_card(context, updated)

    con_amici = ""
    if ospiti_tolti:
        con_amici = f" con {ospiti_tolti} amico" if ospiti_tolti == 1 else f" con {ospiti_tolti} amici"

    if promossi:
        entrano = ", ".join(esc(p["label"]) for p in promossi)
        verbo = "entrano" if len(promossi) > 1 else "entra"
        await context.bot.send_message(
            chat_id=match["chat_id"],
            text=f"🔄 {esc(display_name(user))} si ritira{con_amici}, {verbo} {entrano}.",
            parse_mode=ParseMode.HTML,
        )

    coda = f" Ho tolto anche{con_amici.replace(' con', '')}." if ospiti_tolti else ""
    return await _reply(update, context, query, f"Ti ho tolto dalla lista.{coda}")


async def do_teams(update, context, match, user, query=None):
    if user.id != match["organizer_id"]:
        return await _reply(
            update, context, query,
            f"Solo {match['organizer_name']} può fare le squadre.",
            private=True,
        )

    signups = core.get_signups(match["id"])
    starters, _ = core.split_roster(match, signups)
    if len(starters) < 2:
        return await _reply(update, context, query, "Servono almeno 2 giocatori.", private=True)

    team_a, team_b = core.make_teams([s["label"] for s in starters])
    core.update_match(match["id"], teams="|".join(team_a) + "||" + "|".join(team_b))

    updated = core.get_match(match["id"])
    await refresh_card(context, updated)

    if len(starters) < match["max_players"]:
        await context.bot.send_message(
            chat_id=match["chat_id"],
            text=f"🎽 Squadre fatte in {len(starters)} (ne mancavano "
                 f"{match['max_players'] - len(starters)}).",
        )
    return await _reply(update, context, query, "Squadre fatte 🎽")


async def do_amico(update, context, match, user, query=None):
    """Aggiunge un ospite a nome di chi preme."""
    if match["status"] != "open":
        return await _reply(update, context, query,
                            "Questa partita è stata annullata.", private=True)

    iscritti = core.get_signups(match["id"])
    if not any(s["user_id"] == user.id and s["guest_of"] is None for s in iscritti):
        return await _reply(
            update, context, query,
            "Prima iscriviti tu, poi puoi portare chi vuoi.", private=True,
        )

    esito = core.add_guest(match["id"], user.id, display_name(user))
    await refresh_card(context, core.get_match(match["id"]))
    if esito == "riserva":
        return await _reply(update, context, query,
                            "Il tuo amico è in lista riserve.")
    return await _reply(update, context, query, "Amico aggiunto ⚽")


async def do_togli_amico(update, context, match, user, query=None):
    """
    Toglie un ospite. Chi l'ha portato può togliere i suoi; l'organizzatore
    della partita e gli amministratori del gruppo possono togliere chiunque.
    Con un solo candidato lo rimuove subito, con più di uno chiede quale.
    """
    ospiti = [s for s in core.get_signups(match["id"]) if s["guest_of"] is not None]
    if not ospiti:
        return await _reply(update, context, query,
                            "Non c'è nessun amico in lista.", private=True)

    tutti = user.id == match["organizer_id"] or await is_group_admin(
        context, match["chat_id"], user.id
    )
    candidati = ospiti if tutti else [s for s in ospiti if s["guest_of"] == user.id]

    if not candidati:
        return await _reply(update, context, query,
                            "Puoi togliere solo gli amici che hai portato tu.",
                            private=True)
    if len(candidati) == 1:
        return await _rimuovi_ospite(update, context, match, candidati[0], query)

    tastiera = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"❌ {s['label']}", callback_data=f"gdel:{match['id']}:{s['id']}"
        )]
        for s in candidati
    ])
    if query:
        await query.answer()
    await notify(
        update, context, "Chi tolgo?",
        keyboard=tastiera, drop_command=query is None,
    )


async def _rimuovi_ospite(update, context, match, ospite, query=None):
    etichetta = ospite["label"]
    _, promosso = core.remove_guest(match["id"], ospite["id"])
    await refresh_card(context, core.get_match(match["id"]))

    if promosso:
        # Non uso l'etichetta di chi esce: gli amici si rinumerano subito dopo la
        # rimozione, e «esce Amico 1, entra Amico 2» sarebbe solo confondente.
        await context.bot.send_message(
            chat_id=match["chat_id"],
            text=f"🔄 Esce un amico di {esc(ospite['name'])}, "
                 f"entra {esc(promosso['label'])}.",
            parse_mode=ParseMode.HTML,
        )
    return await _reply(update, context, query, f"Tolto: {etichetta}")


async def _reply(update, context, query, text, alert=True, private=False):
    """
    Popup se l'azione arriva da un bottone, messaggio se arriva da un comando.

    Con private=True l'avviso da comando passa da notify(): va in chat privata
    o si autocancella, invece di restare a ingombrare il gruppo. I popup dei
    bottoni sono già visibili solo a chi ha premuto.
    """
    if query:
        await query.answer(text, show_alert=alert)
    elif private:
        await notify(update, context, text)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, _, payload = query.data.partition(":")

    if action == "gdel":
        await on_guest_delete(update, context, payload, query)
        return

    match_id = payload
    match = core.get_match(match_id)

    if not match:
        await query.answer("Partita non trovata.", show_alert=True)
        return

    user = query.from_user
    if action == "join":
        await do_join(update, context, match, user, query)
    elif action == "leave":
        await do_leave(update, context, match, user, query)
    elif action == "teams":
        await do_teams(update, context, match, user, query)
    elif action == "guest":
        await do_amico(update, context, match, user, query)
    elif action == "unguest":
        await do_togli_amico(update, context, match, user, query)
    else:
        await query.answer()


async def on_guest_delete(update, context, payload, query):
    """❌ premuto nell'elenco «Chi tolgo?»."""
    match_id, _, signup_id = payload.partition(":")
    match = core.get_match(match_id)
    if not match:
        await query.answer("Partita non trovata.", show_alert=True)
        return

    user = query.from_user
    ospite = next(
        (s for s in core.get_signups(match["id"])
         if str(s["id"]) == signup_id and s["guest_of"] is not None),
        None,
    )
    if not ospite:
        await query.answer("Quell'amico non c'è più.", show_alert=True)
        return

    if not (user.id == ospite["guest_of"]
            or user.id == match["organizer_id"]
            or await is_group_admin(context, match["chat_id"], user.id)):
        await query.answer("Puoi togliere solo gli amici che hai portato tu.",
                           show_alert=True)
        return

    etichetta = ospite["label"]
    await _rimuovi_ospite(update, context, match, ospite, query)
    try:
        await query.edit_message_text(f"Tolto: {esc(etichetta)}",
                                      parse_mode=ParseMode.HTML)
    except BadRequest:
        pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Errore non gestito", exc_info=context.error)


# --------------------------------------------------------------------------
# Avvio
# --------------------------------------------------------------------------

def main():
    if not TOKEN:
        raise SystemExit("Manca la variabile d'ambiente TELEGRAM_TOKEN")

    core.init_db()
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler(["start", "help", "aiuto"], cmd_start))
    app.add_handler(CommandHandler(["nuova_partita", "nuova"], cmd_nuova))
    app.add_handler(CommandHandler(["iscrivimi", "ci_sono"], cmd_iscrivimi))
    app.add_handler(CommandHandler(["esco", "cancella_iscrizione"], cmd_esco))
    app.add_handler(CommandHandler(["lista", "chi_gioca"], cmd_lista))
    app.add_handler(CommandHandler(["partite", "partite_aperte"], cmd_partite))
    app.add_handler(CommandHandler("squadre", cmd_squadre))
    app.add_handler(CommandHandler("modifica", cmd_modifica))
    app.add_handler(CommandHandler(["annulla", "cancella_partita"], cmd_annulla))
    app.add_handler(CommandHandler(["elimina", "elimina_partita"], cmd_elimina))
    app.add_handler(CommandHandler(["amico", "porto_un_amico"], cmd_amico))
    app.add_handler(CommandHandler(["togli_amico", "tolgo_un_amico"], cmd_togli_amico))
    app.add_handler(CallbackQueryHandler(on_button))
    # Per ultimo: raccoglie tutti i comandi che nessun handler sopra ha preso.
    app.add_handler(MessageHandler(filters.COMMAND, cmd_sconosciuto))
    app.add_error_handler(on_error)

    if WEBHOOK_URL:
        log.info("Avvio in modalità webhook su %s", WEBHOOK_URL)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
            drop_pending_updates=True,
        )
    else:
        log.info("Avvio in modalità polling (sviluppo locale)")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
