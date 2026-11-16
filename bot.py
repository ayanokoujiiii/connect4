#!/usr/bin/env python3
"""
دوز — Telegram bot, Web App mode (stdlib only, no dependencies)
================================================================================
Why a Web App, not inline-keyboard numbers?
  Telegram inline-keyboard buttons have a fixed size the bot cannot change,
  and changing the Telegram font size does not affect them. That is exactly
  why Behzad kept mis-tapping 1..7 in bazihaabot.

  This bot therefore plays through a full-screen Web App
  (webapp/index.html): whole columns are tap targets, numbers are huge
  and sit BELOW the board, and one tap drops the disc straight in —
  no select-then-confirm step. There is NO undo button on purpose:
  Mehdi removed it — it breaks the game. Colours are chosen per
  player and rock-paper-scissors decides who starts.

  REMOTE play (v4 — like the original @bazihaabot, "بازیهای تلگرامی"):
  the two players are on TWO phones / two accounts, never pass-and-play
  on one phone. ALL of the UI lives inside the Web App (v4, Mehdi's
  order 2026-11-16): /start only shows one button, "ورود به بازی",
  and the Web App's home screen holds every choice — play vs the bot,
  play with a friend (shareable invite link), play a random stranger,
  colours, rock-paper-scissors, rematch. Telegram carries no game
  buttons at all: the game message is just an entry card with the
  Web App button, kept in sync in BOTH players' chats. After both
  join, the game is in the "rps" phase: each player throws on their
  OWN phone and the bot decides who starts.

How state sync works without a server:
  * The Web App is a static page (GitHub Pages, HTTPS). No backend.
  * The current board is encoded in the Web App button URL
    (?board=000...&turn=1&p1=..&p2=..&s1=..&s2=..&g=..).
  * A move is sent back with Telegram.WebApp.sendData() as a normal
    bot message (web_app_data). The bot validates it, updates the game,
    and edits the group message with a fresh URL carrying the new board.
  * The other player just re-opens the game from the group message.

Token rule (binding, AGENTS.md): the bot token is read ONLY from the
environment variable CONNECT4_BOT_TOKEN, transiently. It is NEVER written
to any file, log, memory note or report, and never echoed back.

Run:
  CONNECT4_BOT_TOKEN='<token>' \
  CONNECT4_WEBAPP_URL='https://ayanokoujiiii.github.io/connect4/' \
  python3 bot.py

Optional env:
  CONNECT4_BOT_USERNAME default Cheshmha_Bbot (used for invite links)
  CONNECT4_WEBAPP_URL   default https://ayanokoujiiii.github.io/connect4/
  CONNECT4_STATE_FILE   default state.json (same dir; scores + games only)
  CONNECT4_TURN_TIMEOUT seconds per turn, default 180 (3 min, like the
                        original bot's delay-win). 0 disables timeout.
"""
import json
import os
import random
import re
import secrets
import time
import urllib.parse
import urllib.request

ROWS, COLS = 6, 7
EMPTY, BLUE, RED = 0, 1, 2
CIRCLE = {0: "⚪", 1: "🔵", 2: "🔴"}
FA = "۰۱۲۳۴۵۶۷۸۹"

API = "https://api.telegram.org/bot"
TOKEN = os.environ.get("CONNECT4_BOT_TOKEN", "")
WEBAPP_URL = os.environ.get(
    "CONNECT4_WEBAPP_URL", "https://ayanokoujiiii.github.io/connect4/"
).rstrip("/") + "/"
STATE_FILE = os.environ.get(
    "CONNECT4_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
)
BOT_USERNAME = os.environ.get("CONNECT4_BOT_USERNAME", "Cheshmha_Bbot")
TURN_TIMEOUT = int(os.environ.get("CONNECT4_TURN_TIMEOUT", "180"))


def fa(n):
    return str(n).translate(str.maketrans("0123456789", FA))


# ---------------------------------------------------------------- pure logic
def new_board():
    return [[0] * COLS for _ in range(ROWS)]


def board_str(board):
    return "".join(str(v) for row in board for v in row)


def lowest_empty(board, col):
    for r in range(ROWS - 1, -1, -1):
        if board[r][col] == 0:
            return r
    return -1


def check_win(board, r, c, p):
    """Return list of 4 winning cells, or None."""
    for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
        cells = [(r, c)]
        for s in (1, -1):
            rr, cc = r + dr * s, c + dc * s
            while 0 <= rr < ROWS and 0 <= cc < COLS and board[rr][cc] == p:
                cells.append((rr, cc))
                rr += dr * s
                cc += dc * s
        if len(cells) >= 4:
            return cells[:4]
    return None


def is_full(board):
    return all(v != 0 for row in board for v in row)


def board_text(board):
    return "\n".join("".join(CIRCLE[v] for v in row) for row in board)


def ai_move(board, me=RED, opp=BLUE):
    """Win if possible, block a loss, else prefer centre columns."""
    for col in range(COLS):
        r = lowest_empty(board, col)
        if r == -1:
            continue
        board[r][col] = me
        win = check_win(board, r, col, me) is not None
        board[r][col] = 0
        if win:
            return col
    for col in range(COLS):
        r = lowest_empty(board, col)
        if r == -1:
            continue
        board[r][col] = opp
        win = check_win(board, r, col, opp) is not None
        board[r][col] = 0
        if win:
            return col
    for col in (3, 2, 4, 1, 5, 0, 6):
        if lowest_empty(board, col) != -1:
            return col
    return -1


# ---------------------------------------------------------------- state
def new_game(chat_id, chat_title="", game_id=None):
    return {
        "id": game_id or secrets.token_hex(4),
        "chat_id": chat_id,
        "chat_title": chat_title,
        "board": new_board(),
        "players": {},          # "1": {"id":..,"name":..}, "2": {...}
        "turn": 1,
        "status": "joining",    # joining | playing | finished
        "winner": 0,
        "win_cells": [],
        "last_move": None,      # [row, col, player]
        "history": [],          # [[row, col, player], ...]
        "score": {"1": 0, "2": 0},
        "vs_bot": False,
        "rps": {},
        "mode": "friend",
        "messages": [],      # [{"chat_id":..,"message_id":..}] — synced in both chats
        "message_id": None,
        "last_move_at": time.time(),
        "starter": 1,
    }


GAMES = {}   # game_id -> game
BY_CHAT = {}  # chat_id -> game_id
SCORES = {}  # str(chat_id) -> {"1": n, "2": n}  (survives new games)
WAITING = []  # [{"id":..,"name":..}] — /stranger queue (one waiter at a time)
OFFSET = 0  # getUpdates offset, persisted so a restart never replays old updates


def player_slot(game, user_id):
    """'1' / '2' for a player in this game, else None."""
    for n in ("1", "2"):
        if game["players"].get(n, {}).get("id") == user_id:
            return n
    return None


def join_game(game, user_id, name):
    """Add a player to a game. Returns slot ('1'/'2') or None if full/duplicate.

    When the second player joins, the game moves to the 'rps' phase —
    it does NOT start playing yet: each player must first throw
    rock/paper/scissors on their own phone, and the bot picks the starter.
    """
    if player_slot(game, user_id):
        return player_slot(game, user_id)
    if "1" not in game["players"]:
        game["players"]["1"] = {"id": user_id, "name": name, "color": "blue"}
        return "1"
    if "2" not in game["players"]:
        game["players"]["2"] = {"id": user_id, "name": name, "color": "red"}
        game["status"] = "rps"
        game["last_move_at"] = time.time()
        return "2"
    return None


def record_rps(game, slot, choice):
    """Record one player's RPS throw. Returns 'waiting', 'tie' or 'decided'."""
    if choice not in ("rock", "paper", "scissors"):
        return "waiting"
    game.setdefault("rps", {})[slot] = choice
    if len(game["rps"]) < 2:
        return "waiting"
    w = decide_rps(game["rps"]["1"], game["rps"]["2"])
    game["rps"] = {}
    if w == 0:
        return "tie"  # both throw again
    game["turn"] = w
    game["starter"] = w
    game["status"] = "playing"
    game["last_move_at"] = time.time()
    return "decided"


def ensure_bot_rps(game):
    """Vs-bot games: the bot must always have a throw in. On a tie both
    throws are cleared, so without this the game would wait forever for a
    throw that never comes — Mehdi hit exactly this deadlock on 2026-11-16
    (game stuck in 'rps' with rps={} after a tie)."""
    if game.get("vs_bot") and game.get("status") == "rps" \
            and "2" not in game.setdefault("rps", {}):
        record_rps(game, "2", random.choice(["rock", "paper", "scissors"]))


def pair_stranger(waiting, user_id, name):
    """Pure /stranger queue: returns the waiting partner (dict) or None.

    The caller removes the partner from the queue when one is returned;
    otherwise the caller appends this user to the queue.
    """
    for w in waiting:
        if w["id"] != user_id:
            return w
    return None


def invite_link(game_id):
    return f"https://t.me/{BOT_USERNAME}?start=game_{game_id}"


def valid_client_id(gid):
    """Game ids the Web App generates: 8 lowercase hex chars."""
    return isinstance(gid, str) and re.fullmatch(r"[0-9a-f]{8}", gid) is not None


def create_client_game(chat_id, user, game_id, mode, vs_bot=False, color=None):
    """A game created from inside the Web App — the client generated the
    id, so the Web App already knows it and can jump straight to the RPS
    screen. Idempotent: a repeated tap returns the same game."""
    if not valid_client_id(game_id):
        return None
    if game_id in GAMES:
        g = GAMES[game_id]
        return g if player_slot(g, user["id"]) == "1" else None
    game = new_game(chat_id, game_id=game_id)
    game["mode"] = mode
    game["vs_bot"] = vs_bot
    GAMES[game["id"]] = game
    BY_CHAT[chat_id] = game["id"]
    join_game(game, user["id"], user.get("first_name", "بازیکن"))
    if color in ("blue", "red", "green", "yellow", "purple", "orange"):
        game["players"]["1"]["color"] = color
    if vs_bot:
        game["players"]["2"] = {"id": 0, "name": "ربات 🤖", "color": "red"}
        game["status"] = "rps"
        record_rps(game, "2", random.choice(["rock", "paper", "scissors"]))
    post_game_message(game, chat_id)
    save_state()
    return game


def do_stranger_webapp(chat_id, user, game_id, color=None):
    """Random-opponent pairing started from the Web App. The client's
    game id becomes the game when this user pairs someone; when this
    user only queues, the id is just a queue marker and the game that
    eventually forms uses the other player's id (they get the entry
    card in chat either way)."""
    if not valid_client_id(game_id):
        return None
    partner = pair_stranger(WAITING, user["id"], user.get("first_name", "بازیکن"))
    if partner is None:
        if not any(w["id"] == user["id"] for w in WAITING):
            WAITING.append({"id": user["id"], "name": user.get("first_name", "بازیکن"),
                            "game": game_id, "color": color})
            save_state()
        return None
    WAITING[:] = [w for w in WAITING if w["id"] != partner["id"]]
    game = new_game(chat_id, game_id=game_id)
    game["mode"] = "stranger"
    GAMES[game["id"]] = game
    BY_CHAT[chat_id] = game["id"]
    join_game(game, partner["id"], partner["name"])
    if partner.get("color") in ("blue", "red", "green", "yellow", "purple", "orange"):
        game["players"]["1"]["color"] = partner["color"]
    join_game(game, user["id"], user.get("first_name", "بازیکن"))
    if color in ("blue", "red", "green", "yellow", "purple", "orange"):
        game["players"]["2"]["color"] = color
    post_game_message(game, chat_id)
    try:
        post_game_message(game, partner["id"])
    except Exception:
        pass
    save_state()
    return game


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"games": GAMES, "by_chat": BY_CHAT, "scores": SCORES,
                       "waiting": WAITING, "offset": OFFSET},
                      f, ensure_ascii=False)
    except OSError:
        pass


def load_state():
    global OFFSET
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        GAMES.update(d.get("games", {}))
        BY_CHAT.update({int(k): v for k, v in d.get("by_chat", {}).items()})
        SCORES.update(d.get("scores", {}))
        WAITING.extend(d.get("waiting", []))
        OFFSET = d.get("offset", 0)
    except (OSError, ValueError):
        pass


def game_for_chat(chat_id):
    gid = BY_CHAT.get(chat_id)
    return GAMES.get(gid) if gid else None


def webapp_url(game):
    q = {
        "g": game["id"],
        "board": board_str(game["board"]),
        "turn": game["turn"],
        "p1": game["players"].get("1", {}).get("name", "بازیکن ۱"),
        "p2": game["players"].get("2", {}).get("name", "بازیکن ۲"),
        "s1": game["score"]["1"],
        "s2": game["score"]["2"],
        "c1": game["players"].get("1", {}).get("color", "blue"),
        "c2": game["players"].get("2", {}).get("color", "red"),
        "u1": game["players"].get("1", {}).get("id", 0),
        "u2": game["players"].get("2", {}).get("id", 0),
        "phase": game["status"],
    }
    return WEBAPP_URL + "?" + urllib.parse.urlencode(q)


def player_name(game, n):
    return game["players"].get(str(n), {}).get("name", f"بازیکن {fa(n)}")


def render_text(game):
    p1, p2 = player_name(game, 1), player_name(game, 2)
    lines = ["🎮 <b>دوز</b>",
             f"🔵 {p1}  —  برد: {fa(game['score']['1'])}",
             f"🔴 {p2}  —  برد: {fa(game['score']['2'])}",
             ""]
    if game["status"] == "joining":
        lines.append("👥 بازی ساخته شد — منتظر نفر دوم…")
        lines.append("از دکمه‌ی «ورود به بازی» لینک دعوت رو برای دوستت بفرست — اون با گوشی خودش وارد می‌شه.")
    elif game["status"] == "rps":
        lines.append("✊✋✌️ هر دو نفر با گوشی خودشون صفحه‌ی بازی رو باز کنن:")
        lines.append("اول رنگ مهره‌ی خودشون، بعد سنگ / کاغذ / قیچی.")
        lines.append("برنده‌ی سنگ‌کاغذقیچی بازی رو شروع می‌کنه.")
        if game.get("rps"):
            done = [player_name(game, int(n)) for n in game["rps"]]
            lines.append(f"✅ انتخاب کرده: {'، '.join(done)} — منتظر نفر دوم…")
    elif game["status"] == "finished":
        if game["winner"]:
            lines.append(f"🏆 برنده: {'🔵 ' if game['winner'] == 1 else '🔴 '}"
                         f"{player_name(game, game['winner'])}")
        else:
            lines.append("🤝 مساوی شد!")
        lines.append("برای دوباره بازی کردن، تو صفحه‌ی بازی «بازی دوباره» رو بزن.")
    else:
        lines.append(f"نوبت {'🔵 ' if game['turn'] == 1 else '🔴 '}"
                     f"{player_name(game, game['turn'])} است")
        if TURN_TIMEOUT:
            lines.append(f"⏱ هر نوبت {fa(TURN_TIMEOUT // 60)} دقیقه وقت داری.")
    lines.append("")
    lines.append(board_text(game["board"]))
    lines.append("۱  ۲  ۳  ۴  ۵  ۶  ۷")
    return "\n".join(lines)


def keyboard(game):
    """v4: Telegram carries NO game controls at all — the message is just
    an entry card. Every choice (mode, colour, RPS, drops, rematch) lives
    inside the Web App. No undo button on purpose: Mehdi removed it —
    it breaks the game."""
    return {"inline_keyboard": [[
        {"text": "🎮 ورود به بازی", "web_app": {"url": webapp_url(game)}}
    ]]}


def entry_keyboard():
    """The /start message: one button, no game yet — the Web App's home
    screen holds all the options."""
    return {"inline_keyboard": [[
        {"text": "🎮 ورود به بازی", "web_app": {"url": WEBAPP_URL}}
    ]]}


def decide_rps(a, b):
    """Rock-paper-scissors: 0 tie, 1 first wins, 2 second wins."""
    if a == b:
        return 0
    wins = {("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")}
    return 1 if (a, b) in wins else 2


# ---------------------------------------------------------------- Bot API
def api(method, params=None):
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(API + TOKEN + "/" + method, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=35) as resp:
        return json.load(resp)


def send(chat_id, text, reply_markup=None, reply_to=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        p["reply_markup"] = reply_markup
    if reply_to:
        p["reply_to_message_id"] = reply_to
    return api("sendMessage", p)


def edit(chat_id, msg_id, text, reply_markup=None):
    p = {"chat_id": chat_id, "message_id": msg_id, "text": text,
         "parse_mode": "HTML"}
    if reply_markup:
        p["reply_markup"] = reply_markup
    try:
        return api("editMessageText", p)
    except Exception:
        return None


def answer_callback(cb_id, text="", alert=False):
    try:
        api("answerCallbackQuery",
            {"callback_query_id": cb_id, "text": text, "show_alert": alert})
    except Exception:
        pass


def post_game_message(game, chat_id):
    """Send the game message into a chat and remember it for syncing.

    Remote play keeps the SAME game message in BOTH players' chats,
    so each sees the board on their own phone.
    """
    m = send(chat_id, render_text(game), keyboard(game))
    entry = {"chat_id": chat_id, "message_id": m["result"]["message_id"]}
    game.setdefault("messages", []).append(entry)
    if not game.get("message_id"):
        game["message_id"] = entry["message_id"]
        game["chat_id"] = chat_id
    save_state()
    return entry


def sync_game_message(game):
    seen = set()
    for entry in list(game.get("messages", [])):
        key = (entry["chat_id"], entry["message_id"])
        if key in seen:
            continue
        seen.add(key)
        edit(entry["chat_id"], entry["message_id"],
             render_text(game), keyboard(game))
    if game.get("message_id") and (game["chat_id"], game["message_id"]) not in seen:
        edit(game["chat_id"], game["message_id"],
             render_text(game), keyboard(game))
    save_state()


def apply_drop(game, player, col):
    """Returns (ok, message). Assumes turn/status already checked by caller."""
    r = lowest_empty(game["board"], col)
    if r == -1:
        return False, "این ستون پر شده!"
    game["board"][r][col] = player
    game["history"].append([r, col, player])
    game["last_move"] = [r, col, player]
    game["last_move_at"] = time.time()
    w = check_win(game["board"], r, col, player)
    if w:
        game["status"] = "finished"
        game["winner"] = player
        game["win_cells"] = w
        game["score"][str(player)] += 1
        SCORES[str(game["chat_id"])] = game["score"]
        return True, "win"
    if is_full(game["board"]):
        game["status"] = "finished"
        game["winner"] = 0
        return True, "draw"
    game["turn"] = 2 if player == 1 else 1
    return True, "ok"


def maybe_bot_move(game):
    if not game.get("vs_bot") or game["status"] != "playing":
        return
    if game["turn"] != 2:
        return
    col = ai_move(game["board"])
    if col == -1:
        return
    time.sleep(0.8)
    apply_drop(game, 2, col)


# ---------------------------------------------------------------- handlers
def handle_command(msg):
    """v4: /start is the only command. It shows the entry button (the
    Web App home holds every option) or joins a deep-linked invite."""
    chat = msg["chat"]
    chat_id, text = chat["id"], (msg.get("text") or "").strip()
    cmd = text.split()[0].lstrip("/").split("@")[0]
    if cmd not in ("start", "help", "راهنما"):
        return
    parts = text.split()
    if len(parts) > 1 and parts[1].startswith("game_"):
        # Deep-link join: a friend tapped the invite link.
        game = GAMES.get(parts[1][5:])
        if not game:
            send(chat_id, "این دعوت بازی دیگه معتبر نیست — از دوستت بخواه یه بازی جدید بسازه.")
            return
        user = msg["from"]
        slot = join_game(game, user["id"], user.get("first_name", "بازیکن"))
        if slot is None:
            send(chat_id, "این بازی already دو نفره شده.")
            return
        post_game_message(game, chat_id)
        send(chat_id,
             f"وارد بازی شدی — تو بازیکن {fa(slot)} هستی ✅\n"
             "دکمه‌ی «ورود به بازی» رو بزن تا صفحه باز بشه: اول رنگ مهره‌ت، "
             "بعد سنگ / کاغذ / قیچی. برنده شروع می‌کنه.")
        sync_game_message(game)
        return
    send(chat_id,
         "🎮 <b>دوز</b>\n\n"
         "بازی توی یه صفحه‌ی تمام‌صفحه باز می‌شه — با عددهای درشت پایین "
         "صفحه و ستون‌های بزرگ.\n\n"
         "دکمه‌ی «ورود به بازی» رو بزن؛ همه‌چیز اون‌جاست: بازی با ربات، "
         "بازی با دوستانم، بازی با رقیب شانسی، رنگ مهره و سنگ‌کاغذقیچی.\n\n"
         "هر بازیکن با گوشی خودش بازی می‌کنه — نه کنار هم با یه گوشی.",
         entry_keyboard())


def handle_callback(query):
    """v4: no Telegram-side game buttons exist any more — every choice
    lives in the Web App. Old messages still carry dead buttons; answer
    them politely instead of crashing or doing anything."""
    answer_callback(query["id"], "این دکمه‌ی قدیمیه — بازی تازه رو از دکمه‌ی «ورود به بازی» شروع کن 🎮")


def handle_webapp_data(msg):
    """Everything comes from the Web App via Telegram.WebApp.sendData().
    Create actions (vs_bot / invite / stranger) carry the client-made
    game id in "game"; in-game actions carry the game id in "g"."""
    try:
        payload = json.loads(msg["web_app_data"]["data"])
    except (KeyError, ValueError):
        return
    user = msg["from"]
    chat_id = msg["chat"]["id"]
    action = payload.get("action")
    if action == "vs_bot":
        create_client_game(chat_id, user, payload.get("game"), "bot",
                           vs_bot=True, color=payload.get("color"))
        return
    if action == "invite":
        create_client_game(chat_id, user, payload.get("game"), "friend",
                           color=payload.get("color"))
        return
    if action == "stranger":
        do_stranger_webapp(chat_id, user, payload.get("game"),
                           color=payload.get("color"))
        return
    game = GAMES.get(payload.get("g") or "") or game_for_chat(chat_id)
    if not game:
        return
    if action == "color":
        # Each player picks ONLY their own colour, on their own phone.
        slot = player_slot(game, user["id"])
        col = payload.get("color")
        if slot and col in ("blue", "red", "green", "yellow", "purple", "orange"):
            game["players"][slot]["color"] = col
        elif slot:
            # Backwards compat: old clients sent c1/c2 for both players.
            col = payload.get("c" + slot)
            if col in ("blue", "red", "green", "yellow", "purple", "orange"):
                game["players"][slot]["color"] = col
        sync_game_message(game)
    elif action == "rps":
        # Each player throws on their own phone; the bot decides once both are in.
        slot = player_slot(game, user["id"])
        if slot and game["status"] == "rps":
            ensure_bot_rps(game)
            if game["status"] == "rps":
                record_rps(game, slot, payload.get("choice"))
            save_state()
            sync_game_message(game)
    elif action == "drop":
        if game["status"] != "playing":
            return
        want = None
        for n in ("1", "2"):
            if game["players"].get(n, {}).get("id") == user["id"]:
                want = int(n)
        if want is None or want != game["turn"]:
            return  # not your turn — silently ignore (Web App already guards)
        col = payload.get("col")
        if not isinstance(col, int) or not 0 <= col < COLS:
            return
        ok, _ = apply_drop(game, want, col)
        if ok:
            maybe_bot_move(game)
        sync_game_message(game)
    elif action == "undo":
        # Undo was removed by Mehdi's order — ignore it.
        return
    elif action == "new":
        # Rematch, all inside the Web App: the client made the new id, so
        # it can jump straight to the RPS screen; the bot posts fresh
        # entry cards into every chat that had the old game.
        if not valid_client_id(payload.get("game")):
            return
        if payload.get("game") in GAMES:
            return
        ng = new_game(game["chat_id"], game.get("chat_title", ""),
                      game_id=payload.get("game"))
        ng["score"] = dict(game["score"])
        ng["players"] = {n: dict(p) for n, p in game["players"].items()}
        ng["mode"] = game.get("mode", "friend")
        ng["vs_bot"] = game.get("vs_bot", False)
        if len(ng["players"]) == 2:
            # Fresh RPS decides the starter again — never auto-start.
            ng["status"] = "rps"
        if ng["vs_bot"]:
            ng["players"]["2"] = {"id": 0, "name": "ربات 🤖", "color": "red"}
            record_rps(ng, "2", random.choice(["rock", "paper", "scissors"]))
        GAMES[ng["id"]] = ng
        BY_CHAT[game["chat_id"]] = ng["id"]
        chats = {e["chat_id"] for e in game.get("messages", [])}
        chats.add(game["chat_id"])
        for cid in chats:
            try:
                post_game_message(ng, cid)
            except Exception:
                pass
        save_state()


def check_timeouts():
    if not TURN_TIMEOUT:
        return
    now = time.time()
    for game in list(GAMES.values()):
        if game["status"] != "playing":
            continue
        if now - game.get("last_move_at", now) > TURN_TIMEOUT:
            loser = game["turn"]
            winner = 2 if loser == 1 else 1
            game["status"] = "finished"
            game["winner"] = winner
            game["score"][str(winner)] += 1
            SCORES[str(game["chat_id"])] = game["score"]
            sync_game_message(game)


def main():
    global OFFSET
    if not TOKEN:
        raise SystemExit("CONNECT4_BOT_TOKEN is not set (env var only — "
                         "never put the token in a file).")
    load_state()
    print("Connect4 bot running. WebApp:", WEBAPP_URL, flush=True)
    last_timeout_check = 0
    while True:
        try:
            resp = api("getUpdates", {"offset": OFFSET, "timeout": 30})
        except Exception as e:
            print("getUpdates failed:", e, flush=True)
            time.sleep(5)
            continue
        for upd in resp.get("result", []):
            OFFSET = upd["update_id"] + 1
            if "message" in upd:
                msg = upd["message"]
                if "web_app_data" in msg:
                    handle_webapp_data(msg)
                elif (msg.get("text") or "").startswith("/"):
                    handle_command(msg)
            elif "callback_query" in upd:
                handle_callback(upd["callback_query"])
        save_state()  # persist the offset (and games) after every batch
        if time.time() - last_timeout_check > 30:
            check_timeouts()
            last_timeout_check = time.time()


if __name__ == "__main__":
    main()
