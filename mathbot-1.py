import asyncio, os, json, hmac, hashlib, time
from types import SimpleNamespace
import psycopg
from psycopg.rows import dict_row
from urllib.parse import parse_qsl
from pathlib import Path
import fitz
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, CallbackQuery, ChatJoinRequest,
                           InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()}
URL = os.environ["WEBAPP_URL"].rstrip("/")
CARD = os.getenv("CARD", "karta raqami")
PORT = int(os.getenv("PORT", "8080"))
DATA = Path("data"); DATA.mkdir(exist_ok=True)
ME = ""

DBURL = os.environ["DATABASE_URL"]
def connect(): return psycopg.connect(DBURL, autocommit=True, row_factory=dict_row)
conn = connect()
conn.execute("""
create table if not exists users(id bigint primary key, name text, ts bigint, premium int default 0);
create table if not exists tests(id serial primary key, title text, paid int, price int, pages int, key text);
create table if not exists purchases(id serial primary key, uid bigint, tid int, status text);
create table if not exists results(uid bigint, tid int, score int, total int, ts bigint);
create table if not exists channels(chat_id text primary key, link text, req int);
create table if not exists requests(uid bigint, chat_id text, primary key(uid, chat_id));
create table if not exists pages(tid int, n int, img bytea, primary key(tid, n));
""")

def _run(sql, a, mode):
    global conn
    s = sql.replace("?", "%s"); low = s.lstrip().lower()
    if low.startswith("insert or ignore"):
        s = s.replace("insert or ignore", "insert", 1) + " on conflict do nothing"
    elif low.startswith("insert into") and ("into tests" in low or "into purchases" in low):
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
            conn = connect()  # Neon uxlab qolgan ulanishni yangilaydi

def q(s, a=()): return _run(s, a, "all")
def q1(s, a=()): return _run(s, a, "one")
def x(s, a=()): return _run(s, a, None)

bot = Bot(TOKEN); dp = Dispatcher()
is_admin = F.from_user.id.in_(ADMINS)

class S(StatesGroup):
    title = State(); price = State(); key = State(); chan = State()
    prem = State(); bc = State(); check = State()

# ---------- majburiy obuna ----------
async def not_subbed(uid):
    out = []
    for c in q("select * from channels"):
        ok = False
        try:
            m = await bot.get_chat_member(c["chat_id"], uid)
            ok = m.status in ("member", "administrator", "creator")
        except Exception: pass
        if not ok and q1("select 1 from requests where uid=? and chat_id=?", (uid, c["chat_id"])):
            ok = True  # zayafka yuborgan (tasdiqlanmaydi)
        if not ok: out.append(c)
    return out

def sub_kb(bad):
    b = InlineKeyboardBuilder()
    for i, c in enumerate(bad, 1): b.button(text=f"📢 {i}-kanal", url=c["link"])
    b.button(text="✅ Tekshirish", callback_data="chk"); b.adjust(1)
    return b.as_markup()

def menu():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="📝 Testlarni ochish", web_app=WebAppInfo(url=URL + "/app"))]])

@dp.chat_join_request()
async def on_req(r: ChatJoinRequest):
    x("insert or ignore into requests values(?,?)", (r.from_user.id, str(r.chat.id)))  # tasdiqlamaymiz

@dp.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear(); uid = m.from_user.id
    x("insert or ignore into users(id,name,ts) values(?,?,?)", (uid, m.from_user.full_name, int(time.time())))
    bad = await not_subbed(uid)
    if bad:
        return await m.answer("Foydalanish uchun kanallarga obuna bo'ling (zayafka yuborish ham yetarli):", reply_markup=sub_kb(bad))
    a = command.args or ""
    if a.startswith("buy_"): return await buy_info(m, int(a[4:]), state)
    await m.answer("Salom! Testlarni ishlash uchun tugmani bosing 👇", reply_markup=menu())

@dp.callback_query(F.data == "chk")
async def chk(c: CallbackQuery):
    bad = await not_subbed(c.from_user.id)
    if bad: return await c.answer("Hali hamma kanalga obuna bo'lmadingiz", show_alert=True)
    await c.message.answer("✅ Rahmat! Testlarni ochish 👇", reply_markup=menu())

# ---------- sotib olish ----------
async def buy_info(m, tid, state):
    t = q1("select * from tests where id=?", (tid,))
    if not t: return await m.answer("Test topilmadi")
    await state.set_state(S.check); await state.update_data(tid=tid)
    await m.answer(f"💳 «{t['title']}» narxi: {t['price']:,} so'm\n\nKarta: {CARD}\n\n"
                   "To'lovni qiling va chekni *rasm* qilib shu yerga yuboring.", parse_mode="Markdown")

@dp.message(S.check, F.photo)
async def got_check(m: Message, state: FSMContext):
    tid = (await state.get_data())["tid"]; await state.clear()
    pid = x("insert into purchases(uid,tid,status) values(?,?,'pending')", (m.from_user.id, tid)).lastrowid
    t = q1("select title from tests where id=?", (tid,))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"ok:{pid}"),
        InlineKeyboardButton(text="❌ Bekor", callback_data=f"no:{pid}")]])
    for a in ADMINS:
        await bot.send_photo(a, m.photo[-1].file_id, reply_markup=kb,
            caption=f"Chek: {m.from_user.full_name} (id {m.from_user.id})\nTest: {t['title']}")
    await m.answer("Chek adminga yuborildi. Tasdiqlangach xabar keladi ⏳")

@dp.message(S.check)
async def need_photo(m: Message): await m.answer("Iltimos, chekni rasm qilib yuboring.")

@dp.callback_query(F.data.regexp(r"^(ok|no):"), is_admin)
async def decide(c: CallbackQuery):
    act, pid = c.data.split(":"); p = q1("select * from purchases where id=?", (pid,))
    if not p or p["status"] != "pending": return await c.answer("Allaqachon hal qilingan")
    x("update purchases set status=? where id=?", ("approved" if act == "ok" else "rejected", pid))
    await c.message.edit_caption(caption=(c.message.caption or "") + ("\n\n✅ TASDIQLANDI" if act == "ok" else "\n\n❌ BEKOR QILINDI"))
    await bot.send_message(p["uid"], "✅ To'lov tasdiqlandi! Testni ochishingiz mumkin." if act == "ok"
                           else "❌ To'lov tasdiqlanmadi. Chekni tekshirib qayta urinib ko'ring.", reply_markup=menu() if act == "ok" else None)
    await c.answer()

# ---------- admin panel ----------
def admin_kb():
    b = InlineKeyboardBuilder()
    for t, d in [("📢 Majburiy obuna", "a:sub"), ("📊 Statistika", "a:stat"), ("🗂 Testni boshqarish", "a:tests"),
                 ("⭐ Premium obunachilar", "a:prem"), ("📨 Xabar yuborish", "a:bc")]: b.button(text=t, callback_data=d)
    b.adjust(1); return b.as_markup()

@dp.message(Command("admin"), is_admin)
async def admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 Admin panel\n\nTest qo'shish uchun PDF faylni shu yerga yuboring.", reply_markup=admin_kb())

@dp.callback_query(F.data.startswith("a:"), is_admin)
async def a_menu(c: CallbackQuery, state: FSMContext):
    k = c.data[2:]; b = InlineKeyboardBuilder()
    if k == "stat":
        n = lambda s: list(q1(s).values())[0]
        await c.message.answer(f"👥 Foydalanuvchilar: {n('select count(*) from users')}\n⭐ Premium: {n('select count(*) from users where premium=1')}\n"
            f"📝 Testlar: {n('select count(*) from tests')}\n✅ Topshirilgan: {n('select count(*) from results')}\n"
            f"💰 Tasdiqlangan to'lovlar: {n("select count(*) from purchases where status='approved'")}\n"
            f"⏳ Kutilayotgan: {n("select count(*) from purchases where status='pending'")}\n"
            f"📢 Zayafkalar: {n('select count(*) from requests')}")
    elif k == "sub":
        txt = "📢 Kanallar:\n" + "\n".join(f"{r['chat_id']} · {'zayafka' if r['req'] else 'oddiy'}" for r in q("select * from channels"))
        for r in q("select * from channels"): b.button(text=f"🗑 {r['chat_id']}", callback_data=f"cd:{r['chat_id']}")
        b.button(text="➕ Kanal qo'shish", callback_data="ca"); b.adjust(1)
        await c.message.answer(txt, reply_markup=b.as_markup())
    elif k == "tests":
        for t in q("select * from tests"):
            b.button(text=f"{'💰' if t['paid'] else '🆓'} {t['title']}", callback_data=f"tt:{t['id']}")
            b.button(text="🗑", callback_data=f"td:{t['id']}")
        b.adjust(2); await c.message.answer("Testlar (nomini bossangiz tekin/pullik almashadi):" if q("select 1 from tests") else "Test yo'q. PDF yuboring.", reply_markup=b.as_markup())
    elif k == "prem":
        b.button(text="➕ Qo'shish", callback_data="pa"); b.button(text="➖ Olib tashlash", callback_data="pr"); b.adjust(2)
        ids = "\n".join(f"{r['id']} · {r['name']}" for r in q("select * from users where premium=1")) or "yo'q"
        await c.message.answer(f"⭐ Premium (pullik testlar tekin, pul so'ralmaydi):\n{ids}", reply_markup=b.as_markup())
    elif k == "bc":
        await state.set_state(S.bc); await c.message.answer("Hammaga yuboriladigan xabarni yuboring:")
    await c.answer()

@dp.callback_query(F.data == "ca", is_admin)
async def ca(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.chan)
    await c.message.answer("Format:\n`-1001234567890 | https://t.me/+havola | zayafka`\nyoki oxirida `oddiy`.\nBot kanalda admin bo'lishi shart.", parse_mode="Markdown")
    await c.answer()

@dp.message(S.chan, is_admin)
async def chan_in(m: Message, state: FSMContext):
    try:
        cid, link, kind = [s.strip() for s in m.text.split("|")]
        x("insert into channels values(?,?,?) on conflict (chat_id) do update set link=excluded.link, req=excluded.req", (cid, link, 1 if kind.lower().startswith("z") else 0))
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

@dp.callback_query(F.data.startswith(("tt:", "td:")), is_admin)
async def tmanage(c: CallbackQuery):
    a, i = c.data.split(":")
    if a == "tt":
        x("update tests set paid=1-paid where id=?", (i,))
        t = q1("select paid,price from tests where id=?", (i,))
        await c.answer("Pullik" if t["paid"] else "Tekin", show_alert=True)
    else:
        x("delete from tests where id=?", (i,)); x("delete from pages where tid=?", (i,)); await c.answer("O'chirildi"); await c.message.delete()

# ---------- test qo'shish ----------
@dp.message(F.document, is_admin)
async def new_pdf(m: Message, state: FSMContext):
    if m.document.mime_type != "application/pdf": return await m.answer("Faqat PDF yuboring")
    await state.set_state(S.title); await state.update_data(fid=m.document.file_id)
    await m.answer("Test nomini yozing (masalan: Milliy sertifikat · 04.12.2022):")

@dp.message(S.title, is_admin)
async def t_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🆓 Tekin", callback_data="k:0"),
                                                InlineKeyboardButton(text="💰 Pullik", callback_data="k:1")]])
    await m.answer("Bu test tekinmi yoki pullikmi?", reply_markup=kb)

@dp.callback_query(F.data.startswith("k:"), is_admin)
async def t_kind(c: CallbackQuery, state: FSMContext):
    paid = int(c.data[2:]); await state.update_data(paid=paid, price=0)
    if paid: await state.set_state(S.price); await c.message.answer("Narxini so'mda yozing (masalan 15000):")
    else: await ask_key(c.message, state)
    await c.answer()

@dp.message(S.price, is_admin)
async def t_price(m: Message, state: FSMContext):
    if not m.text.strip().isdigit(): return await m.answer("Faqat raqam")
    await state.update_data(price=int(m.text)); await ask_key(m, state)

async def ask_key(m, state):
    await state.set_state(S.key)
    await m.answer("Javoblar kalitini yuboring:\n1-qator: yopiq javoblar harflari (35 ta; 1-32 faqat A-D, 33-35 A-F)\n"
                   "keyingi qatorlar: yozma javoblar:\n`36: 2√3 | 32/3`\n`37: 3π | √13`\n... (a va b `|` bilan ajratiladi)", parse_mode="Markdown")

def parse_key(t):
    L = [l.strip() for l in t.strip().splitlines() if l.strip()]
    mc = L[0].upper().replace(" ", "")
    assert mc and all(c in ("ABCD" if i < 32 else "ABCDEF") for i, c in enumerate(mc))
    w = {}
    for l in L[1:]:
        n, v = l.split(":", 1); p = [s.strip() for s in v.split("|")]
        assert n.strip().isdigit() and len(p) == 2; w[n.strip()] = p
    return {"mc": mc, "w": w}

@dp.message(S.key, is_admin)
async def t_key(m: Message, state: FSMContext):
    try: key = parse_key(m.text)
    except Exception: return await m.answer("Format xato. Qayta yuboring.")
    d = await state.get_data(); await state.clear()
    tid = x("insert into tests(title,paid,price,pages,key) values(?,?,?,0,?)",
            (d["title"], d["paid"], d["price"], json.dumps(key, ensure_ascii=False))).lastrowid
    folder = DATA / f"t{tid}"; folder.mkdir(exist_ok=True)
    await bot.download(d["fid"], destination=folder / "src.pdf")
    def render():
        doc = fitz.open(folder / "src.pdf")
        for i, p in enumerate(doc):
            x("insert into pages(tid,n,img) values(?,?,?)", (tid, i, p.get_pixmap(dpi=110).tobytes("png")))
        return len(doc)
    n = await asyncio.to_thread(render)
    x("update tests set pages=? where id=?", (n, tid))
    await m.answer(f"✅ Test qo'shildi: {d['title']} ({'pullik' if d['paid'] else 'tekin'}, {n} bet)")

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
        out.append(dict(id=t["id"], title=t["title"], paid=t["paid"], price=t["price"], locked=not has_access(uid, t),
                        done=f"{res['score']}/{res['total']}" if res else None))
    return web.json_response({"bot": ME, "tests": out})

def get_test(r):
    uid = auth(r); t = q1("select * from tests where id=?", (r.match_info["id"],))
    if not t: raise web.HTTPNotFound()
    if not has_access(uid, t): raise web.HTTPForbidden()
    return uid, t

async def api_test(r):
    uid, t = get_test(r); k = json.loads(t["key"])
    return web.json_response(dict(title=t["title"], pages=t["pages"], mc=len(k["mc"]), w=sorted(k["w"], key=int)))

async def api_page(r):
    uid, t = get_test(r)
    row = q1("select img from pages where tid=? and n=?", (t["id"], int(r.match_info["n"])))
    if not row: raise web.HTTPNotFound()
    return web.Response(body=bytes(row["img"]), content_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

def norm(s):
    s = (s or "").lower().replace(" ", "").replace(",", ".").replace("−", "-")
    for a, b in [("π", "pi"), ("√", "sqrt"), ("∛", "cbrt"), ("²", "^2"), ("³", "^3")]: s = s.replace(a, b)
    return s

async def api_submit(r):
    uid, t = get_test(r); k = json.loads(t["key"]); body = await r.json(); res = {}; score = 0
    for i, ch in enumerate(k["mc"], 1):
        got = (body.get("mc") or {}).get(str(i)); ok = got == ch; score += ok
        res[str(i)] = dict(ok=ok, got=got, right=ch)
    for n, (a, b) in k["w"].items():
        for part, right in (("a", a), ("b", b)):
            got = (body.get("w") or {}).get(n + part, ""); ok = norm(got) == norm(right) and bool(got); score += ok
            res[n + part] = dict(ok=ok, got=got, right=right)
    total = len(k["mc"]) + 2 * len(k["w"])
    x("insert into results values(?,?,?,?,?)", (uid, t["id"], score, total, int(time.time())))
    return web.json_response(dict(score=score, total=total, res=res))

async def index(r): return web.FileResponse("WEBAPP/index.html")

async def main():
    global ME
    ME = (await bot.get_me()).username
    app = web.Application()
    app.add_routes([web.get("/app", index), web.get("/api/tests", api_tests), web.get("/api/test/{id}", api_test),
                    web.get("/api/test/{id}/page/{n}", api_page), web.post("/api/test/{id}/submit", api_submit)])
    run = web.AppRunner(app); await run.setup(); await web.TCPSite(run, "0.0.0.0", PORT).start()
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
