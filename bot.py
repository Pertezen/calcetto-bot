"""
Bot Telegram per organizzare i calcetti.
Avvio: python bot.py
"""

import asyncio
import html
import logging
import os

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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

HELP = """⚽ <b>Bot Calcetto</b>

<b>Creare una partita</b>
<code>/nuova_partita venerdì 19:00 | Campetto Centro | 10</code>
(data | posto | numero giocatori)

<b>Comandi</b>
/partite — le partite aperte
/lista <code>ID</code> — chi gioca
/iscrivimi <code>ID</code> — ti iscrivi
/esco <code>ID</code> — ti ritiri
/squadre <code>ID</code> — divide le squadre (solo organizzatore)
/modifica <code>ID ora|posto|posti valore</code>
/annulla <code>ID</code> — annulla la partita, la scheda resta
/elimina <code>ID</code> — la cancella del tutto (solo organizzatore)

Le partite spariscono da sole 3 ore dopo l'orario di inizio.

Il modo più veloce resta comunque premere i bottoni sotto alla partita.
Se c'è una sola partita aperta, l'ID puoi anche non scriverlo."""


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
            lines.append(f"{i}. {esc(s['name'])}")
    else:
        lines.append("<i>Ancora nessuno. Rompi il ghiaccio.</i>")

    if reserves:
        lines.append("")
        lines.append("<b>Riserve</b>")
        for i, s in enumerate(reserves, 1):
            lines.append(f"{i}. {esc(s['name'])}")

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

async def resolve_match(update: Update, context, args):
    """
    Trova la partita dall'ID passato come argomento.
    Se non c'è ID e nel gruppo c'è una sola partita aperta, usa quella.
    """
    chat_id = update.effective_chat.id
    if args:
        match = core.get_match(args[0])
        if not match or match["chat_id"] != chat_id:
            await update.effective_message.reply_text(
                f"Non trovo la partita {core.normalize_id(args[0])}."
            )
            return None
        return match

    aperte = core.open_matches(chat_id)
    if len(aperte) == 1:
        return aperte[0]
    if not aperte:
        await update.effective_message.reply_text(
            "Non c'è nessuna partita aperta. Creane una con /nuova_partita."
        )
    else:
        await update.effective_message.reply_text(
            "Ci sono più partite aperte, dimmi quale (es. <code>/lista CALC-1234</code>).\n"
            "Vedi la lista con /partite.",
            parse_mode=ParseMode.HTML,
        )
    return None


def display_name(user) -> str:
    return user.full_name or user.username or f"Giocatore {user.id}"


# --------------------------------------------------------------------------
# Comandi
# --------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_nuova(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]

    if len(parts) != 3:
        await update.effective_message.reply_text(
            "Mi servono tre cose separate da <code>|</code>:\n\n"
            "<code>/nuova_partita venerdì 19:00 | Campetto Centro | 10</code>\n\n"
            "Per la data va bene anche <code>domani 21:00</code> o <code>30/08 19:00</code>.",
            parse_mode=ParseMode.HTML,
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
        await update.effective_message.reply_text(str(e), parse_mode=ParseMode.HTML)
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
        await update.effective_message.reply_text(
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
        await update.effective_message.reply_text(
            "Come si usa:\n"
            "<code>/modifica CALC-1234 ora venerdì 20:30</code>\n"
            "<code>/modifica CALC-1234 posto Campetto Nord</code>\n"
            "<code>/modifica CALC-1234 posti 12</code>",
            parse_mode=ParseMode.HTML,
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
        await update.effective_message.reply_text("Quella partita non è di questo gruppo.")
        return
    if update.effective_user.id != match["organizer_id"]:
        await update.effective_message.reply_text(
            f"Solo {esc(match['organizer_name'])} può modificare questa partita.",
            parse_mode=ParseMode.HTML,
        )
        return
    if not value:
        await update.effective_message.reply_text("Manca il nuovo valore.")
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
            await update.effective_message.reply_text(
                "Posso cambiare <code>ora</code>, <code>posto</code> o <code>posti</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
    except (ParseError, ValueError) as e:
        await update.effective_message.reply_text(
            str(e) or "Valore non valido.", parse_mode=ParseMode.HTML
        )
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
        await update.effective_message.reply_text(
            f"Solo {esc(match['organizer_name'])} può annullare questa partita.",
            parse_mode=ParseMode.HTML,
        )
        return

    core.update_match(match["id"], status="cancelled")
    updated = core.get_match(match["id"])
    await refresh_card(context, updated)

    iscritti = core.get_signups(match["id"])
    menzioni = ", ".join(esc(s["name"]) for s in iscritti) or "nessuno"
    await update.effective_message.reply_text(
        f"❌ Partita <code>{match['id']}</code> annullata.\nAvvisati: {menzioni}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_elimina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancellazione definitiva. L'ID va scritto: è un'operazione senza ritorno."""
    if not context.args:
        await update.effective_message.reply_text(
            "Dimmi quale partita eliminare: <code>/elimina CALC-1234</code>\n\n"
            "Spariscono partita, iscritti e scheda, senza lasciare traccia. "
            "Se vuoi solo fermarla avvisando chi si era iscritto, usa /annulla.",
            parse_mode=ParseMode.HTML,
        )
        return

    match = core.get_match(context.args[0])
    if not match or match["chat_id"] != update.effective_chat.id:
        await update.effective_message.reply_text(
            f"Non trovo la partita {core.normalize_id(context.args[0])}."
        )
        return
    if update.effective_user.id != match["organizer_id"]:
        await update.effective_message.reply_text(
            f"Solo {esc(match['organizer_name'])} può eliminare questa partita.",
            parse_mode=ParseMode.HTML,
        )
        return

    iscritti = core.get_signups(match["id"])
    await retire_card(
        context.bot, match, f"🗑 Partita <code>{match['id']}</code> eliminata."
    )
    core.delete_match(match["id"])

    menzioni = ", ".join(esc(s["name"]) for s in iscritti) or "nessuno"
    await update.effective_message.reply_text(
        f"🗑 Partita <code>{match['id']}</code> eliminata.\n"
        f"Erano iscritti: {menzioni}",
        parse_mode=ParseMode.HTML,
    )


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


# --------------------------------------------------------------------------
# Azioni condivise fra comandi e bottoni
# --------------------------------------------------------------------------

async def do_join(update, context, match, user, query=None):
    if match["status"] != "open":
        return await _reply(update, query, "Questa partita è stata annullata.")

    esito = core.join(match["id"], user.id, display_name(user))
    updated = core.get_match(match["id"])
    await refresh_card(context, updated)

    if esito == "gia_iscritto":
        return await _reply(update, query, "Sei già iscritto 😉", alert=False)
    if esito == "riserva":
        return await _reply(
            update, query,
            "Sei in lista riserve — entri se qualcuno si ritira.",
        )
    return await _reply(update, query, "Dentro! ⚽")


async def do_leave(update, context, match, user, query=None):
    rimosso, promosso = core.leave(match["id"], user.id)
    if not rimosso:
        return await _reply(update, query, "Non risultavi iscritto.", alert=False)

    updated = core.get_match(match["id"])
    await refresh_card(context, updated)

    if promosso:
        await context.bot.send_message(
            chat_id=match["chat_id"],
            text=f"🔄 {esc(display_name(user))} si ritira, entra {esc(promosso['name'])}.",
            parse_mode=ParseMode.HTML,
        )
    return await _reply(update, query, "Ti ho tolto dalla lista.")


async def do_teams(update, context, match, user, query=None):
    if user.id != match["organizer_id"]:
        return await _reply(
            update, query,
            f"Solo {match['organizer_name']} può fare le squadre.",
        )

    signups = core.get_signups(match["id"])
    starters, _ = core.split_roster(match, signups)
    if len(starters) < 2:
        return await _reply(update, query, "Servono almeno 2 giocatori.")

    team_a, team_b = core.make_teams([s["name"] for s in starters])
    core.update_match(match["id"], teams="|".join(team_a) + "||" + "|".join(team_b))

    updated = core.get_match(match["id"])
    await refresh_card(context, updated)

    if len(starters) < match["max_players"]:
        await context.bot.send_message(
            chat_id=match["chat_id"],
            text=f"🎽 Squadre fatte in {len(starters)} (ne mancavano "
                 f"{match['max_players'] - len(starters)}).",
        )
    return await _reply(update, query, "Squadre fatte 🎽")


async def _reply(update, query, text, alert=True):
    """Risponde via popup se arriva da un bottone, via messaggio se da comando."""
    if query:
        await query.answer(text, show_alert=alert)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, _, match_id = query.data.partition(":")
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
    else:
        await query.answer()


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
    app.add_handler(CallbackQueryHandler(on_button))
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
