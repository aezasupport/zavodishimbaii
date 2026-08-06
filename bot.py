# -*- coding: utf-8 -*-
"""🏭 Бот «Чертежи и техпроцессы» + расчёт гитары — всё в одном файле."""

import asyncio
import html
import logging
import os
import uuid
from datetime import datetime
from math import ceil

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

# ==========================================================
# НАСТРОЙКИ — МЕНЯТЬ ЗДЕСЬ
# ==========================================================

# ⚠️ Токен светился в переписке — перевыпусти через @BotFather (/revoke)!
BOT_TOKEN = "8892313129678698:AAFyTruueldzlN5X8um0lPsa-BxeK0evcKc"

# Твой Telegram-ID (узнать у @userinfobot)
ADMIN_IDS = [
    8753132131284165,  # ← замени на свой ID
]

DB_PATH = "bot.db"
STORAGE_DIR = "storage"
PARTS_PER_PAGE = 20
LIST_PER_PAGE = 15
MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}

# --- Расчёт гитары ---
# Твой комплект сменных шестерён (числа зубьев) — измени под свой набор!
GEAR_SET = [20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100]
GEAR_MIN_DELTA = 15     # правило из справочника: |sum1 - sum2| >= 15 для двойной гитары
MAX_RATIO_ERROR = 0.005 # допуск подбора: 0.5%

WELCOME = (
    "🏭 <b>Чертежи и техпроцессы</b>\n\n"
    "Здесь можно быстро найти и получить чертёж/техпроцесс\n"
    "по номеру детали, а также рассчитать гитару.\n\n"
    "Воспользуйся кнопками меню 👇"
)
HELP_TEXT = (
    "ℹ️ <b>Как пользоваться</b>\n\n"
    "1️⃣ «📋 Каталог деталей» — выбери номер детали,\n"
    "бот пришлёт все чертежи по ней.\n\n"
    "2️⃣ «🔍 Поиск по номеру» — введи номер (или часть).\n\n"
    "3️⃣ «🎛 Расчёт гитары» — подбор сменных шестерён\n"
    "под нужное передаточное отношение.\n\n"
    "Файлы можно пересылать коллегам. Добавление чертежей — у админа."
)
ADMIN_MENU_TEXT = "⚙️ <b>Админ-панель</b>\n\nВыбери действие:"

user_router = Router()
admin_router = Router()


class SearchState(StatesGroup):
    waiting = State()
    results = State()


class AddDrawing(StatesGroup):
    part_number = State()
    files = State()


class GuitarState(StatesGroup):
    ratio = State()
    z_part = State()
    z_tool = State()
    direction = State()


# ==========================================================
# ХЕЛПЕРЫ
# ==========================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def safe_edit(message: Message, text: str, kb=None):
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await message.answer(text, reply_markup=kb)


async def admin_ok(call: CallbackQuery) -> bool:
    if is_admin(call.from_user.id):
        return True
    await call.answer("⛔ Нет доступа", show_alert=True)
    return False


def catalog_label(row) -> str:
    return f"{row['part_number']} ({row['cnt']})" if row["cnt"] else row["part_number"]


def del_label(row) -> str:
    return f"🗑 {row['part_number']} ({row['cnt']})"


# ==========================================================
# РАСЧЁТ ГИТАРЫ
# ==========================================================

def pick_gears(i: float):
    """Подбор шестерён из GEAR_SET под отношение i.
    Возвращает список кортежей (ошибка, (a,b) или (a,b,c,d))."""
    found = []
    # простая гитара (2 шестерни)
    for a in GEAR_SET:
        for b in GEAR_SET:
            err = abs(a / b - i) / i
            if err <= MAX_RATIO_ERROR:
                found.append((err, (a, b)))
    # двойная гитара (4 шестерни)
    seen = set()
    for a in GEAR_SET:
        for b in GEAR_SET:
            s1 = a + b
            for c in GEAR_SET:
                for d in GEAR_SET:
                    s2 = c + d
                    if abs(s1 - s2) < GEAR_MIN_DELTA:
                        continue
                    err = abs((a * c) / (b * d) - i) / i
                    if err <= MAX_RATIO_ERROR:
                        key = tuple(sorted(((a, b), (c, d))))
                        if key in seen:
                            continue
                        seen.add(key)
                        found.append((err, (a, b, c, d)))
    found.sort(key=lambda x: x[0])
    return found


def format_guitar_results(i: float, z_info, found) -> str:
    lines = ["🎛 <b>Расчёт гитары</b>"]
    if z_info:
        lines.append(f"z детали: {z_info[0]} | z инструмента: {z_info[1]}")
    lines.append(f"Нужное i = {i:.5f}\n")
    simple = [f for f in found if len(f[1]) == 2]
    double = [f for f in found if len(f[1]) == 4]
    if simple:
        lines.append("<b>Простая (2 шестерни):</b>")
        for err, g in simple[:3]:
            lines.append(f"• {g[0]} → {g[1]}  (i={g[0] / g[1]:.5f}, Δ{err * 100:.3f}%)")
    if double:
        lines.append("\n<b>Двойная (4 шестерни):</b>")
        for err, g in double[:5]:
            lines.append(f"• {g[0]}→{g[1]} и {g[2]}→{g[3]}  "
                         f"(i={(g[0] * g[2]) / (g[1] * g[3]):.5f}, Δ{err * 100:.3f}%)")
    if not simple and not double:
        lines.append("😕 Не подобралось с точностью ±0.5%.\n"
                     "Проверь i или измени GEAR_SET / MAX_RATIO_ERROR в bot.py.")
    lines.append("\n<i>Набор в расчёте: " + ", ".join(map(str, GEAR_SET)) + "</i>")
    return "\n".join(lines)


# ==========================================================
# БАЗА ДАННЫХ (SQLite)
# ==========================================================

async def connect() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    return db


async def init_db() -> None:
    db = await connect()
    try:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS parts (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   part_number TEXT NOT NULL UNIQUE,
                   name        TEXT DEFAULT '')"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS drawings (
                   id            INTEGER PRIMARY KEY AUTOINCREMENT,
                   part_id       INTEGER NOT NULL,
                   file_path     TEXT NOT NULL,
                   original_name TEXT,
                   description   TEXT DEFAULT '',
                   added_by      INTEGER,
                   added_at      TEXT,
                   FOREIGN KEY (part_id) REFERENCES parts (id) ON DELETE CASCADE)"""
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_drawings_part ON drawings (part_id)")
        await db.commit()
    finally:
        await db.close()


async def get_or_create_part(part_number: str, name: str = "") -> int:
    db = await connect()
    try:
        cur = await db.execute("SELECT id FROM parts WHERE UPPER(part_number) = UPPER(?)", (part_number,))
        row = await cur.fetchone()
        if row:
            return row["id"]
        cur = await db.execute("INSERT INTO parts (part_number, name) VALUES (?, ?)", (part_number, name))
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def get_part(part_id: int):
    db = await connect()
    try:
        cur = await db.execute("SELECT * FROM parts WHERE id = ?", (part_id,))
        return await cur.fetchone()
    finally:
        await db.close()


async def get_parts_with_counts():
    db = await connect()
    try:
        cur = await db.execute(
            """SELECT p.id, p.part_number, p.name, COUNT(d.id) AS cnt
               FROM parts p LEFT JOIN drawings d ON d.part_id = p.id
               GROUP BY p.id ORDER BY p.part_number COLLATE NOCASE"""
        )
        return await cur.fetchall()
    finally:
        await db.close()


async def search_parts(query: str):
    db = await connect()
    try:
        cur = await db.execute(
            """SELECT p.id, p.part_number, p.name, COUNT(d.id) AS cnt
               FROM parts p LEFT JOIN drawings d ON d.part_id = p.id
               WHERE UPPER(p.part_number) LIKE UPPER(?) OR UPPER(p.name) LIKE UPPER(?)
               GROUP BY p.id ORDER BY p.part_number COLLATE NOCASE LIMIT 50""",
            (f"%{query}%", f"%{query}%"),
        )
        return await cur.fetchall()
    finally:
        await db.close()


async def add_drawing(part_id: int, file_path: str, original_name: str,
                      description: str, added_by: int) -> int:
    db = await connect()
    try:
        cur = await db.execute(
            """INSERT INTO drawings (part_id, file_path, original_name, description, added_by, added_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (part_id, file_path, original_name, description, added_by,
             datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def get_drawings(part_id: int):
    db = await connect()
    try:
        cur = await db.execute("SELECT * FROM drawings WHERE part_id = ? ORDER BY id", (part_id,))
        return await cur.fetchall()
    finally:
        await db.close()


async def get_drawing(drawing_id: int):
    db = await connect()
    try:
        cur = await db.execute("SELECT * FROM drawings WHERE id = ?", (drawing_id,))
        return await cur.fetchone()
    finally:
        await db.close()


async def delete_drawing(drawing_id: int) -> str | None:
    db = await connect()
    try:
        cur = await db.execute("SELECT file_path FROM drawings WHERE id = ?", (drawing_id,))
        row = await cur.fetchone()
        if not row:
            return None
        await db.execute("DELETE FROM drawings WHERE id = ?", (drawing_id,))
        await db.commit()
        return row["file_path"]
    finally:
        await db.close()


async def delete_part(part_id: int) -> list[str]:
    db = await connect()
    try:
        cur = await db.execute("SELECT file_path FROM drawings WHERE part_id = ?", (part_id,))
        paths = [r["file_path"] for r in await cur.fetchall()]
        await db.execute("DELETE FROM parts WHERE id = ?", (part_id,))
        await db.commit()
        return paths
    finally:
        await db.close()


async def get_stats() -> dict:
    db = await connect()
    try:
        cur = await db.execute("SELECT COUNT(*) AS c FROM parts")
        parts = (await cur.fetchone())["c"]
        cur = await db.execute("SELECT COUNT(*) AS c FROM drawings")
        files = (await cur.fetchone())["c"]
        return {"parts": parts, "files": files}
    finally:
        await db.close()


# ==========================================================
# КЛАВИАТУРЫ
# ==========================================================

def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def main_menu(is_adm: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [btn("📋 Каталог деталей", "menu:catalog")],
        [btn("🔍 Поиск по номеру", "menu:search")],
        [btn("🎛 Расчёт гитары", "menu:guitar")],
        [btn("ℹ️ Справка", "menu:help")],
    ]
    if is_adm:
        rows.append([btn("⚙️ Админ-панель", "adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Назад в меню", "menu:main")]])


def back_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Назад в админ-панель", "adm:menu")]])


def parts_page_kb(parts, page: int, prefix: str, label_fmt,
                  back: tuple = ("⬅️ Назад в меню", "menu:main")):
    total_pages = max(1, ceil(len(parts) / PARTS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))
    rows = []
    for row in parts[page * PARTS_PER_PAGE:(page + 1) * PARTS_PER_PAGE]:
        rows.append([btn(label_fmt(row)[:40], f"{prefix}:{row['id']}")])
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("⬅️", f"{prefix}:page:{page - 1}"))
        nav.append(btn(f"📄 {page + 1}/{total_pages}", "noop"))
        if page < total_pages - 1:
            nav.append(btn("➡️", f"{prefix}:page:{page + 1}"))
        rows.append(nav)
    rows.append([btn(back[0], back[1])])
    return InlineKeyboardMarkup(inline_keyboard=rows), page


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("➕ Добавить чертёж", "adm:add")],
        [btn("🗑 Удалить чертёж/деталь", "adm:del")],
        [btn("📋 Список деталей", "adm:list")],
        [btn("📊 Статистика", "adm:stats")],
        [btn("⬅️ Главное меню", "menu:main")],
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Отмена", "add:cancel")]])


def add_files_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ Готово", "add:done")],
        [btn("❌ Отмена", "add:cancel")],
    ])


def part_files_admin_kb(drawings, part_id: int) -> InlineKeyboardMarkup:
    rows = []
    for d in drawings:
        label = d["original_name"] or f"файл #{d['id']}"
        if d["description"]:
            label += f" — {d['description']}"
        rows.append([btn(f"❌ {label}"[:45], f"dfile:{d['id']}")])
    rows.append([btn("💥 Удалить деталь целиком", f"dwhole:{part_id}")])
    rows.append([btn("⬅️ Назад", "adm:del")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_delete_part_kb(part_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ Да, удалить всё", f"dwy:{part_id}")],
        [btn("↩️ Нет, назад", f"dpart:{part_id}")],
    ])


def guitar_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🔢 Ввести отношение i вручную", "guitar:ratio")],
        [btn("⚙️ По числу зубьев", "guitar:teeth")],
        [btn("⬅️ Назад в меню", "menu:main")],
    ])


def guitar_dir_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("i = z_инстр / z_дет", "guitar:dir1")],
        [btn("i = z_дет / z_инстр", "guitar:dir2")],
        [btn("❌ Отмена", "guitar:cancel")],
    ])


# ==========================================================
# ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЕЙ
# ==========================================================

async def send_part_files(message: Message, part) -> None:
    drawings = await get_drawings(part["id"])
    title = f"📐 <b>{html.escape(part['part_number'])}</b>"
    if part["name"]:
        title += f" — {html.escape(part['name'])}"
    await message.answer(title)
    for d in drawings:
        path = d["file_path"]
        if not os.path.exists(path):
            await message.answer(f"⚠️ Файл #{d['id']} не найден на диске, сообщите администратору")
            continue
        caption = html.escape(d["description"])[:1000] if d["description"] else None
        input_file = FSInputFile(path, filename=d["original_name"] or os.path.basename(path))
        try:
            if os.path.splitext(path)[1].lower() in PHOTO_EXT:
                await message.answer_photo(input_file, caption=caption)
            else:
                await message.answer_document(input_file, caption=caption)
        except TelegramBadRequest as e:
            await message.answer(f"⚠️ Не удалось отправить файл {d['original_name']}: {e}")


@user_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME, reply_markup=main_menu(is_admin(message.from_user.id)))


@user_router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, reply_markup=back_main_kb())


@user_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu(is_admin(message.from_user.id)))


@user_router.callback_query(F.data == "menu:main")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call.message, WELCOME, main_menu(is_admin(call.from_user.id)))
    await call.answer()


@user_router.callback_query(F.data == "menu:help")
async def cb_help(call: CallbackQuery):
    await safe_edit(call.message, HELP_TEXT, back_main_kb())
    await call.answer()


@user_router.callback_query(F.data == "menu:catalog")
async def cb_catalog(call: CallbackQuery):
    parts = await get_parts_with_counts()
    if not parts:
        await safe_edit(call.message, "📋 Каталог пока пуст — администратор ещё не добавил чертежи.", back_main_kb())
        await call.answer()
        return
    kb, _ = parts_page_kb(parts, 0, "open", catalog_label)
    await safe_edit(call.message, "📋 Выбери номер детали:", kb)
    await call.answer()


@user_router.callback_query(F.data.startswith("open:page:"))
async def cb_catalog_page(call: CallbackQuery):
    page = int(call.data.split(":")[2])
    parts = await get_parts_with_counts()
    kb, _ = parts_page_kb(parts, page, "open", catalog_label)
    await safe_edit(call.message, "📋 Выбери номер детали:", kb)
    await call.answer()


@user_router.callback_query(F.data.startswith("open:"))
async def cb_open_part(call: CallbackQuery):
    part_id = int(call.data.split(":")[1])
    part = await get_part(part_id)
    if not part:
        await call.answer("Деталь не найдена", show_alert=True)
        return
    drawings = await get_drawings(part_id)
    if not drawings:
        await call.answer("По этой детали чертежи ещё не добавлены", show_alert=True)
        return
    await call.answer("Отправляю чертежи… ⏳")
    await send_part_files(call.message, part)


@user_router.callback_query(F.data == "menu:search")
async def cb_search(call: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.waiting)
    await safe_edit(call.message, "🔍 Введи номер детали (или его часть) текстом:\n\nОтмена — /start")
    await call.answer()


@user_router.message(SearchState.waiting, F.text)
async def search_input(message: Message, state: FSMContext):
    query = message.text.strip()
    if not query or query.startswith("/"):
        await state.clear()
        await message.answer(WELCOME, reply_markup=main_menu(is_admin(message.from_user.id)))
        return
    parts = await search_parts(query)
    if not parts:
        await state.clear()
        await message.answer(f"😕 По запросу «{html.escape(query)}» ничего не найдено.", reply_markup=back_main_kb())
        return
    await state.set_state(SearchState.results)
    await state.update_data(query=query)
    kb, _ = parts_page_kb(parts, 0, "srch", catalog_label)
    await message.answer(f"🔎 Результаты по запросу «{html.escape(query)}»:", reply_markup=kb)


@user_router.message(SearchState.waiting)
async def search_wrong_type(message: Message):
    await message.answer("Пожалуйста, отправь номер детали текстом.")


@user_router.callback_query(F.data.startswith("srch:page:"))
async def cb_search_page(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split(":")[2])
    data = await state.get_data()
    parts = await search_parts(data.get("query", ""))
    if not parts:
        await call.answer("Ничего не найдено", show_alert=True)
        return
    kb, _ = parts_page_kb(parts, page, "srch", catalog_label)
    await safe_edit(call.message, "🔎 Результаты поиска:", kb)
    await call.answer()


# ---------- расчёт гитары ----------

@user_router.callback_query(F.data == "menu:guitar")
async def cb_guitar(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(
        call.message,
        "🎛 <b>Расчёт гитары сменных шестерен</b>\n\n"
        "Подбор набора шестерён под нужное передаточное отношение.\n"
        "Если в паспорте станка есть постоянная K — используй ручной ввод:\n"
        "i = K · z_инстр / z_дет.\n\n"
        "Выбери способ:",
        guitar_menu_kb(),
    )
    await call.answer()


@user_router.callback_query(F.data == "guitar:ratio")
async def cb_guitar_ratio(call: CallbackQuery, state: FSMContext):
    await state.set_state(GuitarState.ratio)
    await safe_edit(call.message, "🔢 Введи нужное передаточное отношение i числом.\n"
                                  "Например: 0.4167 или 1.25\n\nОтмена — /start")
    await call.answer()


@user_router.message(GuitarState.ratio, F.text)
async def guitar_ratio_input(message: Message, state: FSMContext):
    text = message.text.strip().replace(",", ".")
    if text.startswith("/"):
        await state.clear()
        return
    try:
        i = float(text)
    except ValueError:
        await message.answer("Не понял число. Пример: 0.4167")
        return
    if i <= 0:
        await message.answer("i должно быть больше нуля.")
        return
    await state.clear()
    found = pick_gears(i)
    await message.answer(format_guitar_results(i, None, found), reply_markup=guitar_menu_kb())


@user_router.callback_query(F.data == "guitar:teeth")
async def cb_guitar_teeth(call: CallbackQuery, state: FSMContext):
    await state.set_state(GuitarState.z_part)
    await safe_edit(call.message, "⚙️ Сколько зубьев у ДЕТАЛИ (нарезаемой шестерни)?")
    await call.answer()


@user_router.message(GuitarState.z_part, F.text)
async def guitar_z_part(message: Message, state: FSMContext):
    text = message.text.strip()
    if text.startswith("/"):
        await state.clear()
        return
    try:
        z = int(text)
    except ValueError:
        await message.answer("Нужно целое число зубьев.")
        return
    if z <= 0:
        await message.answer("Число зубьев должно быть больше нуля.")
        return
    await state.set_state(GuitarState.z_tool)
    await state.update_data(z_part=z)
    await message.answer(f"Принято: z детали = {z}.\n\nТеперь: сколько зубьев у ИНСТРУМЕНТА (долбяка/фрезы)?")


@user_router.message(GuitarState.z_tool, F.text)
async def guitar_z_tool(message: Message, state: FSMContext):
    text = message.text.strip()
    if text.startswith("/"):
        await state.clear()
        return
    try:
        z = int(text)
    except ValueError:
        await message.answer("Нужно целое число зубьев.")
        return
    if z <= 0:
        await message.answer("Число зубьев должно быть больше нуля.")
        return
    await state.update_data(z_tool=z)
    await state.set_state(GuitarState.direction)
    await message.answer("Как считается i по схеме твоего станка?", reply_markup=guitar_dir_kb())


@user_router.callback_query(F.data.startswith("guitar:dir"))
async def cb_guitar_dir(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    z_part = data.get("z_part")
    z_tool = data.get("z_tool")
    if not z_part or not z_tool:
        await state.clear()
        await call.answer("Сбились данные, начни заново", show_alert=True)
        return
    i = z_tool / z_part if call.data.endswith("1") else z_part / z_tool
    await state.clear()
    found = pick_gears(i)
    await call.message.answer(format_guitar_results(i, (z_part, z_tool), found),
                              reply_markup=guitar_menu_kb())
    await call.answer()


@user_router.callback_query(F.data == "guitar:cancel")
async def cb_guitar_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call.message, "🎛 Расчёт гитары отменён.", guitar_menu_kb())
    await call.answer()


@user_router.message(StateFilter(GuitarState.ratio, GuitarState.z_part, GuitarState.z_tool))
async def guitar_wrong(message: Message):
    await message.answer("Отправь число текстом, либо /start — в меню.")


@user_router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@user_router.message()
async def fallback(message: Message):
    await message.answer("🤷 Не понимаю. Воспользуйся кнопками меню.",
                         reply_markup=main_menu(is_admin(message.from_user.id)))


@user_router.callback_query()
async def cb_fallback(call: CallbackQuery):
    await call.answer()


# ==========================================================
# ХЕНДЛЕРЫ АДМИНА
# ==========================================================

@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к админ-панели.")
        return
    await state.clear()
    await message.answer(ADMIN_MENU_TEXT, reply_markup=admin_menu_kb())


@admin_router.callback_query(F.data == "adm:menu")
async def cb_admin_menu(call: CallbackQuery, state: FSMContext):
    if not await admin_ok(call):
        return
    await state.clear()
    await safe_edit(call.message, ADMIN_MENU_TEXT, admin_menu_kb())
    await call.answer()


@admin_router.callback_query(F.data == "adm:add")
async def cb_add_start(call: CallbackQuery, state: FSMContext):
    if not await admin_ok(call):
        return
    await state.set_state(AddDrawing.part_number)
    await call.message.edit_text(
        "➕ <b>Добавление чертежа</b>\n\n"
        "Шаг 1/2. Отправь номер детали текстом.\n"
        "Можно добавить название через |, например:\n"
        "<code>123-4567890-01 | Клапан Ду50</code>",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@admin_router.message(AddDrawing.part_number, F.text & ~F.text.startswith("/"))
async def add_step_part_number(message: Message, state: FSMContext):
    text = message.text.strip()
    if "|" in text:
        number, name = [t.strip() for t in text.split("|", 1)]
    else:
        number, name = text, ""
    if not number:
        await message.answer("Номер не должен быть пустым. Попробуй ещё раз:")
        return
    part_id = await get_or_create_part(number, name)
    await state.set_state(AddDrawing.files)
    await state.update_data(part_id=part_id, part_number=number)
    await message.answer(
        f"✅ Деталь <b>{html.escape(number)}</b> найдена/создана.\n\n"
        "Шаг 2/2. Теперь отправь файлы (PDF, JPG, DOCX и т.д.).\n"
        "Можно отправить несколько подряд — подпись к файлу\n"
        "станет его описанием.\n\n"
        "Когда закончишь — нажми «Готово».",
        reply_markup=add_files_kb(),
    )


@admin_router.message(AddDrawing.files, F.document | F.photo)
async def add_step_file(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    part_id = data.get("part_id")
    if part_id is None:
        await state.clear()
        await message.answer("Что-то пошло не так. Начни заново: /admin")
        return

    if message.document:
        file_id = message.document.file_id
        size = message.document.file_size or 0
        original_name = message.document.file_name or "файл"
    else:
        photo = message.photo[-1]
        file_id = photo.file_id
        size = photo.file_size or 0
        original_name = "фото.jpg"

    if size > MAX_DOWNLOAD_SIZE:
        await message.answer("⚠️ Файл больше 20 МБ — бот не может его сохранить "
                             "(лимит Telegram Bot API). Отправь файл поменьше.")
        return

    description = (message.caption or "").strip()

    try:
        tg_file = await bot.get_file(file_id)
        ext = os.path.splitext(tg_file.file_path)[1].lower() or (".jpg" if message.photo else ".bin")
        file_path = os.path.join(STORAGE_DIR, f"{uuid.uuid4().hex}{ext}")
        await bot.download_file(tg_file.file_path, destination=file_path)
    except Exception as e:  # noqa: BLE001
        await message.answer(f"⚠️ Не удалось скачать файл из Telegram: {e}")
        return

    await add_drawing(part_id, file_path, original_name, description, message.from_user.id)
    drawings = await get_drawings(part_id)
    await message.answer(
        f"✅ Сохранено! Файлов у этой детали: {len(drawings)}.\n\n"
        "Отправь следующий файл или нажми «Готово».",
        reply_markup=add_files_kb(),
    )


@admin_router.message(AddDrawing.files, F.text.startswith("/"))
async def add_files_slash(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено. Админ-меню: /admin",
                         reply_markup=main_menu(is_admin(message.from_user.id)))


@admin_router.message(AddDrawing.files)
async def add_files_wrong(message: Message):
    await message.answer("🤖 Отправь файл (документ или фото) либо нажми кнопку ниже.")


@admin_router.callback_query(F.data == "add:done")
async def cb_add_done(call: CallbackQuery, state: FSMContext):
    if not await admin_ok(call):
        return
    data = await state.get_data()
    await state.clear()
    number = data.get("part_number", "")
    text = f"🎉 Готово! Чертежи для «{html.escape(number)}» сохранены." if number else "🎉 Готово!"
    await call.message.edit_text(text, reply_markup=admin_menu_kb())
    await call.answer()


@admin_router.callback_query(F.data == "add:cancel")
async def cb_add_cancel(call: CallbackQuery, state: FSMContext):
    if not await admin_ok(call):
        return
    await state.clear()
    await call.message.edit_text(ADMIN_MENU_TEXT, reply_markup=admin_menu_kb())
    await call.answer("Отменено")


@admin_router.callback_query(F.data == "adm:del")
async def cb_del_start(call: CallbackQuery):
    if not await admin_ok(call):
        return
    parts = await get_parts_with_counts()
    if not parts:
        await call.message.edit_text("Каталог пуст — удалять нечего.", reply_markup=back_admin_kb())
        await call.answer()
        return
    kb, _ = parts_page_kb(parts, 0, "dpart", del_label, back=("⬅️ Назад в админ-панель", "adm:menu"))
    await call.message.edit_text("🗑 Выбери деталь:", reply_markup=kb)
    await call.answer()


@admin_router.callback_query(F.data.startswith("dpart:page:"))
async def cb_del_page(call: CallbackQuery):
    if not await admin_ok(call):
        return
    page = int(call.data.split(":")[2])
    parts = await get_parts_with_counts()
    kb, _ = parts_page_kb(parts, page, "dpart", del_label, back=("⬅️ Назад в админ-панель", "adm:menu"))
    await safe_edit(call.message, "🗑 Выбери деталь:", kb)
    await call.answer()


async def render_part_admin(message: Message, part_id: int):
    part = await get_part(part_id)
    if not part:
        await message.edit_text("Деталь не найдена.", reply_markup=back_admin_kb())
        return
    drawings = await get_drawings(part_id)
    text = (f"🗑 <b>{html.escape(part['part_number'])}</b>"
            + (f" — {html.escape(part['name'])}" if part["name"] else "")
            + "\n\nНажми ❌, чтобы удалить файл, либо удали деталь целиком.")
    if not drawings:
        text += "\n\n(файлов у детали нет)"
    await message.edit_text(text, reply_markup=part_files_admin_kb(drawings, part_id))


@admin_router.callback_query(F.data.startswith("dpart:"))
async def cb_del_part(call: CallbackQuery):
    if not await admin_ok(call):
        return
    part_id = int(call.data.split(":")[1])
    await render_part_admin(call.message, part_id)
    await call.answer()


@admin_router.callback_query(F.data.startswith("dfile:"))
async def cb_del_file(call: CallbackQuery):
    if not await admin_ok(call):
        return
    drawing_id = int(call.data.split(":")[1])
    drawing = await get_drawing(drawing_id)
    if not drawing:
        await call.answer("Файл уже удалён", show_alert=True)
        return
    path = await delete_drawing(drawing_id)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
    await call.answer("Файл удалён ✅")
    await render_part_admin(call.message, drawing["part_id"])


@admin_router.callback_query(F.data.startswith("dwhole:"))
async def cb_del_whole_ask(call: CallbackQuery):
    if not await admin_ok(call):
        return
    part_id = int(call.data.split(":")[1])
    part = await get_part(part_id)
    if not part:
        await call.answer("Деталь не найдена", show_alert=True)
        return
    await call.message.edit_text(
        f"⚠️ Точно удалить деталь <b>{html.escape(part['part_number'])}</b> "
        "и ВСЕ её файлы? Действие необратимо.",
        reply_markup=confirm_delete_part_kb(part_id),
    )
    await call.answer()


@admin_router.callback_query(F.data.startswith("dwy:"))
async def cb_del_whole_yes(call: CallbackQuery):
    if not await admin_ok(call):
        return
    part_id = int(call.data.split(":")[1])
    part = await get_part(part_id)
    paths = await delete_part(part_id)
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    number = html.escape(part["part_number"]) if part else str(part_id)
    await call.message.edit_text(f"🗑 Деталь <b>{number}</b> удалена.", reply_markup=back_admin_kb())
    await call.answer()


async def render_list(message: Message, page: int):
    parts = await get_parts_with_counts()
    if not parts:
        await message.edit_text("Каталог пуст.", reply_markup=back_admin_kb())
        return
    total = max(1, ceil(len(parts) / LIST_PER_PAGE))
    page = max(0, min(page, total - 1))
    start = page * LIST_PER_PAGE
    lines = []
    for i, row in enumerate(parts[start:start + LIST_PER_PAGE], start=start + 1):
        line = f"{i}. <b>{html.escape(row['part_number'])}</b>"
        if row["name"]:
            line += f" — {html.escape(row['name'])}"
        line += f" ({row['cnt']} файл.)"
        lines.append(line)
    text = f"📋 <b>Список деталей</b> (стр. {page + 1}/{total})\n\n" + "\n".join(lines)
    rows = []
    if total > 1:
        nav = []
        if page > 0:
            nav.append(btn("⬅️", f"alist:{page - 1}"))
        nav.append(btn(f"{page + 1}/{total}", "noop"))
        if page < total - 1:
            nav.append(btn("➡️", f"alist:{page + 1}"))
        rows.append(nav)
    rows.append([btn("⬅️ Назад в админ-панель", "adm:menu")])
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data == "adm:list")
async def cb_list(call: CallbackQuery):
    if not await admin_ok(call):
        return
    await render_list(call.message, 0)
    await call.answer()


@admin_router.callback_query(F.data.startswith("alist:"))
async def cb_list_page(call: CallbackQuery):
    if not await admin_ok(call):
        return
    page = int(call.data.split(":")[1])
    await render_list(call.message, page)
    await call.answer()


@admin_router.callback_query(F.data == "adm:stats")
async def cb_stats(call: CallbackQuery):
    if not await admin_ok(call):
        return
    s = await get_stats()
    total_size = 0
    for dirpath, _, filenames in os.walk(STORAGE_DIR):
        for name in filenames:
            try:
                total_size += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    text = ("📊 <b>Статистика</b>\n\n"
            f"Деталей в каталоге: {s['parts']}\n"
            f"Файлов сохранено: {s['files']}\n"
            f"Занято на диске: {total_size / 1024 / 1024:.1f} МБ")
    await call.message.edit_text(text, reply_markup=back_admin_kb())
    await call.answer()


# ==========================================================
# ЗАПУСК
# ==========================================================

async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(STORAGE_DIR, exist_ok=True)
    await init_db()

    bot = Bot(token=BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_routers(admin_router, user_router)

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен")