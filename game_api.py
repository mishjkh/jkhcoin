import hashlib
import hmac
import json
import math
import os
import re
import secrets
import string
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


router = APIRouter(prefix="/api")
CAPACITY = 1000
RECHARGE_MS = 3600
DAY_MS = 86_400_000
FARM_DURATION_MS = DAY_MS * 3
FARM_TOKEN_BATCH = 100
FARM_DAILY_INCOME = 100
MAX_INIT_DATA_AGE_SECONDS = 86_400


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL must be configured for cloud game profiles.")
    return value


def initialize_database() -> None:
    with psycopg.connect(_database_url()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS game_profiles (
                telegram_user_id BIGINT PRIMARY KEY,
                player_id TEXT NOT NULL UNIQUE,
                card_number VARCHAR(12) NOT NULL UNIQUE,
                referral_code VARCHAR(11) NOT NULL UNIQUE,
                referral_used BOOLEAN NOT NULL DEFAULT FALSE,
                referral_code_used BOOLEAN NOT NULL DEFAULT FALSE,
                revision BIGINT NOT NULL DEFAULT 1,
                state JSONB NOT NULL,
                updated_at BIGINT NOT NULL
            )
            """
        )
        connection.execute(
            "ALTER TABLE game_profiles ADD COLUMN IF NOT EXISTS "
            "referral_code_used BOOLEAN NOT NULL DEFAULT FALSE"
        )


def _telegram_user(init_data: str | None) -> dict[str, Any]:
    token = os.environ.get("BOT_TOKEN")
    if not token or not init_data:
        raise HTTPException(status_code=401, detail="Open the game from its Telegram bot.")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        values = dict(pairs)
        received_hash = values.pop("hash")
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
    except (ValueError, KeyError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Invalid Telegram session data.") from None
    if len(values) != len(pairs) - 1 or not isinstance(user, dict):
        raise HTTPException(status_code=401, detail="Invalid Telegram session data.")
    now = int(time.time())
    if auth_date > now + 60 or now - auth_date > MAX_INIT_DATA_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Telegram session has expired. Reopen the game.")
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram session signature.")
    user_id = user.get("id")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise HTTPException(status_code=401, detail="Telegram user ID is missing.") from None
    if user_id <= 0:
        raise HTTPException(status_code=401, detail="Invalid Telegram user ID.")
    return {"id": user_id, "username": user.get("username")}


def _auth(x_telegram_init_data: str | None = Header(default=None)) -> dict[str, Any]:
    return _telegram_user(x_telegram_init_data)


def _random_id() -> str:
    return "".join(secrets.choice(string.ascii_uppercase) for _ in range(3)) + "".join(
        secrets.choice(string.digits) for _ in range(7)
    )


def _random_card() -> str:
    return "8888" + "".join(secrets.choice(string.digits) for _ in range(8))


def _random_referral_code() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(11))


def _number(value: Any, default: int = 0, maximum: int = 2_000_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    return max(0, min(maximum, int(value)))


def _legacy_state(legacy: Any, player_id: str, card_number: str, referral_code: str) -> dict[str, Any]:
    legacy = legacy if isinstance(legacy, dict) else {}
    try:
        game = json.loads(legacy.get("game") or "{}")
    except (TypeError, json.JSONDecodeError):
        game = {}
    if not isinstance(game, dict):
        game = {}
    return {
        "playerId": player_id,
        "game": {
            "energy": CAPACITY,
            "total": _number(game.get("total")),
            "coins": 0,
            "updatedAt": int(time.time() * 1000),
        },
        "bank": {
            "cardNumber": card_number,
            "tokenBalance": 0,
            "farm": {"purchases": 0, "active": None},
        },
        "referral": {"code": referral_code, "used": False, "codeUsed": False},
        "giftDate": "",
        "language": "en" if legacy.get("language") == "en" else "ru",
        "wallet": {"address": "", "chain": ""},
    }


def _farm_production(state: dict[str, Any], now: int) -> bool:
    bank = state["bank"]
    farm = bank["farm"]
    active = farm.get("active")
    if not isinstance(active, dict):
        return False
    elapsed = min(FARM_DURATION_MS, max(0, now - active["startedAt"]))
    produced = min(FARM_DAILY_INCOME * 3, elapsed * FARM_DAILY_INCOME / DAY_MS)
    batches = min(3, int((produced + 1e-8) // FARM_TOKEN_BATCH))
    if batches <= active["creditedBatches"]:
        return False
    bank["tokenBalance"] += (batches - active["creditedBatches"]) * FARM_TOKEN_BATCH
    active["creditedBatches"] = batches
    return True


def _refresh_energy(state: dict[str, Any], now: int) -> bool:
    game = state["game"]
    energy = game["energy"]
    updated_at = game["updatedAt"]
    elapsed = max(0, now - updated_at)
    restored = elapsed // RECHARGE_MS
    if restored <= 0:
        return False
    game["energy"] = min(CAPACITY, energy + restored)
    game["updatedAt"] = now if game["energy"] == CAPACITY else updated_at + restored * RECHARGE_MS
    return True


def _fetch_profile(connection: psycopg.Connection, user_id: int, lock: bool = False) -> dict[str, Any]:
    suffix = " FOR UPDATE" if lock else ""
    row = connection.execute(
        f"SELECT player_id, card_number, referral_code, referral_used, referral_code_used, revision, state "
        f"FROM game_profiles WHERE telegram_user_id = %s{suffix}",
        (user_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Game profile is not initialized.")
    return row


def _save_profile(connection: psycopg.Connection, user_id: int, row: dict[str, Any], state: dict[str, Any]) -> None:
    connection.execute(
        "UPDATE game_profiles SET state = %s, revision = revision + 1, updated_at = %s "
        "WHERE telegram_user_id = %s",
        (Jsonb(state), int(time.time() * 1000), user_id),
    )
    row["revision"] += 1


def _response(row: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {"state": state, "revision": row["revision"]}


@router.post("/profile/bootstrap")
def bootstrap_profile(
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(_auth),
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    with psycopg.connect(_database_url(), row_factory=dict_row) as connection:
        existing = connection.execute(
            "SELECT player_id, card_number, referral_code, referral_used, referral_code_used, revision, state "
            "FROM game_profiles WHERE telegram_user_id = %s FOR UPDATE",
            (user["id"],),
        ).fetchone()
        if existing is None:
            player_id, card_number, referral_code = _random_id(), _random_card(), _random_referral_code()
            state = _legacy_state(payload.get("legacy"), player_id, card_number, referral_code)
            for _ in range(5):
                try:
                    with connection.transaction():
                        connection.execute(
                            "INSERT INTO game_profiles (telegram_user_id, player_id, card_number, referral_code, "
                            "referral_used, referral_code_used, revision, state, updated_at) "
                            "VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s) "
                            "ON CONFLICT (telegram_user_id) DO NOTHING",
                            (
                                user["id"], player_id, card_number, referral_code,
                                state["referral"]["used"], state["referral"]["codeUsed"], Jsonb(state), now,
                            ),
                        )
                    break
                except psycopg.errors.UniqueViolation:
                    card_number, referral_code = _random_card(), _random_referral_code()
            existing = _fetch_profile(connection, user["id"], lock=True)
            if existing["state"]["bank"]["cardNumber"] != existing["card_number"]:
                existing["state"]["bank"]["cardNumber"] = existing["card_number"]
                existing["state"]["referral"]["code"] = existing["referral_code"]
                _save_profile(connection, user["id"], existing, existing["state"])
        state = existing["state"]
        changed = _refresh_energy(state, now) | _farm_production(state, now)
        if changed:
            _save_profile(connection, user["id"], existing, state)
        return _response(existing, state)


@router.post("/action/tap")
def tap(user: dict[str, Any] = Depends(_auth)) -> dict[str, Any]:
    now = int(time.time() * 1000)
    with psycopg.connect(_database_url(), row_factory=dict_row) as connection:
        row = _fetch_profile(connection, user["id"], lock=True)
        state = row["state"]
        _refresh_energy(state, now)
        _farm_production(state, now)
        if state["game"]["energy"] <= 0:
            raise HTTPException(status_code=409, detail="No energy remaining.")
        state["game"]["energy"] -= 1
        state["game"]["total"] += 1
        _save_profile(connection, user["id"], row, state)
        return _response(row, state)


@router.post("/action/gift")
def claim_gift(user: dict[str, Any] = Depends(_auth)) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date().isoformat()
    with psycopg.connect(_database_url(), row_factory=dict_row) as connection:
        row = _fetch_profile(connection, user["id"], lock=True)
        state = row["state"]
        _refresh_energy(state, int(time.time() * 1000))
        _farm_production(state, int(time.time() * 1000))
        if state.get("giftDate") == today:
            raise HTTPException(status_code=409, detail="Daily reward already claimed.")
        state["giftDate"] = today
        state["game"]["coins"] += 10
        _save_profile(connection, user["id"], row, state)
        return _response(row, state)


@router.post("/action/farm")
def buy_farm(user: dict[str, Any] = Depends(_auth)) -> dict[str, Any]:
    now = int(time.time() * 1000)
    with psycopg.connect(_database_url(), row_factory=dict_row) as connection:
        row = _fetch_profile(connection, user["id"], lock=True)
        state = row["state"]
        _refresh_energy(state, now)
        _farm_production(state, now)
        farm = state["bank"]["farm"]
        active = farm.get("active")
        if isinstance(active, dict) and now - active["startedAt"] < FARM_DURATION_MS:
            raise HTTPException(status_code=409, detail="The farm cycle is still running.")
        price = 1000 + farm["purchases"] * 500
        if state["game"]["coins"] < price:
            raise HTTPException(status_code=409, detail="Not enough coins.")
        state["game"]["coins"] -= price
        farm["purchases"] += 1
        farm["active"] = {"startedAt": now, "creditedBatches": 0}
        _save_profile(connection, user["id"], row, state)
        return _response(row, state)


@router.post("/action/exchange")
def exchange_tokens(user: dict[str, Any] = Depends(_auth)) -> dict[str, Any]:
    now = int(time.time() * 1000)
    with psycopg.connect(_database_url(), row_factory=dict_row) as connection:
        row = _fetch_profile(connection, user["id"], lock=True)
        state = row["state"]
        _refresh_energy(state, now)
        _farm_production(state, now)
        if state["bank"]["tokenBalance"] < FARM_TOKEN_BATCH:
            raise HTTPException(status_code=409, detail="Not enough tokens.")
        state["bank"]["tokenBalance"] -= FARM_TOKEN_BATCH
        state["game"]["coins"] += 1000
        _save_profile(connection, user["id"], row, state)
        return _response(row, state)


@router.post("/action/transfer")
def transfer_tokens(
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(_auth),
) -> dict[str, Any]:
    card_number = str(payload.get("cardNumber", "")).replace(" ", "")
    amount = payload.get("amount")
    if not re.fullmatch(r"8888\d{8}", card_number):
        raise HTTPException(status_code=400, detail="Enter a valid 12-digit card number.")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise HTTPException(status_code=400, detail="Enter a positive whole number of tokens.")
    now = int(time.time() * 1000)
    with psycopg.connect(_database_url(), row_factory=dict_row) as connection:
        recipient = connection.execute(
            "SELECT telegram_user_id FROM game_profiles WHERE card_number = %s",
            (card_number,),
        ).fetchone()
        if recipient is None:
            raise HTTPException(status_code=404, detail="Card not found.")
        recipient_id = recipient["telegram_user_id"]
        if recipient_id == user["id"]:
            raise HTTPException(status_code=400, detail="Cannot transfer tokens to yourself.")
        rows = connection.execute(
            "SELECT telegram_user_id, player_id, card_number, referral_code, referral_used, "
            "referral_code_used, revision, state "
            "FROM game_profiles WHERE telegram_user_id = ANY(%s) ORDER BY telegram_user_id FOR UPDATE",
            ([user["id"], recipient_id],),
        ).fetchall()
        by_id = {entry["telegram_user_id"]: entry for entry in rows}
        sender_row, recipient_row = by_id[user["id"]], by_id[recipient_id]
        sender_state, recipient_state = sender_row["state"], recipient_row["state"]
        _farm_production(sender_state, now)
        _farm_production(recipient_state, now)
        if sender_state["bank"]["tokenBalance"] < amount:
            raise HTTPException(status_code=409, detail="Insufficient token balance.")
        sender_state["bank"]["tokenBalance"] -= amount
        recipient_state["bank"]["tokenBalance"] += amount
        _save_profile(connection, user["id"], sender_row, sender_state)
        _save_profile(connection, recipient_id, recipient_row, recipient_state)
        return _response(sender_row, sender_state)


@router.post("/action/referral")
def redeem_referral(
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(_auth),
) -> dict[str, Any]:
    code = str(payload.get("code", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9]{11}", code):
        raise HTTPException(status_code=400, detail="Invalid invite code.")
    with psycopg.connect(_database_url(), row_factory=dict_row) as connection:
        owner = connection.execute(
            "SELECT telegram_user_id, referral_code_used FROM game_profiles WHERE referral_code = %s",
            (code,),
        ).fetchone()
        if owner is None:
            raise HTTPException(status_code=404, detail="Invite code not found.")
        owner_id = owner["telegram_user_id"]
        if owner_id == user["id"]:
            raise HTTPException(status_code=400, detail="Cannot redeem your own invite code.")
        if owner["referral_code_used"]:
            raise HTTPException(status_code=409, detail="Invite code already redeemed.")
        rows = connection.execute(
            "SELECT telegram_user_id, player_id, card_number, referral_code, referral_used, "
            "referral_code_used, revision, state "
            "FROM game_profiles WHERE telegram_user_id = ANY(%s) ORDER BY telegram_user_id FOR UPDATE",
            ([user["id"], owner_id],),
        ).fetchall()
        by_id = {entry["telegram_user_id"]: entry for entry in rows}
        if by_id[user["id"]]["referral_used"]:
            raise HTTPException(status_code=409, detail="Invite code already redeemed.")
        if by_id[owner_id]["referral_code_used"]:
            raise HTTPException(status_code=409, detail="Invite code already redeemed.")
        caller_row = by_id[user["id"]]
        owner_row = by_id[owner_id]
        state = caller_row["state"]
        state["referral"]["used"] = True
        state["referral"]["usedCode"] = code
        owner_state = owner_row["state"]
        owner_state["referral"]["codeUsed"] = True
        _save_profile(connection, owner_id, owner_row, owner_state)
        connection.execute(
            "UPDATE game_profiles SET referral_code_used = TRUE WHERE telegram_user_id = %s",
            (owner_id,),
        )
        _save_profile(connection, user["id"], caller_row, state)
        connection.execute(
            "UPDATE game_profiles SET referral_used = TRUE WHERE telegram_user_id = %s",
            (user["id"],),
        )
        caller_row["referral_used"] = True
        return _response(caller_row, state)


@router.post("/action/language")
def set_language(
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(_auth),
) -> dict[str, Any]:
    language = payload.get("language")
    if language not in {"ru", "en"}:
        raise HTTPException(status_code=400, detail="Unsupported language.")
    with psycopg.connect(_database_url(), row_factory=dict_row) as connection:
        row = _fetch_profile(connection, user["id"], lock=True)
        state = row["state"]
        state["language"] = language
        _save_profile(connection, user["id"], row, state)
        return _response(row, state)


@router.post("/action/wallet")
def save_wallet(
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(_auth),
) -> dict[str, Any]:
    address = payload.get("address")
    chain = payload.get("chain")
    if not isinstance(address, str) or not address or len(address) > 128:
        raise HTTPException(status_code=400, detail="Invalid TON wallet address.")
    if chain not in {"-239", "-3"}:
        raise HTTPException(status_code=400, detail="Unsupported TON network.")
    with psycopg.connect(_database_url(), row_factory=dict_row) as connection:
        row = _fetch_profile(connection, user["id"], lock=True)
        state = row["state"]
        state["wallet"] = {"address": address, "chain": chain}
        _save_profile(connection, user["id"], row, state)
        return _response(row, state)
