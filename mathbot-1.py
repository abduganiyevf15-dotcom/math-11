import asyncio, os, json, hmac, hashlib, time, html
from types import SimpleNamespace
from urllib.parse import parse_qsl
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
import fitz
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, ChatJoinRequest, InlineKeyboardMarkup, InlineKeyboardButton,
                           WebAppInfo, ReplyKeyboardMarkup, KeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()}
URL = os.environ["WEBAPP_URL"].rstrip("/")
CARD = os.getenv("CARD", "karta raqami")
PORT = int(os.getenv("PORT", "8080"))
EXAM = 150 * 60  # 2 soat 30 daqiqa
DATA = Path("data"); DATA.mkdir(exist_ok=True)
ME = ""

# ---------- baza (Neon) ----------
DBURL = os.environ["DATABASE_URL"]
def connect(): return psycopg.connect(DBURL, autocommit=True, row_factory=dict_row)
conn = connect()
conn.execute("""
create table if not exists users(id bigint primary key, name text, ts bigint, premium int default 0);
alter table users add column if not exists balance bigint default 0;
alter table users add column if not exists named int default 1;
create table if not exists tests(id serial primary key, title text, paid int, price int, pages int, key text);
alter table tests add column if not exists kind text default 'ms';
alter table tests add column if not exists minutes int default 150;
create table if not exists purchases(id serial primary key, uid bigint, tid int, status text);
create table if not exists results(uid bigint, tid int, score int, total int, ts bigint);
create table if not exists channels(chat_id text primary key, link text, req int);
create table if not exists requests(uid bigint, chat_id text, primary key(uid, chat_id));
create table if not exists pages(tid int, n int, img bytea, primary key(tid, n));
create table if not exists sessions(uid bigint, tid int, start bigint, answers text, done int default 0, primary key(uid, tid));
create table if not exists folders(id serial primary key, name text);
alter table tests add column if not exists folder_id int;
create table if not exists admins(uid bigint primary key, name text);
create table if not exists settings(key text primary key, value text);
create table if not exists topups(id serial primary key, uid bigint, amount bigint, status text);
""")

def _run(sql, a, mode):
    global conn
    s = sql.replace("?", "%s"); low = s.lstrip().lower()
    if low.startswith("insert or ignore"):
        s = s.replace("insert or ignore", "insert", 1) + " on conflict do nothing"
    elif low.startswith("insert into") and any(k in low for k in ("into tests", "into purchases", "into topups", "into folders")):
        s += " returning id"
    for t in (0, 1):
        try:
            c = conn.execute(s, a)
            if mode == "all": return c.fetchall()
            if mode == "one": return c.fetchone()
            r = SimpleNamespace(rowcount=c.rowcount, lastrowid=None)
            if s.endswith(" returning id"): r.lastrowid = c.fetchone()["id"]
            return r
        except (psycopg.OperationalError, psycopg.InterfaceError):
            if t: raise
            try: conn.close()
            except Exception: pass
            conn = connect()
def q(s, a=()): return _run(s, a, "all")
def q1(s, a=()): return _run(s, a, "one")
def x(s, a=()): return _run(s, a, None)

ENV_ADMINS = set(ADMINS)  # asosiy adminlar (Render dagi ADMIN_IDS)
for _r in q("select uid from admins"): ADMINS.add(_r["uid"])

bot = Bot(TOKEN); dp = Dispatcher()
is_admin = F.from_user.id.in_(ADMINS)
is_owner = F.from_user.id.in_(ENV_ADMINS)
esc = html.escape
def get_card():
    r = q1("select value from settings where key='card'")
    return r["value"] if r and r["value"] else CARD
def fmt(n): return f"{int(n or 0):,}".replace(",", " ")

class S(StatesGroup):
    title = State(); price = State(); key = State(); chan = State(); prem = State()
    bc = State(); check = State(); amount = State(); name = State()
    tprice = State(); tmin = State(); tname = State(); minutes = State(); regname = State(); card = State(); fnew = State(); frename = State(); confirm = State(); contact = State(); reply = State(); admadd = State()

# ---------- tugmalar ----------
def menu_kb(uid):
    rows = [[KeyboardButton(text="📝 Testlar")],
            [KeyboardButton(text="📊 Natijalarim"), KeyboardButton(text="🏆 Reyting")],
            [KeyboardButton(text="👤 Profil"), KeyboardButton(text="✉️ Murojaat")]]
    if uid in ADMINS: rows.append([KeyboardButton(text="🛠 Admin panel")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def admin_kb():
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text="📢 Majburiy obuna"), KeyboardButton(text="📊 Statistika")],
        [KeyboardButton(text="🗂 Testni boshqarish"), KeyboardButton(text="⭐ Premium")],
        [KeyboardButton(text="📨 Xabar yuborish"), KeyboardButton(text="💳 Karta raqami")], [KeyboardButton(text="📚 Kitoblar (papkalar)"), KeyboardButton(text="👮 Adminlar")], [KeyboardButton(text="🏠 Asosiy menyu")]])

def app_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="📝 Testlarni boshlash", web_app=WebAppInfo(url=URL + "/app"))]])

def reg(u):
    x("insert or ignore into users(id,name,ts,named) values(?,?,?,0)", (u.id, u.full_name, int(time.time())))

# ---------- majburiy obuna ----------
async def not_subbed(uid):
    out = []
    for c in q("select * from channels"):
        ok = False
        try:
            m = await bot.get_chat_member(c["chat_id"], uid)
            ok = m.status in ("member", "administrator", "creator")
        except Exception: pass
        if not ok and q1("select 1 from requests where uid=? and chat_id=?", (uid, c["chat_id"])): ok = True
        if not ok: out.append(c)
    return out

def sub_kb(bad):
    b = InlineKeyboardBuilder()
    for i, c in enumerate(bad, 1): b.button(text=f"📢 {i}-kanal", url=c["link"])
    b.button(text="✅ Tekshirish", callback_data="chk"); b.adjust(1)
    return b.as_markup()

@dp.chat_join_request()
async def on_req(r: ChatJoinRequest):
    x("insert or ignore into requests values(?,?)", (r.from_user.id, str(r.chat.id)))  # tasdiqlamaymiz

async def home(m, uid):
    u = q1("select name from users where id=?", (uid,))
    await m.answer("🏠 Asosiy menyu", reply_markup=menu_kb(uid))
    name = esc(u["name"] if u else "")
    await m.answer(f"👋 Assalomu alaykum, <b>{name}</b>!\n\n"
                   "Siz bu yerda Milliy Sertifikatda tushgan savollar va Matematikadan misollar ishlab "
                   "o'z darajangizni ko'tarishingiz mumkin!\n\n"
                   "Testni ishlang, javoblarni belgilang va «Yakunlash»ni bosing — natija va xatolar darhol chiqadi.",
                   parse_mode="HTML", reply_markup=app_kb())

async def need_name(m, uid, state):
    u = q1("select named from users where id=?", (uid,))
    if u and not u["named"]:
        await state.set_state(S.regname)
        await m.answer("👋 Assalomu alaykum! Botdan foydalanish uchun <b>ism va familiyangizni</b> yozing.\nMasalan: <b>Ali Valiyev</b>", parse_mode="HTML")
        return True
    return False

@dp.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear(); reg(m.from_user); uid = m.from_user.id
    bad = await not_subbed(uid)
    if bad:
        return await m.answer("Foydalanish uchun kanallarga obuna bo'ling (zayafka yuborish ham yetarli):", reply_markup=sub_kb(bad))
    if await need_name(m, uid, state): return
    a = command.args or ""
    if a == "topup" or a.startswith("buy_"):
        await m.answer("🏠", reply_markup=menu_kb(uid)); return await ask_amount(m, state)
    await home(m, uid)

@dp.message(S.regname)
async def regname_in(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    if len(t) < 3 or len(t) > 60 or len(t.split()) < 2 or not any(ch.isalpha() for ch in t):
        return await m.answer("Iltimos, ism va familiyangizni to'liq yozing.\nMasalan: Ali Valiyev")
    x("update users set name=?, named=1 where id=?", (t, m.from_user.id)); await state.clear()
    await home(m, m.from_user.id)

@dp.callback_query(F.data == "chk")
async def chk(c: CallbackQuery, state: FSMContext):
    if await not_subbed(c.from_user.id): return await c.answer("Hali hamma kanalga obuna bo'lmadingiz", show_alert=True)
    reg(c.from_user)
    if not await need_name(c.message, c.from_user.id, state): await home(c.message, c.from_user.id)
    await c.answer()

# ---------- pastdagi menyu tugmalari (holatni tozalaydi) ----------
@dp.message(F.text == "🏠 Asosiy menyu")
async def m_home(m: Message, state: FSMContext):
    await state.clear(); reg(m.from_user); await home(m, m.from_user.id)

@dp.message(F.text == "📝 Testlar")
async def m_tests(m: Message, state: FSMContext):
    await state.clear(); reg(m.from_user)
    bad = await not_subbed(m.from_user.id)
    if bad: return await m.answer("Avval kanallarga obuna bo'ling:", reply_markup=sub_kb(bad))
    await m.answer("Testlarni ochish uchun tugmani bosing 👇", reply_markup=app_kb())

@dp.message(F.text == "📊 Natijalarim")
async def m_results(m: Message, state: FSMContext):
    await state.clear()
    rows = q("select t.title, r.score, r.total, r.ts from results r join tests t on t.id=r.tid where r.uid=? order by r.ts desc limit 10", (m.from_user.id,))
    if not rows: return await m.answer("Siz hali test ishlamagansiz.")
    await m.answer("📊 <b>Oxirgi natijalaringiz</b>\n\n" + "\n".join(
        f"• {esc(r['title'])}: <b>{r['score']}/{r['total']}</b> · {time.strftime('%d.%m %H:%M', time.gmtime(r['ts'] + 18000))}" for r in rows), parse_mode="HTML")

@dp.message(F.text == "🏆 Reyting")
async def m_rating(m: Message, state: FSMContext):
    await state.clear()
    rows = q("select u.name, sum(b.best) as s from (select uid,tid,max(score) as best from results group by uid,tid) b "
             "join users u on u.id=b.uid group by u.id,u.name order by s desc limit 10")
    if not rows: return await m.answer("Reyting hozircha bo'sh.")
    med = ["🥇", "🥈", "🥉"]
    await m.answer("🏆 <b>Reyting (to'g'ri javoblar yig'indisi)</b>\n\n" + "\n".join(
        f"{med[i] if i < 3 else str(i + 1) + '.'} {esc(r['name'])} — <b>{r['s']}</b>" for i, r in enumerate(rows)), parse_mode="HTML")

async def profile(m, uid):
    u = q1("select * from users where id=?", (uid,))
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Ismni o'zgartirish", callback_data="pn"); b.button(text="💳 Hisobni to'ldirish", callback_data="pt"); b.adjust(1)
    await m.answer(f"👤 <b>Profil</b>\n\nIsm: {esc(u['name'])}\nID: <code>{uid}</code>\n"
                   f"💰 Hisob: <b>{fmt(u['balance'])}</b> so'm\n⭐ Premium: {'ha (pullik testlar tekin)' if u['premium'] else 'yo`q'}",
                   parse_mode="HTML", reply_markup=b.as_markup())

@dp.message(F.text == "👤 Profil")
async def m_profile(m: Message, state: FSMContext):
    await state.clear(); reg(m.from_user); await profile(m, m.from_user.id)

@dp.message(F.text == "✉️ Murojaat")
async def m_contact(m: Message, state: FSMContext):
    await state.set_state(S.contact)
    await m.answer("✍️ Adminga xabaringizni yozing (matn, rasm yoki fayl yuborishingiz mumkin).\nBekor qilish uchun pastdagi menyu tugmalaridan birini bosing.")

@dp.message(S.contact)
async def contact_in(m: Message, state: FSMContext):
    await state.clear(); u = m.from_user
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Javob berish", callback_data=f"rp:{u.id}")]])
    head = f"✉️ <b>Yangi murojaat</b>\n{esc(u.full_name)} · id <code>{u.id}</code>" + (f" · @{u.username}" if u.username else "")
    sent = 0
    for a in ADMINS:
        try:
            await bot.send_message(a, head, parse_mode="HTML")
            await m.copy_to(a, reply_markup=kb); sent += 1
        except Exception: pass
    await m.answer("✅ Xabaringiz adminga yuborildi. Javob shu bot orqali keladi." if sent else "Hozircha xabar yuborib bo'lmadi, keyinroq urinib ko'ring.")

@dp.callback_query(F.data == "pn")
async def pn(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.name); await c.message.answer("Yangi ismingizni yozing:"); await c.answer()

@dp.message(S.name)
async def name_in(m: Message, state: FSMContext):
    if not m.text or len(m.text) > 60: return await m.answer("Ism 60 belgidan oshmasin. Qayta yozing:")
    x("update users set name=?, named=1 where id=?", (m.text.strip(), m.from_user.id)); await state.clear()
    await m.answer("✅ Ism o'zgartirildi"); await profile(m, m.from_user.id)

# ---------- hisobni to'ldirish ----------
async def ask_amount(m, state):
    await state.set_state(S.amount)
    await m.answer("💳 Hisobni qancha so'mga to'ldirmoqchisiz?\nSummani raqam bilan yozing (masalan: 20000)")

@dp.callback_query(F.data == "pt")
async def pt(c: CallbackQuery, state: FSMContext):
    await ask_amount(c.message, state); await c.answer()

@dp.message(S.amount)
async def amount_in(m: Message, state: FSMContext):
    t = (m.text or "").replace(" ", "")
    if not t.isdigit() or int(t) < 1000: return await m.answer("Kamida 1000 so'm. Faqat raqam yozing:")
    await state.update_data(amount=int(t)); await state.set_state(S.check)
    await m.answer(f"💳 <b>{fmt(t)}</b> so'mni quyidagi kartaga o'tkazing:\n\n<code>{esc(get_card())}</code>\n\n"
                   "To'lovdan so'ng <b>chekni rasm qilib</b> shu yerga yuboring.", parse_mode="HTML")

@dp.message(S.check, F.photo)
async def got_check(m: Message, state: FSMContext):
    amount = (await state.get_data())["amount"]; await state.clear()
    pid = x("insert into topups(uid,amount,status) values(?,?,'pending')", (m.from_user.id, amount)).lastrowid
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"tok:{pid}"),
                                                InlineKeyboardButton(text="❌ Bekor", callback_data=f"tno:{pid}")]])
    for a in ADMINS:
        await bot.send_photo(a, m.photo[-1].file_id, reply_markup=kb,
            caption=f"💳 Hisob to'ldirish\n{m.from_user.full_name} (id {m.from_user.id})\nSumma: {fmt(amount)} so'm")
    await m.answer("Chek adminga yuborildi. Tasdiqlangach hisobingiz to'ldiriladi ⏳")

@dp.message(S.check)
async def need_photo(m: Message): await m.answer("Iltimos, chekni rasm qilib yuboring.")

@dp.callback_query(F.data.regexp(r"^t(ok|no):"), is_admin)
async def t_decide(c: CallbackQuery):
    act, tid = c.data.split(":"); ok = act == "tok"
    p = q1("select * from topups where id=?", (tid,))
    if not p: return await c.answer("Topilmadi")
    if not x("update topups set status=? where id=? and status='pending'", ("approved" if ok else "rejected", tid)).rowcount:
        return await c.answer("Allaqachon hal qilingan")
    if ok: x("update users set balance=balance+? where id=?", (p["amount"], p["uid"]))
    await c.message.edit_caption(caption=(c.message.caption or "") + ("\n\n✅ TASDIQLANDI" if ok else "\n\n❌ BEKOR QILINDI"))
    try:
        await bot.send_message(p["uid"], f"✅ Hisobingiz {fmt(p['amount'])} so'mga to'ldirildi." if ok
                               else "❌ To'lov tasdiqlanmadi. Chekni tekshirib qayta urinib ko'ring.")
    except Exception: pass
    await c.answer()

# ---------- admin panel ----------
@dp.message(Command("admin"), is_admin)
@dp.message(F.text == "🛠 Admin panel", is_admin)
async def admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 Admin panel\n\nTest qo'shish uchun PDF faylni shu yerga yuboring.", reply_markup=admin_kb())

ADM = {"📢 Majburiy obuna": "sub", "📊 Statistika": "stat", "🗂 Testni boshqarish": "tests",
       "⭐ Premium": "prem", "📨 Xabar yuborish": "bc", "💳 Karta raqami": "card", "📚 Kitoblar (papkalar)": "folders", "👮 Adminlar": "admins"}

@dp.message(F.text.in_(set(ADM)), is_admin)
async def adm_text(m: Message, state: FSMContext):
    await state.clear(); k = ADM[m.text]; b = InlineKeyboardBuilder()
    if k == "stat":
        n = lambda s: list(q1(s).values())[0]
        await m.answer(f"👥 Foydalanuvchilar: {n('select count(*) from users')}\n⭐ Premium: {n('select count(*) from users where premium=1')}\n"
            f"📝 Testlar: {n('select count(*) from tests')}\n✅ Topshirilgan: {n('select count(*) from results')}\n"
            f"💰 Foydalanuvchilar hisobi jami: {fmt(n('select coalesce(sum(balance),0) from users'))} so'm\n"
            f"⏳ Kutilayotgan to'lovlar: {n(chr(115)+'elect count(*) from topups where status='+chr(39)+'pending'+chr(39))}\n"
            f"📢 Zayafkalar: {n('select count(*) from requests')}")
    elif k == "sub":
        chs = q("select * from channels")
        txt = "📢 Kanallar:\n" + ("\n".join(f"{r['chat_id']} · {'zayafka' if r['req'] else 'oddiy'}" for r in chs) or "yo'q")
        for r in chs: b.button(text=f"🗑 {r['chat_id']}", callback_data=f"cd:{r['chat_id']}")
        b.button(text="➕ Kanal qo'shish", callback_data="ca"); b.adjust(1)
        await m.answer(txt, reply_markup=b.as_markup())
    elif k == "tests":
        await tests_list(m)
    elif k == "prem":
        b.button(text="➕ Qo'shish", callback_data="pa"); b.button(text="➖ Olib tashlash", callback_data="pr"); b.adjust(2)
        ids = "\n".join(f"{r['id']} · {r['name']}" for r in q("select * from users where premium=1")) or "yo'q"
        await m.answer(f"⭐ Premium (pullik testlar tekin, pul so'ralmaydi):\n{ids}", reply_markup=b.as_markup())
    elif k == "folders":
        await folders_list(m)
    elif k == "admins":
        await admins_list(m, m.from_user.id)
    elif k == "card":
        b.button(text="✏️ O'zgartirish", callback_data="cc")
        await m.answer(f"💳 Hozirgi karta (foydalanuvchilarga shu ko'rsatiladi):\n\n<code>{esc(get_card())}</code>", parse_mode="HTML", reply_markup=b.as_markup())
    elif k == "bc":
        await state.set_state(S.bc); await m.answer("Hammaga yuboriladigan xabarni yuboring:")

@dp.callback_query(F.data.startswith("rp:"), is_admin)
async def rp(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.reply); await state.update_data(to=int(c.data[3:]))
    await c.message.answer("↩️ Javobingizni yozing (matn, rasm yoki fayl):"); await c.answer()

@dp.message(S.reply, is_admin)
async def reply_in(m: Message, state: FSMContext):
    to = (await state.get_data())["to"]; await state.clear()
    try:
        await bot.send_message(to, "📩 <b>Admindan javob:</b>", parse_mode="HTML"); await m.copy_to(to)
        await m.answer("✅ Javob yuborildi")
    except Exception: await m.answer("❌ Yuborib bo'lmadi (foydalanuvchi botni bloklagan bo'lishi mumkin).")

def nm(uid):
    r = q1("select name from users where id=?", (uid,))
    return r["name"] if r else "—"

async def admins_list(m, uid):
    if uid not in ENV_ADMINS: return await m.answer("Bu bo'lim faqat asosiy admin uchun.")
    extra = q("select * from admins order by uid"); b = InlineKeyboardBuilder()
    txt = "👮 <b>Adminlar</b>\n\nAsosiy adminlar:\n" + "\n".join(f"• {esc(nm(i))} · <code>{i}</code>" for i in sorted(ENV_ADMINS))
    txt += "\n\nQo'shimcha adminlar:\n" + ("\n".join(f"• {esc(e['name'] or '')} · <code>{e['uid']}</code>" for e in extra) if extra else "yo'q")
    for e in extra: b.button(text=f"🗑 {e['name'] or e['uid']}", callback_data=f"ad:{e['uid']}")
    b.button(text="➕ Admin qo'shish", callback_data="aa"); b.adjust(1)
    await m.answer(txt, parse_mode="HTML", reply_markup=b.as_markup())

@dp.callback_query(F.data == "aa", is_owner)
async def aa(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.admadd)
    await c.message.answer("Yangi adminning Telegram ID raqamini yuboring.\n(U botga avval /start bosgan bo'lishi kerak. ID ni @userinfobot dan bilish mumkin.)"); await c.answer()

@dp.message(S.admadd, is_owner)
async def admadd_in(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    if not t.isdigit(): return await m.answer("Faqat ID raqamini yuboring.")
    uid = int(t)
    if uid in ADMINS:
        await state.clear(); return await m.answer("Bu foydalanuvchi allaqachon admin.")
    u = q1("select name from users where id=?", (uid,))
    if not u: return await m.answer("Bu ID bazada yo'q. U avval botga /start bosishi kerak.")
    x("insert into admins(uid,name) values(?,?) on conflict (uid) do nothing", (uid, u["name"])); ADMINS.add(uid); await state.clear()
    try: await bot.send_message(uid, "👮 Siz botda admin qilindingiz. /admin buyrug'ini yozing.")
    except Exception: pass
    await m.answer("✅ Admin qo'shildi"); await admins_list(m, m.from_user.id)

@dp.callback_query(F.data.startswith("ad:"), is_owner)
async def adm_del(c: CallbackQuery):
    uid = int(c.data[3:])
    x("delete from admins where uid=?", (uid,))
    if uid not in ENV_ADMINS: ADMINS.discard(uid)
    try: await bot.send_message(uid, "Sizning adminlik huquqingiz olib tashlandi.")
    except Exception: pass
    await c.message.delete(); await admins_list(c.message, c.from_user.id); await c.answer("O'chirildi")

@dp.callback_query(F.data == "cc", is_admin)
async def cc(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.card)
    await c.message.answer("Yangi karta raqami va egasining ismini yozing.\nMasalan: 8600 1234 5678 9012 Ali Valiyev\nBir nechta karta bo'lsa, har birini yangi qatorga yozing:")
    await c.answer()

@dp.message(S.card, is_admin)
async def card_in(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    if len(t) < 8 or len(t) > 300: return await m.answer("Karta ma'lumotini to'liq yozing.")
    x("insert into settings(key,value) values('card',?) on conflict (key) do update set value=excluded.value", (t,))
    await state.clear()
    await m.answer(f"✅ Saqlandi. Endi foydalanuvchilarga shu ko'rsatiladi:\n\n<code>{esc(t)}</code>", parse_mode="HTML")

@dp.callback_query(F.data == "ca", is_admin)
async def ca(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.chan)
    await c.message.answer("Format:\n`-1001234567890 | https://t.me/+havola | zayafka`\nyoki oxirida `oddiy`.\nBot kanalda admin bo'lishi shart.", parse_mode="Markdown")
    await c.answer()

@dp.message(S.chan, is_admin)
async def chan_in(m: Message, state: FSMContext):
    try:
        cid, link, kind = [s.strip() for s in m.text.split("|")]
        x("insert into channels values(?,?,?) on conflict (chat_id) do update set link=excluded.link, req=excluded.req",
          (cid, link, 1 if kind.lower().startswith("z") else 0))
        await state.clear(); await m.answer("✅ Qo'shildi")
    except Exception: await m.answer("Format xato, qayta yuboring.")

@dp.callback_query(F.data.startswith("cd:"), is_admin)
async def cd(c: CallbackQuery):
    x("delete from channels where chat_id=?", (c.data[3:],)); await c.answer("O'chirildi"); await c.message.delete()

@dp.callback_query(F.data.in_({"pa", "pr"}), is_admin)
async def pa(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.prem); await state.update_data(v=1 if c.data == "pa" else 0)
    await c.message.answer("Foydalanuvchi ID raqamini yuboring (botga /start bosgan bo'lishi kerak):"); await c.answer()

@dp.message(S.prem, is_admin)
async def prem_in(m: Message, state: FSMContext):
    v = (await state.get_data())["v"]
    if not m.text.strip().isdigit(): return await m.answer("Faqat raqam yuboring")
    n = x("update users set premium=? where id=?", (v, int(m.text))).rowcount
    await state.clear(); await m.answer("✅ Bajarildi" if n else "Bu ID bazada yo'q")

@dp.message(S.bc, is_admin)
async def bc(m: Message, state: FSMContext):
    await state.clear(); ok = 0
    for u in q("select id from users"):
        try: await m.copy_to(u["id"]); ok += 1
        except Exception: pass
        await asyncio.sleep(0.05)
    await m.answer(f"📨 {ok} kishiga yuborildi")

async def tests_list(m):
    ts = q("select * from tests order by id"); b = InlineKeyboardBuilder()
    for t in ts:
        b.button(text=f"{'💰' if t['paid'] else '🆓'} {'MS' if t['kind'] == 'ms' else 'Oddiy'} · {t['title']}", callback_data=f"tv:{t['id']}")
    b.adjust(1)
    await m.answer("🗂 Testni tanlang (ichida tahrirlash mumkin):" if ts else "Test yo'q. PDF yuboring.", reply_markup=b.as_markup())

def panel_text(t):
    kind = "Milliy sertifikat" if t["kind"] == "ms" else "Oddiy test (A-D)"
    fo = ""
    if t["kind"] == "od":
        f = q1("select name from folders where id=?", (t["folder_id"],)) if t["folder_id"] else None
        fo = f"\n📁 Papka: {esc(f['name']) if f else 'yo`q'}"
    st = ("💰 Pullik — " + fmt(t["price"]) + " so'm") if t["paid"] else "🆓 Tekin"
    return f"🗂 <b>{esc(t['title'])}</b>\n\nTuri: {kind}{fo}\nHolati: {st}\n⏱ Vaqt: {t['minutes']} daqiqa\n📄 Sahifalar: {t['pages']}"

def panel_kb(t):
    i = t["id"]; b = InlineKeyboardBuilder()
    b.button(text="🆓 Tekin qilish" if t["paid"] else "💰 Pullik qilish", callback_data=f"tp:{i}")
    if t["paid"]: b.button(text="💵 Narxni o'zgartirish", callback_data=f"tpr:{i}")
    b.button(text="⏱ Vaqtni o'zgartirish", callback_data=f"tm:{i}")
    b.button(text="✏️ Nomini o'zgartirish", callback_data=f"tn:{i}")
    if t["kind"] == "od": b.button(text="📁 Papkani o'zgartirish", callback_data=f"tfm:{i}")
    b.button(text="🗑 O'chirish", callback_data=f"td:{i}")
    b.button(text="⬅️ Orqaga", callback_data="tl"); b.adjust(1)
    return b.as_markup()

async def show_panel(m, tid, edit=False):
    t = q1("select * from tests where id=?", (tid,))
    if not t: return await m.answer("Test topilmadi")
    if edit: await m.edit_text(panel_text(t), parse_mode="HTML", reply_markup=panel_kb(t))
    else: await m.answer(panel_text(t), parse_mode="HTML", reply_markup=panel_kb(t))

@dp.callback_query(F.data == "tl", is_admin)
async def tl(c: CallbackQuery):
    await c.message.delete(); await tests_list(c.message); await c.answer()

@dp.callback_query(F.data.startswith("tv:"), is_admin)
async def tv(c: CallbackQuery):
    await show_panel(c.message, int(c.data[3:])); await c.answer()

@dp.callback_query(F.data.startswith(("tp:", "tpr:")), is_admin)
async def tp(c: CallbackQuery, state: FSMContext):
    a, i = c.data.split(":"); i = int(i); t = q1("select * from tests where id=?", (i,))
    if not t: return await c.answer("Topilmadi")
    if a == "tp" and t["paid"]:
        x("update tests set paid=0 where id=?", (i,)); await show_panel(c.message, i, edit=True)
    else:
        await state.set_state(S.tprice); await state.update_data(tid=i)
        await c.message.answer("Narxini so'mda yozing (masalan 15000):")
    await c.answer()

@dp.message(S.tprice, is_admin)
async def tprice_in(m: Message, state: FSMContext):
    tx = (m.text or "").replace(" ", "")
    if not tx.isdigit(): return await m.answer("Faqat raqam yozing")
    d = await state.get_data(); await state.clear()
    x("update tests set price=?, paid=1 where id=?", (int(tx), d["tid"])); await show_panel(m, d["tid"])

@dp.callback_query(F.data.startswith("tm:"), is_admin)
async def tm(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.tmin); await state.update_data(tid=int(c.data[3:]))
    await c.message.answer("Yangi vaqtni daqiqada yozing (masalan 150):"); await c.answer()

@dp.message(S.tmin, is_admin)
async def tmin_in(m: Message, state: FSMContext):
    tx = (m.text or "").strip()
    if not tx.isdigit() or not 1 <= int(tx) <= 600: return await m.answer("1 dan 600 gacha raqam yozing")
    d = await state.get_data(); await state.clear()
    x("update tests set minutes=? where id=?", (int(tx), d["tid"])); await show_panel(m, d["tid"])

@dp.callback_query(F.data.startswith("tn:"), is_admin)
async def tn(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.tname); await state.update_data(tid=int(c.data[3:]))
    await c.message.answer("Yangi nomni yozing:"); await c.answer()

@dp.message(S.tname, is_admin)
async def tname_in(m: Message, state: FSMContext):
    if not m.text: return await m.answer("Matn yozing")
    d = await state.get_data(); await state.clear()
    x("update tests set title=? where id=?", (m.text.strip(), d["tid"])); await show_panel(m, d["tid"])

@dp.callback_query(F.data.startswith("td:"), is_admin)
async def td(c: CallbackQuery):
    i = c.data[3:]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Ha, o'chirish", callback_data=f"tdy:{i}"),
                                                InlineKeyboardButton(text="❌ Yo'q", callback_data=f"tv:{i}")]])
    await c.message.answer("Testni o'chirishni tasdiqlaysizmi?", reply_markup=kb); await c.answer()

@dp.callback_query(F.data.startswith("tdy:"), is_admin)
async def tdy(c: CallbackQuery):
    i = c.data[4:]
    for sql in ("delete from tests where id=?", "delete from pages where tid=?", "delete from sessions where tid=?"): x(sql, (i,))
    await c.message.edit_text("🗑 Test o'chirildi"); await c.answer()

# ---------- kitoblar (papkalar) ----------
async def folders_list(m):
    fs = q("select f.*, (select count(*) from tests t where t.folder_id=f.id) as c from folders f order by f.id")
    b = InlineKeyboardBuilder()
    for f in fs: b.button(text=f"📁 {f['name']} · {f['c']} ta", callback_data=f"fv:{f['id']}")
    b.button(text="➕ Papka qo'shish", callback_data="fa"); b.adjust(1)
    await m.answer("📚 Kitoblar (papkalar). Oddiy testlar shu papkalar ichida ko'rinadi:" if fs else "Papka yo'q. Birinchisini qo'shing:", reply_markup=b.as_markup())

async def folder_panel(m, fid):
    f = q1("select f.*, (select count(*) from tests t where t.folder_id=f.id) as c from folders f where f.id=?", (fid,))
    if not f: return await m.answer("Papka topilmadi")
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Nomini o'zgartirish", callback_data=f"fr:{fid}"); b.button(text="🗑 O'chirish", callback_data=f"fdel:{fid}")
    b.button(text="⬅️ Orqaga", callback_data="fl"); b.adjust(1)
    await m.answer(f"📁 <b>{esc(f['name'])}</b>\nTestlar: {f['c']} ta", parse_mode="HTML", reply_markup=b.as_markup())

@dp.callback_query(F.data == "fl", is_admin)
async def fl(c: CallbackQuery):
    await c.message.delete(); await folders_list(c.message); await c.answer()

@dp.callback_query(F.data.startswith("fv:"), is_admin)
async def fv(c: CallbackQuery):
    await folder_panel(c.message, int(c.data[3:])); await c.answer()

@dp.callback_query(F.data == "fa", is_admin)
async def fa(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.fnew); await state.update_data(ctx="sec")
    await c.message.answer("Kitob (papka) nomini yozing:"); await c.answer()

@dp.message(S.fnew, is_admin)
async def fnew_in(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    if len(t) < 2 or len(t) > 80: return await m.answer("Nom 2–80 belgi bo'lsin. Qayta yozing:")
    fid = x("insert into folders(name) values(?)", (t,)).lastrowid
    d = await state.get_data()
    if d.get("ctx") == "flow":
        await state.update_data(folder_id=fid, ctx=None); await ask_paid(m)
    else:
        await state.clear(); await m.answer("✅ Papka qo'shildi"); await folders_list(m)

@dp.callback_query(F.data.startswith("fr:"), is_admin)
async def fr(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.frename); await state.update_data(fid=int(c.data[3:]))
    await c.message.answer("Yangi nomni yozing:"); await c.answer()

@dp.message(S.frename, is_admin)
async def frename_in(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    if len(t) < 2 or len(t) > 80: return await m.answer("Nom 2–80 belgi bo'lsin.")
    d = await state.get_data(); await state.clear()
    x("update folders set name=? where id=?", (t, d["fid"])); await folder_panel(m, d["fid"])

@dp.callback_query(F.data.startswith("fdel:"), is_admin)
async def fdel(c: CallbackQuery):
    i = int(c.data[5:])
    if q1("select count(*) as c from tests where folder_id=?", (i,))["c"]:
        return await c.answer("Papkada testlar bor. Avval ularni boshqa papkaga o'tkazing yoki o'chiring.", show_alert=True)
    x("delete from folders where id=?", (i,)); await c.message.edit_text("🗑 Papka o'chirildi"); await c.answer()

@dp.callback_query(F.data.startswith("tfm:"), is_admin)
async def tfm(c: CallbackQuery):
    i = c.data[4:]; b = InlineKeyboardBuilder()
    for f in q("select * from folders order by id"): b.button(text=f"📁 {f['name']}", callback_data=f"tfs:{i}:{f['id']}")
    b.adjust(1)
    await c.message.answer("Qaysi papkaga o'tkazamiz?" if q1("select 1 from folders") else "Avval 📚 Kitoblar bo'limida papka qo'shing.", reply_markup=b.as_markup()); await c.answer()

@dp.callback_query(F.data.startswith("tfs:"), is_admin)
async def tfs(c: CallbackQuery):
    _, tid, fid = c.data.split(":")
    x("update tests set folder_id=? where id=?", (int(fid), int(tid))); await show_panel(c.message, int(tid)); await c.answer("Ko'chirildi")

async def ask_paid(m):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🆓 Tekin", callback_data="k:0"),
                                                InlineKeyboardButton(text="💰 Pullik", callback_data="k:1")]])
    await m.answer("Bu test tekinmi yoki pullikmi?", reply_markup=kb)

async def ask_folder(m, state):
    fs = q("select * from folders order by id")
    if not fs:
        await state.set_state(S.fnew); await state.update_data(ctx="flow")
        return await m.answer("Hali papka yo'q. Test qaysi kitobdan ekanini yozing (yangi papka nomi):")
    b = InlineKeyboardBuilder()
    for f in fs: b.button(text=f"📁 {f['name']}", callback_data=f"kf:{f['id']}")
    b.button(text="➕ Yangi papka", callback_data="kfn"); b.adjust(1)
    await m.answer("Test qaysi kitob (papka) ichiga qo'shilsin?", reply_markup=b.as_markup())

@dp.callback_query(F.data.startswith("kf:"), is_admin)
async def kf(c: CallbackQuery, state: FSMContext):
    await state.update_data(folder_id=int(c.data[3:])); await ask_paid(c.message); await c.answer()

@dp.callback_query(F.data == "kfn", is_admin)
async def kfn(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.fnew); await state.update_data(ctx="flow")
    await c.message.answer("Yangi papka (kitob) nomini yozing:"); await c.answer()

# ---------- test qo'shish ----------
@dp.message(F.document, is_admin)
async def new_pdf(m: Message, state: FSMContext):
    if m.document.mime_type != "application/pdf": return await m.answer("Faqat PDF yuboring")
    await state.set_state(S.title); await state.update_data(fid=m.document.file_id)
    await m.answer("Test nomini yozing (masalan: Milliy sertifikat · 04.12.2022):")

@dp.message(S.title, is_admin)
async def t_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎓 Milliy sertifikat", callback_data="kd:ms")],
                                               [InlineKeyboardButton(text="📝 Oddiy test (faqat A-D)", callback_data="kd:od")]])
    await m.answer("Test turini tanlang:", reply_markup=kb)

@dp.callback_query(F.data.startswith("kd:"), is_admin)
async def t_kindsel(c: CallbackQuery, state: FSMContext):
    kind = c.data[3:]; await state.update_data(kind=kind, folder_id=None)
    if kind == "od": await ask_folder(c.message, state)
    else: await ask_paid(c.message)
    await c.answer()

@dp.callback_query(F.data.startswith("k:"), is_admin)
async def t_kind(c: CallbackQuery, state: FSMContext):
    paid = int(c.data[2:]); await state.update_data(paid=paid, price=0)
    if paid: await state.set_state(S.price); await c.message.answer("Narxini so'mda yozing (masalan 15000):")
    else: await ask_minutes(c.message, state)
    await c.answer()

@dp.message(S.price, is_admin)
async def t_price(m: Message, state: FSMContext):
    if not m.text.strip().isdigit(): return await m.answer("Faqat raqam")
    await state.update_data(price=int(m.text)); await ask_minutes(m, state)

async def ask_minutes(m, state):
    await state.set_state(S.minutes)
    await m.answer("⏱ Imtihon vaqtini daqiqada yozing (masalan 150 — 2 soat 30 daqiqa):")

@dp.message(S.minutes, is_admin)
async def t_minutes(m: Message, state: FSMContext):
    tx = (m.text or "").strip()
    if not tx.isdigit() or not 1 <= int(tx) <= 600: return await m.answer("1 dan 600 gacha raqam yozing")
    await state.update_data(minutes=int(tx)); await ask_key(m, state)

async def ask_key(m, state):
    d = await state.get_data(); await state.set_state(S.key)
    if d.get("kind") == "od":
        await m.answer("Javoblar kalitini yuboring: faqat A, B, C, D harflari, bir qatorda, bo'sh joysiz.\nMasalan: `ABCDABCDAB...`", parse_mode="Markdown")
    else:
        await m.answer("Javoblar kalitini yuboring:\n1-qator: yopiq javoblar harflari (35 ta; 1-32 faqat A-D, 33-35 A-F)\n"
                       "keyingi qatorlar: yozma javoblar:\n`36: 2√3 | 32/3`\n`37: 3π | √13`\n... (a va b `|` bilan ajratiladi)", parse_mode="Markdown")

def parse_key(t, kind="ms"):
    L = [l.strip() for l in t.strip().splitlines() if l.strip()]
    mc = L[0].upper().replace(" ", "")
    if kind == "od":
        assert mc and set(mc) <= set("ABCD"); return {"mc": mc, "w": {}}
    assert mc and all(c in ("ABCD" if i < 32 else "ABCDEF") for i, c in enumerate(mc))
    w = {}
    for l in L[1:]:
        n, v = l.split(":", 1); p = [s.strip() for s in v.split("|")]
        assert n.strip().isdigit() and len(p) == 2; w[n.strip()] = p
    return {"mc": mc, "w": w}

@dp.message(S.key, is_admin)
async def t_key(m: Message, state: FSMContext):
    d = await state.get_data(); kind = d.get("kind", "ms")
    try: key = parse_key(m.text, kind)
    except Exception: return await m.answer("Format xato. Qayta yuboring.")
    await state.update_data(key=key); await state.set_state(S.confirm)
    fo = ""
    if kind == "od":
        f = q1("select name from folders where id=?", (d.get("folder_id"),)) if d.get("folder_id") else None
        fo = f"📁 Papka: {esc(f['name']) if f else 'yo`q'}\n"
    st = f"💰 Pullik — {fmt(d['price'])} so'm" if d["paid"] else "🆓 Tekin"
    wr = f"\n✍️ Yozma savollar: {len(key['w'])} ta" if key["w"] else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Ha, qo'shish", callback_data="addok"),
                                                InlineKeyboardButton(text="❌ Bekor qilish", callback_data="addno")]])
    await m.answer(f"📝 <b>Test ma'lumotlari</b>\n\nNomi: {esc(d['title'])}\nTuri: {'Oddiy test (A-D)' if kind == 'od' else 'Milliy sertifikat'}\n{fo}"
                   f"Holati: {st}\n⏱ Vaqt: {d.get('minutes', 150)} daqiqa\n✅ Yopiq savollar: {len(key['mc'])} ta{wr}\n\n"
                   "<b>Testni qo'shishni tasdiqlaysizmi?</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "addno", S.confirm, is_admin)
async def add_no(c: CallbackQuery, state: FSMContext):
    await state.clear(); await c.message.edit_text("❌ Test qo'shish bekor qilindi."); await c.answer()

@dp.callback_query(F.data == "addok", S.confirm, is_admin)
async def add_ok(c: CallbackQuery, state: FSMContext):
    d = await state.get_data(); await state.clear(); kind = d.get("kind", "ms"); key = d["key"]
    await c.message.edit_text("⏳ Test qo'shilmoqda, biroz kuting..."); await c.answer()
    tid = x("insert into tests(title,paid,price,pages,key,kind,minutes,folder_id) values(?,?,?,0,?,?,?,?)",
            (d["title"], d["paid"], d["price"], json.dumps(key, ensure_ascii=False), kind, d.get("minutes", 150), d.get("folder_id"))).lastrowid
    folder = DATA / f"t{tid}"; folder.mkdir(exist_ok=True)
    await bot.download(d["fid"], destination=folder / "src.pdf")
    def render():
        doc = fitz.open(folder / "src.pdf")
        for i, p in enumerate(doc):
            x("insert into pages(tid,n,img) values(?,?,?)", (tid, i, p.get_pixmap(dpi=110).tobytes("png")))
        return len(doc)
    n = await asyncio.to_thread(render)
    x("update tests set pages=? where id=?", (n, tid))
    await c.message.answer(f"✅ Test qo'shildi: {d['title']} ({'Oddiy' if kind == 'od' else 'Milliy sertifikat'}, {'pullik' if d['paid'] else 'tekin'}, {n} bet, {d.get('minutes', 150)} daqiqa)")

# ---------- web API ----------
def auth(r):
    raw = r.headers.get("X-Init") or r.query.get("i", "")
    d = dict(parse_qsl(raw, keep_blank_values=True)); h = d.pop("hash", None)
    s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    k = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not h or not hmac.compare_digest(hmac.new(k, s.encode(), hashlib.sha256).hexdigest(), h):
        raise web.HTTPUnauthorized()
    return json.loads(d["user"])["id"]

def has_access(uid, t):
    if not t["paid"] or uid in ADMINS: return True
    u = q1("select premium from users where id=?", (uid,))
    if u and u["premium"]: return True
    return bool(q1("select 1 from purchases where uid=? and tid=? and status='approved'", (uid, t["id"])))

async def api_tests(r):
    uid = auth(r)
    if await not_subbed(uid): raise web.HTTPForbidden(text="sub")
    out = []
    for t in q("select * from tests order by id"):
        res = q1("select score,total from results where uid=? and tid=? order by ts desc", (uid, t["id"]))
        out.append(dict(id=t["id"], title=t["title"], paid=t["paid"], price=t["price"], kind=t["kind"], minutes=t["minutes"], folder_id=t["folder_id"] or 0, locked=not has_access(uid, t),
                        done=f"{res['score']}/{res['total']}" if res else None))
    u = q1("select balance from users where id=?", (uid,))
    folders = [dict(id=f["id"], name=f["name"], count=f["c"]) for f in q(
        "select f.id, f.name, (select count(*) from tests t where t.folder_id=f.id and t.kind='od') as c from folders f order by f.id") if f["c"]]
    n0 = q1("select count(*) as c from tests where kind='od' and folder_id is null")["c"]
    if n0: folders.append(dict(id=0, name="Boshqa testlar", count=n0))
    return web.json_response({"bot": ME, "balance": (u["balance"] if u else 0) or 0, "tests": out, "folders": folders})

def get_test(r):
    uid = auth(r)
    try: t = q1("select * from tests where id=?", (int(r.match_info["id"]),))
    except ValueError: t = None
    if not t: raise web.HTTPNotFound()
    if not has_access(uid, t): raise web.HTTPForbidden()
    return uid, t

async def api_test(r):
    uid, t = get_test(r); k = json.loads(t["key"])
    return web.json_response(dict(title=t["title"], pages=t["pages"], kind=t["kind"], mc=len(k["mc"]), w=sorted(k["w"], key=int)))

async def api_page(r):
    uid, t = get_test(r)
    row = q1("select img from pages where tid=? and n=?", (t["id"], int(r.match_info["n"])))
    if not row: raise web.HTTPNotFound()
    return web.Response(body=bytes(row["img"]), content_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

def norm(s):
    s = (s or "").lower().replace(" ", "").replace(",", ".").replace("−", "-")
    for a, b in [("π", "pi"), ("√", "sqrt"), ("∛", "cbrt"), ("²", "^2"), ("³", "^3")]: s = s.replace(a, b)
    return s

def grade(t, body):
    k = json.loads(t["key"]); res = {}; score = 0
    for i, ch in enumerate(k["mc"], 1):
        got = (body.get("mc") or {}).get(str(i)); ok = got == ch; score += ok
        res[str(i)] = dict(ok=ok, got=got, right=ch)
    for n, (a, b) in k["w"].items():
        for part, right in (("a", a), ("b", b)):
            got = (body.get("w") or {}).get(n + part, ""); ok = bool(got) and norm(got) == norm(right); score += ok
            res[n + part] = dict(ok=ok, got=got, right=right)
    return score, len(k["mc"]) + 2 * len(k["w"]), res

def finalize(uid, t, body):
    score, total, res = grade(t, body)
    x("insert into results values(?,?,?,?,?)", (uid, t["id"], score, total, int(time.time())))
    x("update sessions set done=1 where uid=? and tid=?", (uid, t["id"]))
    return score, total, res

async def api_start(r):
    uid, t = get_test(r); now = int(time.time())
    s = q1("select * from sessions where uid=? and tid=?", (uid, t["id"]))
    if s and s["done"]:
        x("delete from sessions where uid=? and tid=?", (uid, t["id"])); s = None
    if s and s["start"] + t["minutes"] * 60 <= now:
        finalize(uid, t, json.loads(s["answers"] or "{}")); return web.json_response({"expired": True})
    if not s:
        x("insert into sessions(uid,tid,start,answers,done) values(?,?,?,'{}',0)", (uid, t["id"], now))
        s = {"start": now, "answers": "{}"}
    return web.json_response(dict(left=s["start"] + t["minutes"] * 60 - now, answers=json.loads(s["answers"] or "{}")))

async def api_save(r):
    uid, t = get_test(r); body = await r.json()
    x("update sessions set answers=? where uid=? and tid=? and done=0", (json.dumps(body), uid, t["id"]))
    return web.json_response({"ok": True})

async def api_submit(r):
    uid, t = get_test(r); body = await r.json()
    score, total, res = finalize(uid, t, body)
    return web.json_response(dict(score=score, total=total, res=res))

async def api_buy(r):
    uid = auth(r)
    try: t = q1("select * from tests where id=?", (int(r.match_info["id"]),))
    except ValueError: t = None
    if not t: raise web.HTTPNotFound()
    if has_access(uid, t): return web.json_response({"ok": True})
    if not x("update users set balance=balance-? where id=? and balance>=?", (t["price"], uid, t["price"])).rowcount:
        u = q1("select balance from users where id=?", (uid,))
        return web.json_response({"ok": False, "balance": (u["balance"] if u else 0), "price": t["price"]}, status=402)
    x("insert into purchases(uid,tid,status) values(?,?,'approved')", (uid, t["id"]))
    return web.json_response({"ok": True})

async def index(r):
    dirs = [Path(".")] + [p for p in Path(".").iterdir() if p.is_dir() and p.name.lower() == "webapp"]
    for d in dirs:
        for f in d.iterdir():
            if f.is_file() and f.name.lower() == "index.html": return web.FileResponse(f)
    raise web.HTTPNotFound(text="index.html topilmadi. Repoda bor fayllar: " + ", ".join(sorted(os.listdir("."))))

async def watcher():  # vaqti tugagan urinishlarni avtomatik tekshiradi
    while True:
        await asyncio.sleep(60)
        try:
            for s in q("select s.* from sessions s join tests t on t.id=s.tid where s.done=0 and s.start+t.minutes*60<=?", (int(time.time()),)):
                t = q1("select * from tests where id=?", (s["tid"],))
                if not t: x("delete from sessions where uid=? and tid=?", (s["uid"], s["tid"])); continue
                sc, tot, _ = finalize(s["uid"], t, json.loads(s["answers"] or "{}"))
                try: await bot.send_message(s["uid"], f"⏰ Vaqt tugadi! «{t['title']}» natijangiz: {sc}/{tot}")
                except Exception: pass
        except Exception as e: print("watcher:", e)

async def main():
    global ME
    ME = (await bot.get_me()).username
    app = web.Application()
    app.add_routes([web.get("/app", index), web.get("/api/tests", api_tests), web.get("/api/test/{id}", api_test),
                    web.get("/api/test/{id}/page/{n}", api_page), web.post("/api/test/{id}/submit", api_submit),
                    web.post("/api/test/{id}/start", api_start), web.post("/api/test/{id}/save", api_save),
                    web.post("/api/test/{id}/buy", api_buy)])
    run = web.AppRunner(app); await run.setup(); await web.TCPSite(run, "0.0.0.0", PORT).start()
    asyncio.create_task(watcher())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
