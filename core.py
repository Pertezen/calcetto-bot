"""
Logica e persistenza del bot calcetto.
Nessuna dipendenza da Telegram: tutto qui dentro è testabile da solo.
"""

import os
import random
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Rome"))
DB_PATH = os.environ.get("DB_PATH", "calcetto.db")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


SIGNUPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS signups (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id  TEXT NOT NULL,
    user_id   INTEGER NOT NULL,   -- chi occupa il posto; per un ospite, chi l'ha portato
    name      TEXT NOT NULL,
    guest_of  INTEGER,            -- NULL = giocatore vero, altrimenti chi l'ha aggiunto
    joined_at TEXT NOT NULL
);
"""


def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS matches (
            id             TEXT PRIMARY KEY,
            chat_id        INTEGER NOT NULL,
            organizer_id   INTEGER NOT NULL,
            organizer_name TEXT NOT NULL,
            when_ts        TEXT NOT NULL,
            place          TEXT NOT NULL,
            max_players    INTEGER NOT NULL,
            status         TEXT NOT NULL DEFAULT 'open',
            card_msg_id    INTEGER,
            teams          TEXT,
            created_at     TEXT NOT NULL
        );
        """ + SIGNUPS_SCHEMA)

        _migra_ospiti(conn)

        conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_signups_match ON signups(match_id);
        CREATE INDEX IF NOT EXISTS idx_matches_chat  ON matches(chat_id, status);
        -- un giocatore vero si iscrive una volta sola; gli ospiti no, sono più d'uno
        CREATE UNIQUE INDEX IF NOT EXISTS idx_signups_player
            ON signups(match_id, user_id) WHERE guest_of IS NULL;
        """)


def _migra_ospiti(conn):
    """
    Porta la vecchia tabella signups (una riga per giocatore, nessun ospite)
    al nuovo schema, conservando le iscrizioni già raccolte.
    """
    colonne = [r[1] for r in conn.execute("PRAGMA table_info(signups)")]
    if not colonne or "guest_of" in colonne:
        return
    conn.executescript("""
        ALTER TABLE signups RENAME TO signups_old;
        """ + SIGNUPS_SCHEMA + """
        INSERT INTO signups (match_id, user_id, name, guest_of, joined_at)
            SELECT match_id, user_id, name, NULL, joined_at FROM signups_old;
        DROP TABLE signups_old;
    """)


# --------------------------------------------------------------------------
# Parsing data/ora
# --------------------------------------------------------------------------

class ParseError(ValueError):
    """Input dell'utente non valido, con messaggio già pronto da mostrare."""


_TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})")
_DATE_RE = re.compile(r"(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?")

_WEEKDAYS = {
    "lun": 0, "lunedi": 0, "lunedì": 0,
    "mar": 1, "martedi": 1, "martedì": 1,
    "mer": 2, "mercoledi": 2, "mercoledì": 2,
    "gio": 3, "giovedi": 3, "giovedì": 3,
    "ven": 4, "venerdi": 4, "venerdì": 4,
    "sab": 5, "sabato": 5,
    "dom": 6, "domenica": 6,
}


def parse_when(text: str, now: datetime | None = None) -> datetime:
    """
    Accetta: '30/08 19:00', '30/08/2026 21.30', 'oggi 19:00',
             'domani 20:30', 'venerdì 19:00'.
    Ritorna un datetime timezone-aware nel futuro.
    """
    now = now or datetime.now(TZ)
    raw = text.strip().lower()

    time_match = _TIME_RE.search(raw)
    if not time_match:
        raise ParseError(
            "Non ho capito l'orario. Scrivilo come <code>19:00</code>."
        )
    hour, minute = int(time_match.group(1)), int(time_match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ParseError("Orario non valido.")

    # Tolgo l'orario dalla stringa, così non confonde il parser della data
    rest = (raw[: time_match.start()] + " " + raw[time_match.end():]).strip()

    if "domani" in rest:
        day = now.date() + timedelta(days=1)
    elif "dopodomani" in rest:
        day = now.date() + timedelta(days=2)
    elif "oggi" in rest or rest == "":
        day = now.date()
    elif (weekday := _match_weekday(rest)) is not None:
        delta = (weekday - now.weekday()) % 7
        if delta == 0:
            delta = 7  # "venerdì" detto di venerdì = venerdì prossimo
        day = now.date() + timedelta(days=delta)
    else:
        date_match = _DATE_RE.search(rest)
        if not date_match:
            raise ParseError(
                "Non ho capito la data. Prova con <code>30/08</code>, "
                "<code>domani</code> o <code>venerdì</code>."
            )
        d, m = int(date_match.group(1)), int(date_match.group(2))
        y = date_match.group(3)
        if y:
            y = int(y)
            year = y if y > 100 else 2000 + y
        else:
            year = now.year
        try:
            day = datetime(year, m, d).date()
        except ValueError:
            raise ParseError("Quella data non esiste.")
        # Senza anno esplicito, una data passata di poco (tipico a cavallo
        # di capodanno) vale per l'anno prossimo. Se lo scarto è grande,
        # è quasi certamente un errore di battitura: meglio dirlo.
        if not date_match.group(3) and day < now.date():
            shifted = day.replace(year=year + 1)
            if (shifted - now.date()).days > 90:
                raise ParseError(
                    "Quella data è già passata. Se intendevi l'anno prossimo, "
                    "scrivilo per esteso (es. <code>20/08/2027</code>)."
                )
            day = shifted

    when = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)

    if when < now - timedelta(minutes=5):
        raise ParseError("Quella data è già passata.")
    return when


def _match_weekday(text: str) -> int | None:
    for word in re.findall(r"[a-zàèéìòù]+", text):
        if word in _WEEKDAYS:
            return _WEEKDAYS[word]
    return None


def format_when(iso: str) -> str:
    """ISO -> 'ven 30/08 alle 19:00'."""
    dt = datetime.fromisoformat(iso)
    giorni = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
    anno = f"/{dt.year}" if dt.year != datetime.now(TZ).year else ""
    return f"{giorni[dt.weekday()]} {dt.day:02d}/{dt.month:02d}{anno} alle {dt:%H:%M}"


# --------------------------------------------------------------------------
# Partite
# --------------------------------------------------------------------------

def new_match_id() -> str:
    with get_db() as conn:
        for _ in range(50):
            candidate = f"CALC-{random.randint(1000, 9999)}"
            exists = conn.execute(
                "SELECT 1 FROM matches WHERE id = ?", (candidate,)
            ).fetchone()
            if not exists:
                return candidate
    raise RuntimeError("Non riesco a generare un ID libero")


def create_match(chat_id, organizer_id, organizer_name, when, place, max_players):
    if max_players < 2:
        raise ParseError("Servono almeno 2 giocatori.")
    if max_players > 40:
        raise ParseError("Massimo 40 giocatori.")
    if not place.strip():
        raise ParseError("Manca il posto.")

    match_id = new_match_id()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO matches
               (id, chat_id, organizer_id, organizer_name, when_ts, place,
                max_players, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (match_id, chat_id, organizer_id, organizer_name, when.isoformat(),
             place.strip(), max_players, datetime.now(TZ).isoformat()),
        )
    return match_id


def get_match(match_id: str):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE id = ?", (normalize_id(match_id),)
        ).fetchone()


def normalize_id(raw: str) -> str:
    """'2847', 'calc-2847', 'CALC2847' -> 'CALC-2847'."""
    raw = (raw or "").strip().upper().replace(" ", "")
    digits = re.sub(r"\D", "", raw)
    return f"CALC-{digits}" if digits else raw


def set_card_msg(match_id: str, msg_id: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE matches SET card_msg_id = ? WHERE id = ?", (msg_id, match_id)
        )


def update_match(match_id: str, **fields):
    allowed = {"when_ts", "place", "max_players", "status", "teams"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k} = ?" for k in updates)
    with get_db() as conn:
        conn.execute(
            f"UPDATE matches SET {sets} WHERE id = ?",
            (*updates.values(), match_id),
        )


def open_matches(chat_id: int):
    """Partite aperte e non ancora giocate, dalla più vicina."""
    cutoff = (datetime.now(TZ) - timedelta(hours=3)).isoformat()
    with get_db() as conn:
        return conn.execute(
            """SELECT * FROM matches
               WHERE chat_id = ? AND status = 'open' AND when_ts > ?
               ORDER BY when_ts""",
            (chat_id, cutoff),
        ).fetchall()


def delete_match(match_id: str) -> bool:
    """
    Cancella davvero la partita e tutte le sue iscrizioni.
    Ritorna True se c'era qualcosa da cancellare.
    """
    match_id = normalize_id(match_id)
    with get_db() as conn:
        conn.execute("DELETE FROM signups WHERE match_id = ?", (match_id,))
        cur = conn.execute("DELETE FROM matches WHERE id = ?", (match_id,))
        return cur.rowcount > 0


def expired_matches(hours: int = 3):
    """
    Partite iniziate da più di `hours` ore, annullate comprese.
    Il confronto è su datetime veri e non su stringhe: a cavallo del cambio
    d'ora legale il testo ISO non è ordinabile in modo affidabile.
    """
    limit = datetime.now(TZ) - timedelta(hours=hours)
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM matches").fetchall()
    return [r for r in rows if datetime.fromisoformat(r["when_ts"]) <= limit]


# --------------------------------------------------------------------------
# Iscrizioni
# --------------------------------------------------------------------------

def get_signups(match_id: str):
    """
    Iscritti in ordine di arrivo, come dizionari con in più 'label': il nome da
    mostrare. Per un ospite è «Amico 2 di Lorenzo», e la numerazione si ricalcola
    a ogni lettura, così dopo una rimozione non restano buchi.
    """
    with get_db() as conn:
        righe = conn.execute(
            "SELECT * FROM signups WHERE match_id = ? ORDER BY joined_at, id",
            (match_id,),
        ).fetchall()

    iscritti, contatori = [], {}
    for r in righe:
        d = dict(r)
        if d["guest_of"] is None:
            d["label"] = d["name"]
        else:
            n = contatori.get(d["guest_of"], 0) + 1
            contatori[d["guest_of"]] = n
            d["label"] = f"Amico {n} di {d['name']}"
        iscritti.append(d)
    return iscritti


def guests_of(match_id: str, user_id: int):
    """Gli ospiti portati da una certa persona."""
    return [s for s in get_signups(match_id) if s["guest_of"] == user_id]


def add_guest(match_id: str, user_id: int, adder_name: str) -> str:
    """Aggiunge un ospite. Ritorna 'in' o 'riserva'."""
    prima = get_signups(match_id)
    match = get_match(match_id)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO signups (match_id, user_id, name, guest_of, joined_at)
               VALUES (?, ?, ?, ?, ?)""",
            (match_id, user_id, adder_name, user_id, datetime.now(TZ).isoformat()),
        )
    return "in" if len(prima) < match["max_players"] else "riserva"


def _rimuovi_riga(match, prima, row_id):
    """
    Cancella una riga di iscrizione e ritorna la riserva che entra al suo posto
    (None se non ce n'erano, o se il posto liberato era già in panchina).
    """
    era_titolare = any(s["id"] == row_id for s in prima[: match["max_players"]])
    c_erano_riserve = len(prima) > match["max_players"]

    with get_db() as conn:
        conn.execute("DELETE FROM signups WHERE id = ?", (row_id,))

    if era_titolare and c_erano_riserve:
        dopo = get_signups(match["id"])
        return dopo[match["max_players"] - 1]
    return None


def remove_guest(match_id: str, signup_id: int):
    """
    Toglie un ospite. Ritorna (ospite_rimosso | None, promosso | None):
    None come primo elemento significa che quella riga non esiste più.
    """
    match = get_match(match_id)
    prima = get_signups(match_id)
    ospite = next(
        (s for s in prima
         if s["id"] == int(signup_id) and s["guest_of"] is not None),
        None,
    )
    if not ospite:
        return None, None
    return ospite, _rimuovi_riga(match, prima, ospite["id"])


def split_roster(match, signups):
    """Divide gli iscritti in titolari e riserve secondo l'ordine di arrivo."""
    limit = match["max_players"]
    return list(signups[:limit]), list(signups[limit:])


def join(match_id: str, user_id: int, name: str) -> str:
    """Ritorna 'in', 'riserva' o 'gia_iscritto'."""
    match = get_match(match_id)
    signups = get_signups(match_id)

    if any(s["user_id"] == user_id and s["guest_of"] is None for s in signups):
        return "gia_iscritto"

    with get_db() as conn:
        conn.execute(
            "INSERT INTO signups (match_id, user_id, name, joined_at) VALUES (?, ?, ?, ?)",
            (match_id, user_id, name, datetime.now(TZ).isoformat()),
        )
    return "in" if len(signups) < match["max_players"] else "riserva"


def leave(match_id: str, user_id: int):
    """
    Ritira una persona e con lei gli amici che aveva portato: erano suoi ospiti,
    senza di lei quei posti tornano liberi per gli altri.

    Ritorna (rimosso: bool, promossi: list, ospiti_tolti: int).
    """
    match = get_match(match_id)
    prima = get_signups(match_id)

    miei = [s for s in prima if s["user_id"] == user_id]
    if not any(s["guest_of"] is None for s in miei):
        return False, [], 0

    ospiti_tolti = len(miei) - 1
    promossi = []
    # dall'ultimo arrivato al primo: così gli indici delle riserve restano sani
    for riga in sorted(miei, key=lambda s: s["joined_at"], reverse=True):
        promosso = _rimuovi_riga(match, get_signups(match_id), riga["id"])
        if promosso:
            promossi.append(promosso)

    return True, promossi, ospiti_tolti


# --------------------------------------------------------------------------
# Squadre
# --------------------------------------------------------------------------

def make_teams(names: list[str], seed=None) -> tuple[list[str], list[str]]:
    """Mescola e divide in due squadre il più pari possibile."""
    pool = list(names)
    rng = random.Random(seed)
    rng.shuffle(pool)
    half = (len(pool) + 1) // 2
    return pool[:half], pool[half:]
