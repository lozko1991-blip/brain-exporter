#!/usr/bin/env python3
"""
Brain API → XML Exporter v4.0
─────────────────────────────────────────────────────────────────
Підтримує КІЛЬКА фідів (feeds.json) — по одному XML на маркетплейс,
кожен зі своїми категоріями та своєю націнкою (% + грн).

Режими:
  full  — повна вигрузка: товари + характеристики + фото + повний опис
  quick — тільки ціни і наявність (швидко), бере дані з кешу

Запуск:
  EXPORT_MODE=full  python export.py
  EXPORT_MODE=quick python export.py

Що змінилось проти v3:
  • Кілька фідів з feeds.json (fallback на старий config.json)
  • Націнка задається per-feed (percent + fixed), а не зашита в коді
  • ВИПРАВЛЕНО опис: тепер качаємо повний `description` через /product/
  • ВИПРАВЛЕНО наявність: рахуємо реальні залишки зі `stocks`
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from xml.dom.minidom import parseString
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx

# ══════════════════════════════════════════════════════════════════
API_BASE   = "http://api.brain.com.ua"
OUTPUT_DIR = Path("output")
CACHE_FILE = Path("products_cache.json")
CATS_FILE  = Path("categories.json")
FEEDS_FILE = Path("feeds.json")
KASTA_COLORS_FILE = Path("kasta_colors.json")
KASTA_CHARS_FILE = Path("kasta_characteristics.json")
GENDER_PINS_FILE = Path("gender_pins.json")  # Закріплення статі товарів (стабільні категорії)
OUTPUT_DIR.mkdir(exist_ok=True)

# ══════════════════════════════════════════════════════════════════
#  ПАРАЛЕЛЬНІ SHARD-ДЖОБИ + СТІЙКІСТЬ ДО ЗБОЇВ BRAIN
# ──────────────────────────────────────────────────────────────────
# Документація Brain НЕ описує rate-ліміт / термін сесії / кількість
# одночасних сесій. Тому проєктуємо захищено:
#   • EXPORT_STAGE=setup  — один auth + категорії (передає SID далі)
#   • EXPORT_STAGE=shard  — качає СВОЮ частину категорій (свій кеш-файл)
#   • EXPORT_STAGE=merge  — збирає частини в один кеш і будує XML
#   • EXPORT_STAGE=solo   — усе в одному процесі (локально / малий каталог)
# SID береться з BRAIN_SID (спільний на всі shard-и) — Brain бачить ОДНУ
# сесію + паралельні читання, що для нього найбезпечніше. Якщо SID відхилено
# — shard сам перелогіниться (BRAIN_LOGIN/BRAIN_PASSWORD мають бути в env).
EXPORT_STAGE = (os.environ.get("EXPORT_STAGE") or "solo").strip().lower()
SHARD_INDEX  = int(os.environ.get("SHARD_INDEX", "0") or 0)
SHARD_TOTAL  = max(1, int(os.environ.get("SHARD_TOTAL", "1") or 1))
# Бюджет часу (хв) на качання у shard/solo. Коли вичерпано — процес
# КОРЕКТНО зупиняє завантаження, зберігає що встиг і виходить успішно
# (0 = без ліміту). Це гарантує, що таймаут джоба не втратить прогрес.
TIME_BUDGET_MIN = float(os.environ.get("TIME_BUDGET_MIN", "0") or 0)

def shard_cache_file(idx: int) -> Path:
    return Path(f"products_cache.shard{idx}.json")

# Префікс для ID товарів і категорій у XML — щоб уникнути колізій
# з товарами інших постачальників на маркетплейсі. "br" = Brain.
ID_PREFIX = "br"

def pid_ext(pid) -> str:
    """ID товару для XML: br12345"""
    return f"{ID_PREFIX}{pid}"

def cid_ext(cid) -> str:
    """ID категорії для XML: br1181"""
    return f"{ID_PREFIX}{cid}"


# ══════════════════════════════════════════════════════════════════
#  ЛОГУВАННЯ
# ══════════════════════════════════════════════════════════════════

def log(msg: str):
    import sys
    text = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc), flush=True)


# ══════════════════════════════════════════════════════════════════
#  КОНФІГ + ФІДИ
# ══════════════════════════════════════════════════════════════════

def load_base_config() -> dict:
    """Спільні налаштування: логін, мова, режим, назва магазину."""
    cfg = {}
    if Path("config.json").exists():
        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))

    if os.environ.get("BRAIN_LOGIN"):    cfg["login"]     = os.environ["BRAIN_LOGIN"]
    if os.environ.get("BRAIN_PASSWORD"): cfg["password"]  = os.environ["BRAIN_PASSWORD"]
    if os.environ.get("LANG"):           cfg["lang"]      = os.environ["LANG"]
    if os.environ.get("SHOP_NAME"):      cfg["shop_name"] = os.environ["SHOP_NAME"]
    if os.environ.get("SHOP_URL"):       cfg["shop_url"]  = os.environ["SHOP_URL"]
    if os.environ.get("EXPORT_MODE"):    cfg["mode"]      = os.environ["EXPORT_MODE"]

    cfg.setdefault("lang",      "ua")
    cfg.setdefault("shop_name", "Мій магазин")
    cfg.setdefault("shop_url",  "https://example.com.ua")
    cfg.setdefault("mode",      "quick")
    return cfg


def load_feeds(base: dict) -> list:
    """
    Повертає список фідів. Кожен фід:
      {
        "id": "rozetka",                 # → output/rozetka.xml
        "name": "Rozetka",               # назва магазину в XML
        "category_ids": [1181, 1191],    # вибрані категорії
        "markup_percent": 20,            # +20%
        "markup_fixed": 50,              # +50 грн
        "lang": "ua"                     # (опц.) перевизначає базову мову
      }
    Якщо feeds.json немає — будуємо один фід зі старого config.json.
    """
    if FEEDS_FILE.exists():
        data = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))
        feeds = data.get("feeds", data) if isinstance(data, dict) else data

        # shop_url може лежати у feeds.json — він має пріоритет над config.json,
        # але НЕ над змінною оточення SHOP_URL (її вже застосовано в base).
        if isinstance(data, dict) and data.get("shop_url") and not os.environ.get("SHOP_URL"):
            base["shop_url"] = data["shop_url"]

        out = []
        seen_ids: dict[str, int] = {}   # для захисту від дублікатів id
        for f in feeds:
            raw_id = str(f.get("id") or f.get("name") or "feed").strip() or "feed"

            # ── ЗАХИСТ ВІД ДУБЛІКАТІВ ID ──
            # Два фіди з однаковим id писали б у той самий файл і один
            # мовчки затирав би інший. Робимо id унікальним: rozetka, rozetka-2...
            fid = raw_id
            if fid in seen_ids:
                seen_ids[fid] += 1
                fid = f"{raw_id}-{seen_ids[raw_id]}"
                log(f"⚠️  Дубль id '{raw_id}' — перейменовано на '{fid}', "
                    f"щоб не затерти {raw_id}.xml. Виправ id у feeds.json!")
            else:
                seen_ids[fid] = 1

            cat_ids = [int(x) for x in f.get("category_ids", [])]
            if not cat_ids:
                log(f"⚠️  Фід '{fid}' не має жодної категорії — XML буде порожнім.")

            out.append({
                "id":             fid,
                "name":           f.get("name") or base["shop_name"],
                "category_ids":   cat_ids,
                "markup_percent": float(f.get("markup_percent", 0) or 0),
                "markup_fixed":   float(f.get("markup_fixed", 0) or 0),
                "lang":           f.get("lang") or base["lang"],
                # формат фіда: "kasta" → KASTA-білдер, інакше Rozetka/Prom YML
                "format":         str(f.get("format", "yml")).strip().lower(),
                # батьківські категорії, де робити розбивку за статтю (тільки KASTA).
                # порожньо → розбивки немає взагалі.
                "split_category_ids": [int(x) for x in f.get("split_category_ids", [])],
                # префікси ID (щоб не було колізій з іншими постачальниками на маркетплейсі)
                "prefix_offer":    str(f.get("prefix_offer")    or "br").strip() or "br",
                "prefix_category": str(f.get("prefix_category") or "br").strip() or "br",
            })
        log(f"📑 feeds.json: {len(out)} фід(ів)")
        return out

    # ── Fallback: старий config.json як один фід ──
    cfg = {}
    if Path("config.json").exists():
        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    log("📑 feeds.json не знайдено — використовую config.json як один фід")
    return [{
        "id":             "catalog",
        "name":           base["shop_name"],
        "category_ids":   [int(x) for x in cfg.get("category_ids", [])],
        "markup_percent": float(cfg.get("markup_percent", 0) or 0),
        "markup_fixed":   float(cfg.get("markup_fixed", 0) or 0),
        "lang":           base["lang"],
        "format":         "yml",
        "split_category_ids": [],
        "prefix_offer":    "br",
        "prefix_category": "br",
    }]


# ══════════════════════════════════════════════════════════════════
#  АВТОРИЗАЦІЯ
# ══════════════════════════════════════════════════════════════════

async def auth(client: httpx.AsyncClient, login: str, password: str) -> str:
    md5_pass = hashlib.md5(password.encode("utf-8")).hexdigest()
    log(f"🔐 Авторизація: {login}")
    # кілька спроб — Brain інколи відповідає 5xx/таймаутом на сплеск запитів
    last = None
    for attempt in range(4):
        try:
            resp = await client.post(
                f"{API_BASE}/auth",
                data={"login": login, "password": md5_pass},
                timeout=20,
            )
            data = resp.json()
            if data.get("status") == 1 and data.get("result"):
                log(f"✅ Авторизовано, SID: {data['result'][:8]}...")
                return data["result"]
            last = data
        except Exception as e:
            last = e
        await asyncio.sleep(2 ** attempt)   # 1,2,4,8 c
    raise Exception(f"❌ Помилка авторизації після 4 спроб: {last}")


# Поточний робочий SID (може оновитись автоперелогіном). Глобал, щоб усі
# fetch-функції бачили свіжий токен без передавання крізь усі виклики.
# last_ts/count — захист від КАСКАДУ перелогінів: якщо Brain single-session
# і вбиває старий SID на новий auth, shard-и могли б нескінченно
# перелогінювати одне одного. Тротлимо й після ліміту зупиняємось коректно.
_SID_STATE = {"sid": "", "login": "", "password": "", "reauth_ts": 0.0, "reauth_count": 0}
REAUTH_MIN_INTERVAL = 30      # не частіше, ніж раз на 30 с
REAUTH_MAX_TOTAL    = 12      # після стількох перелогінів — стоп (не дратуємо Brain)

class StopFetching(Exception):
    """Сигнал коректно зупинити завантаження (зберегти прогрес, не падати)."""

def _looks_like_dead_session(data: dict) -> bool:
    """Чи відповідь Brain означає «сесія недійсна» (треба перелогінитись)."""
    if not isinstance(data, dict):
        return False
    if data.get("status") == 1:
        return False
    txt = json.dumps(data, ensure_ascii=False).lower()
    return any(k in txt for k in ("session", "сесі", "auth", "sid", "unauthor", "token"))

async def ensure_sid(client: httpx.AsyncClient) -> str:
    """Повертає робочий SID; за потреби перелогінюється (для shard-ів зі спільним SID)."""
    if _SID_STATE["sid"]:
        return _SID_STATE["sid"]
    _SID_STATE["sid"] = await auth(client, _SID_STATE["login"], _SID_STATE["password"])
    return _SID_STATE["sid"]

async def reauth(client: httpx.AsyncClient) -> str:
    """
    Примусовий перелогін (коли поточний SID відхилено Brain), з тротлінгом:
      • не частіше REAUTH_MIN_INTERVAL — інакше чекаємо (щоб не «бомбити» auth);
      • не більше REAUTH_MAX_TOTAL разів — інакше зупиняємось КОРЕКТНО
        (StopFetching), бо це ознака проблеми з сесіями і подальші спроби лише
        ризикують блокуванням акаунта. Прогрес уже збережено чекпойнтами.
    """
    _SID_STATE["reauth_count"] += 1
    if _SID_STATE["reauth_count"] > REAUTH_MAX_TOTAL:
        raise StopFetching(
            f"забагато перелогінів ({_SID_STATE['reauth_count']}) — "
            f"схоже, Brain не тримає паралельні/довгі сесії. Зупиняюсь, "
            f"щоб не отримати блокування; прогрес збережено."
        )
    gap = time.monotonic() - _SID_STATE["reauth_ts"]
    if gap < REAUTH_MIN_INTERVAL:
        await asyncio.sleep(REAUTH_MIN_INTERVAL - gap)
    _SID_STATE["reauth_ts"] = time.monotonic()
    _SID_STATE["sid"] = ""
    log("♻️  SID відхилено Brain — перелогінююсь...")
    return await ensure_sid(client)


async def logout(client: httpx.AsyncClient, sid: str):
    try:
        await client.get(f"{API_BASE}/logout/{sid}", timeout=10)
        log("👋 Сесію закрито")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
#  КАТЕГОРІЇ
# ══════════════════════════════════════════════════════════════════

async def fetch_categories(client: httpx.AsyncClient, sid: str, lang: str) -> list:
    log("📂 Завантаження категорій...")
    for attempt in range(4):
        try:
            current_sid = _SID_STATE["sid"] or sid
            resp = await client.get(f"{API_BASE}/categories/{current_sid}?lang={lang}", timeout=20)
            data = resp.json()
            if data.get("status") == 1:
                cats = data.get("result", [])
                log(f"   Знайдено: {len(cats)} категорій")
                return cats
            if _looks_like_dead_session(data):
                sid = await reauth(client)
                continue
            log(f"   ⚠️ Неуспішна відповідь категорій (спроба {attempt+1}): {data}")
        except StopFetching:
            raise
        except Exception as e:
            if attempt == 3:
                log(f"   ❌ Помилка завантаження категорій: {e}")
        await asyncio.sleep(1.5 ** attempt)
    return []


def load_categories_flat() -> list:
    """
    Плоский список категорій {categoryID, parentID, name} з categories.json.
    Потрібен merge-стадії, яка будує XML, але сама не качає категорії з API.
    """
    if not CATS_FILE.exists():
        return []
    data = json.loads(CATS_FILE.read_text(encoding="utf-8"))
    flat = []
    stack = list(data.get("categories", []))
    while stack:
        node = stack.pop()
        flat.append({
            "categoryID": node["categoryID"],
            "parentID":   node.get("parentID", 1),
            "name":       node.get("name", ""),
        })
        stack.extend(node.get("children", []))
    log(f"📂 categories.json прочитано: {len(flat)} категорій (для merge)")
    return flat


def get_all_descendants(cats_by_parent: dict, parent_id: int) -> set:
    """Усі нащадки категорії (включно з нею). Ітеративно — без рекурсії."""
    result = set()
    stack = [parent_id]
    while stack:
        cur = stack.pop()
        if cur in result:
            continue
        result.add(cur)
        stack.extend(cats_by_parent.get(cur, []))
    return result


def save_categories_json(all_cats: list):
    """Дерево категорій для адмінки (index.html)."""
    cat_map = {c["categoryID"]: {
        "categoryID": c["categoryID"],
        "parentID":   c["parentID"],
        "name":       c["name"],
        "children":   [],
    } for c in all_cats}

    roots = []
    for c in all_cats:
        node = cat_map[c["categoryID"]]
        pid  = c.get("parentID", 1)
        if pid == 1 or pid not in cat_map:
            roots.append(node)
        else:
            cat_map[pid]["children"].append(node)

    CATS_FILE.write_text(
        json.dumps({
            "generated":  datetime.now().strftime("%Y-%m-%d %H:%M"),
            "total":      len(all_cats),
            "categories": roots,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log(f"📂 categories.json збережено ({len(all_cats)} категорій)")


# ══════════════════════════════════════════════════════════════════
#  ЗАВАНТАЖЕННЯ ТОВАРІВ
# ══════════════════════════════════════════════════════════════════

async def fetch_products_page(
    client: httpx.AsyncClient, sid: str, cat_id: int,
    lang: str, offset: int, limit: int = 100
) -> list:
    for attempt in range(4):
        try:
            current_sid = _SID_STATE["sid"] or sid
            resp = await client.get(
                f"{API_BASE}/products/{cat_id}/{current_sid}?lang={lang}&limit={limit}&offset={offset}",
                timeout=30,
            )
            data = resp.json()
            if data.get("status") == 1:
                result = data["result"]
                if isinstance(result, list):
                    return result
                if isinstance(result, dict):
                    return result.get("list") or result.get("products") or []
            # сесія померла → перелогін і повтор із новим SID
            if _looks_like_dead_session(data):
                sid = await reauth(client)
                continue
        except StopFetching:
            raise   # сигнал коректної зупинки — не ковтаємо
        except Exception as e:
            if attempt == 3:
                log(f"   ⚠️ Кат.{cat_id} offset={offset}: {e}")
        await asyncio.sleep(1.5 ** attempt)
    return []


async def fetch_product_full(client: httpx.AsyncClient, sid: str, pid: int) -> dict:
    """
    Повна картка товару. Дає те, чого немає у списку /products:
    повний `description`, `koduktved`, а також `options` (характеристики).
    Завжди запитуємо `lang=ua_ru` — за ОДИН запит отримуємо обидві мови:
      name / description / country / options (укр)
      + name_ru / description_ru / country_ru / options_ru (рос).
    Це дозволяє заповнити для KASTA і name_ua, і name_ru, незалежно від
    того, яку мову вибрано в Action (вона впливає лише на дерево категорій
    і на Фазу 1 — обидві перекриваються цією двомовною Фазою 2).
    """
    for attempt in range(3):
        try:
            current_sid = _SID_STATE["sid"] or sid
            r = await client.get(f"{API_BASE}/product/{pid}/{current_sid}?lang=ua_ru", timeout=20)
            d = r.json()
            if d.get("status") == 1 and isinstance(d.get("result"), dict):
                return d["result"]
            if _looks_like_dead_session(d):
                sid = await reauth(client)
                continue
            # Якщо status != 1, але сесія жива — це може бути тимчасовий збій API (rate limit / error)
            # Чекаємо і повторюємо
        except StopFetching:
            raise
        except Exception:
            pass
        await asyncio.sleep(1.5 ** attempt)
    return {}


async def fetch_pictures(client: httpx.AsyncClient, sid: str, pid: int) -> list:
    for attempt in range(3):
        try:
            current_sid = _SID_STATE["sid"] or sid
            r = await client.get(f"{API_BASE}/product_pictures/{pid}/{current_sid}", timeout=15)
            d = r.json()
            if d.get("status") == 1:
                result = d.get("result", [])
                if isinstance(result, list):
                    return result
                if isinstance(result, dict):
                    return result.get("pictures", [])
            if _looks_like_dead_session(d):
                sid = await reauth(client)
                continue
            # Тимчасовий збій
        except StopFetching:
            raise
        except Exception:
            pass
        await asyncio.sleep(1.5 ** attempt)
    return []


def _shard_filter(cat_ids: list) -> list:
    """Залишає лише категорії цього shard-а (round-robin по відсортованому списку)."""
    if SHARD_TOTAL <= 1:
        return list(cat_ids)
    ordered = sorted(set(int(c) for c in cat_ids))
    mine = [c for i, c in enumerate(ordered) if i % SHARD_TOTAL == SHARD_INDEX]
    log(f"🧩 Shard {SHARD_INDEX}/{SHARD_TOTAL}: {len(mine)}/{len(ordered)} категорій")
    return mine


async def fetch_all_products_full(
    client: httpx.AsyncClient, sid: str, cat_ids: list, lang: str,
    out_cache: Path = CACHE_FILE, resume_from: list | None = None,
) -> list:
    """
    FULL: базові дані + повна картка (опис+характеристики) + фото. Кешуємо.

    Стійкість до збоїв/таймаутів Brain:
      • TIME_BUDGET_MIN — коли час вичерпано, КОРЕКТНО зупиняємось, зберігши
        вже завантажене (джоб виходить успішно, прогрес не втрачено).
      • resume_from — товари, вже збагачені попереднім прогоном: не качаємо
        повторно (продовжуємо з місця після таймауту/обриву).
      • Чекпойнт кешу кожні CHECKPOINT_EVERY товарів.
      • Помилка окремого товару НІКОЛИ не валить процес — товар пропускається.
    """
    deadline = (time.monotonic() + TIME_BUDGET_MIN * 60) if TIME_BUDGET_MIN > 0 else None
    def time_left() -> bool:
        return deadline is None or time.monotonic() < deadline

    cat_ids = _shard_filter(cat_ids)

    # уже збагачені товари з попереднього прогону (resume)
    enriched_prev: dict[int, dict] = {}
    for p in (resume_from or []):
        pid = pid_of(p)
        if pid is not None and is_enriched(p):
            enriched_prev[pid] = p

    pool: dict[int, dict] = {}

    # Фаза 1: базові дані (список товарів)
    log("\n📦 Фаза 1: базові дані товарів...")
    skipped_oos = 0
    phase1_stopped = False
    for i, cat_id in enumerate(cat_ids, 1):
        if not time_left():
            log("⏳ Бюджет часу вичерпано на Фазі 1 — зупиняюсь коректно.")
            break
        offset = 0
        cat_count = 0
        while True:
            try:
                products = await fetch_products_page(client, sid, cat_id, lang, offset)
            except StopFetching as e:
                log(f"\n🛑 Зупинка завантаження: {e}")
                phase1_stopped = True
                break
            if not products:
                break
            for p in products:
                pid = pid_of(p)
                if pid is None:
                    continue
                # Пропускаємо відсутні ще ДО завантаження описів/фото — це головна
                # економія часу. Обережно: фільтруємо лише коли дані про наявність
                # реально присутні у відповіді списку.
                is_archive = is_true(p.get("is_archive", False))
                has_stock_field = ("stocks" in p) or ("available" in p)
                if is_archive or (has_stock_field and stock_qty(p) == 0):
                    skipped_oos += 1
                    continue
                p["categoryID"] = p.get("categoryID", cat_id)
                pool[pid] = p
                cat_count += 1
            print(f"   [{i}/{len(cat_ids)}] Кат.{cat_id}: {offset + len(products)}", end="\r")
            if len(products) < 100:
                break
            offset += 100
            await asyncio.sleep(0.4)
        log(f"   [{i}/{len(cat_ids)}] Категорія {cat_id}: {cat_count} в наявності")
        if phase1_stopped:
            break

    product_list = list(pool.values())
    log(f"\n✅ Фаза 1: {len(product_list)} товарів у наявності "
        f"(пропущено відсутніх: {skipped_oos})")

    # ── RESUME: переносимо вже збагачені картки, не качаємо їх повторно ──
    todo = []
    reused = 0
    for p in product_list:
        pid = pid_of(p)
        if pid is not None and pid in enriched_prev:
            # беремо стару збагачену картку, але оновлюємо ціни/наявність зі свіжого списку
            old = enriched_prev[pid]
            for k in ("price", "price_uah", "retail_price_uah", "recommendable_price",
                      "stocks", "stocks_expected", "available", "is_archive"):
                if k in p:
                    old[k] = p[k]
            pool[pid] = old
            reused += 1
        else:
            todo.append(p)
    if reused:
        log(f"♻️  Resume: повторно використано {reused} вже збагачених карток, "
            f"докачати треба {len(todo)}")

    # Фаза 2: повна картка (опис + характеристики, обидві мови) + фото.
    # 2 запити/товар (product вже містить options). batch=4 ≈ ліміт Brain.
    log("📋 Фаза 2: опис + характеристики + фото (паралельно по 4)...")
    total    = len(todo)
    batch_sz = 4
    CHECKPOINT_EVERY = 250
    since_ckpt = 0
    stopped_early = False

    for start in range(0, total, batch_sz):
        if not time_left():
            log(f"\n⏳ Бюджет часу вичерпано на Фазі 2 ({start}/{total}) — "
                f"зберігаю прогрес і виходжу коректно.")
            stopped_early = True
            break
        batch = todo[start:start + batch_sz]
        pids  = [pid_of(p) for p in batch]

        all_results = await asyncio.gather(*[
            asyncio.gather(
                fetch_product_full(client, sid, pid),
                fetch_pictures(client, sid, pid),
            )
            for pid in pids
        ], return_exceptions=True)

        # сигнал коректної зупинки (каскад перелогінів) — зберігаємо й виходимо
        if any(isinstance(res, StopFetching) for res in all_results):
            log(f"\n🛑 Зупинка завантаження на Фазі 2 ({start}/{total}) — "
                f"забагато перелогінів. Прогрес збережено.")
            stopped_early = True
            break

        for p, res in zip(batch, all_results):
            # помилка цілого батч-елемента не валить прогін
            if isinstance(res, Exception):
                p.setdefault("pictures", [])
                continue
            full, pics = res
            if full:
                for k, v in full.items():
                    if v not in (None, "", []):
                        p[k] = v
            p.pop("options_ru", None)
            p["pictures"] = pics

        done = min(start + batch_sz, total)
        since_ckpt += len(batch)
        if since_ckpt >= CHECKPOINT_EVERY:
            save_cache(list(pool.values()), out_cache, quiet=True)
            since_ckpt = 0
        pct  = int(done / total * 100) if total else 100
        bar  = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"   [{bar}] {pct}% ({done}/{total})", end="\r")
        await asyncio.sleep(2)

    final = list(pool.values())
    log(f"\n{'⚠️ Фаза 2 ЗУПИНЕНА за часом' if stopped_early else '✅ Фаза 2 завершена'} "
        f"— у кеші {len(final)} товарів")
    save_cache(final, out_cache)
    return final


async def fetch_prices_only(
    client: httpx.AsyncClient, sid: str, cat_ids: list, lang: str
) -> dict:
    """QUICK: тільки ціни і наявність. pid → basic_product_dict."""
    log("\n⚡ Quick: завантаження цін і наявності...")
    prices = {}
    for i, cat_id in enumerate(cat_ids, 1):
        offset = 0
        while True:
            # Використовуємо глобальний SID
            current_sid = _SID_STATE["sid"] or sid
            products = await fetch_products_page(client, current_sid, cat_id, lang, offset)
            if not products:
                break
            for p in products:
                pid = (p.get("productID") or p.get("product_id")
                       or p.get("ID") or p.get("id"))
                if pid:
                    p["categoryID"] = p.get("categoryID") or cat_id
                    prices[int(pid)] = p
            print(f"   [{i}/{len(cat_ids)}] Кат.{cat_id}: {offset + len(products)}", end="\r")
            if len(products) < 100:
                break
            offset += 100
            await asyncio.sleep(0.4)
        log(f"   [{i}/{len(cat_ids)}] Категорія {cat_id}: {len(prices)} товарів (накопич.)")
    log(f"✅ Ціни отримані: {len(prices)} товарів")
    return prices


async def enrich_products(client: httpx.AsyncClient, sid: str, todo: list) -> list:
    """Отримує повний опис, характеристики та фотографії для списку товарів (Фаза 2 для нових товарів)."""
    if not todo:
        return todo
    log(f"📋 Збагачення {len(todo)} нових товарів (опис + фото, паралельно по 4)...")
    total = len(todo)
    batch_sz = 4
    for start in range(0, total, batch_sz):
        batch = todo[start:start + batch_sz]
        pids  = [pid_of(p) for p in batch]
        
        # Використовуємо глобальний SID
        current_sid = _SID_STATE["sid"] or sid
        all_results = await asyncio.gather(*[
            asyncio.gather(
                fetch_product_full(client, current_sid, pid),
                fetch_pictures(client, current_sid, pid),
            )
            for pid in pids
        ], return_exceptions=True)

        if any(isinstance(res, StopFetching) for res in all_results):
            log(f"🛑 Зупинка збагачення: забагато перелогінів.")
            break

        for p, res in zip(batch, all_results):
            if isinstance(res, Exception):
                p.setdefault("pictures", [])
                continue
            full, pics = res
            if full:
                for k, v in full.items():
                    if v not in (None, "", []):
                        p[k] = v
            p.pop("options_ru", None)
            p["pictures"] = pics
            
        done = min(start + batch_sz, total)
        pct = int(done / total * 100) if total else 100
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"   [{bar}] {pct}% ({done}/{total})", end="\r")
        await asyncio.sleep(2)
    print()  # Новий рядок після завершення прогрес-бару
    return todo


# ══════════════════════════════════════════════════════════════════
#  КЕШ
# ══════════════════════════════════════════════════════════════════

def save_cache(products: list, path: Path = CACHE_FILE, quiet: bool = False):
    path.write_text(
        json.dumps({
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "count":     len(products),
            "products":  products,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    if not quiet:
        size_mb = path.stat().st_size / 1024 / 1024
        log(f"💾 Кеш збережено: {len(products)} товарів ({size_mb:.1f} МБ) → {path.name}")


def load_cache(path: Path = CACHE_FILE) -> list:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        products = data.get("products", [])
        log(f"📦 Кеш завантажено: {len(products)} товарів з {path.name} (створено {data.get('generated','?')})")
        return products
    except Exception as e:
        log(f"⚠️ Помилка читання кешу {path.name}: {e}")
        return []


def load_gender_pins(path: Path = GENDER_PINS_FILE) -> dict:
    """
    Завантажує файл закріплень статі товарів.
    Повертає dict {str(productID): 'girl'|'boy'|'woman'|'man'|None}.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pins = data.get("pins", {})
        log(f"📌 Пінів статі завантажено: {len(pins)} товарів з {path.name}")
        return pins
    except Exception as e:
        log(f"⚠️ Помилка читання gender_pins {path.name}: {e}")
        return {}


def save_gender_pins(pins: dict, path: Path = GENDER_PINS_FILE):
    """
    Зберігає dict {str(productID): gender} у файл gender_pins.json.
    """
    path.write_text(
        json.dumps({
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "count": len(pins),
            "pins": pins,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log(f"📌 Пінів статі збережено: {len(pins)} товарів → {path.name}")


def pid_of(p: dict):
    v = (p.get("productID") or p.get("product_id") or p.get("ID") or p.get("id"))
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def is_enriched(p: dict) -> bool:
    """Товар уже має повну картку (Фаза 2 пройдена) — можна не качати повторно."""
    return ("pictures" in p) and (("options" in p) or ("description" in p))


def apply_prices_to_cache(products: list, prices: dict) -> list:
    updated = gone = 0
    for p in products:
        pid = int(p.get("productID") or p.get("id") or 0)
        if pid and pid in prices:
            new = prices[pid]
            p["price"]               = new.get("price", 0)
            p["price_uah"]           = new.get("price_uah", 0)
            p["retail_price_uah"]    = new.get("retail_price_uah", 0)
            p["recommendable_price"] = new.get("recommendable_price", 0)
            p["stocks"]              = new.get("stocks")
            p["stocks_expected"]     = new.get("stocks_expected")
            p["available"]           = new.get("available")
            p["is_archive"]          = new.get("is_archive", 0)
            updated += 1
        else:
            # Товару більше немає у свіжому списку категорії → вважаємо відсутнім.
            # Обнуляємо ВСІ поля наявності, щоб stock_qty() гарантовано дав 0.
            p["stocks"] = []
            p["available"] = {}
            p["is_archive"] = True
            gone += 1
    log(f"🔄 Оновлено цін: {updated}/{len(products)} (зникли з наявності: {gone})")
    return products


# ══════════════════════════════════════════════════════════════════
#  ДОПОМІЖНІ
# ══════════════════════════════════════════════════════════════════

def safe(text) -> str:
    if not text:
        return ""
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(text)).strip()


def is_true(v) -> bool:
    """
    Нормалізація булевих полів Brain.
    API може віддавати їх як JSON-boolean (true/false), число (1/0)
    або рядок ("1"/"0"/"true"/"false"). Зводимо все до bool.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "t")
    return False


# Скільки штук ставити, коли товар у наявності, але точну кількість
# (поле `available`) API не повернув. 99 завищувало залишки — ставимо 2.
DEFAULT_STOCK = 2


def base_price(p: dict) -> float:
    """
    Базова ЗАКУПІВЕЛЬНА ціна в ГРИВНІ (ДО націнки).

    ВАЖЛИВО:
      • `price`            — у валюті постачальника (USD/EUR), НЕ використовуємо.
      • `price_uah`        — оптова закупівельна в грн — це наша база.
      • `retail_price_uah` / `recommendable_price` — це РОЗДРІБНІ ціни.
        Їх НЕ можна підставляти у fallback бази, інакше на роздріб ще
        накрутиться націнка і ціна злетить. Тому база = тільки price_uah.
    """
    try:
        v = float(str(p.get("price_uah") or 0).replace(",", "."))
        if v > 0:
            return v
    except Exception:
        pass
    return 0.0


def stock_qty(p: dict) -> int:
    """
    Кількість товару в наявності.

    За документацією Brain:
      • `stocks`    — масив ID складів, де товар є (напр. [1,2,3]).
      • `available` — словник {складID: кількість} (напр. {"1":3,"2":1}).
        Приходить ТІЛЬКИ для акаунтів зі статусом OWN_LOGISTICS_MODE.

    Логіка:
      1) Якщо є `available` з реальними кількостями → повертаємо їх суму.
      2) Інакше якщо є хоч один склад у `stocks` → точну к-сть не знаємо,
         ставимо DEFAULT_STOCK (2 шт).
      3) Інакше 0 (товару немає).
    """
    # 1) Точна кількість зі складів
    available = p.get("available")
    if isinstance(available, dict) and available:
        total = 0
        for v in available.values():
            try:
                total += int(float(str(v).replace(",", ".")))
            except Exception:
                pass
        if total > 0:
            return total
        # available є, але всі нулі → товару фактично немає
        return 0

    # 2) Склади (масив ID складів). ВАЖЛИВО відрізняти:
    #    • stocks ВІДСУТНІЙ у відповіді (None) — акаунт не віддає залишки →
    #      кількість невідома, але товар є в каталозі з ціною → DEFAULT_STOCK.
    #    • stocks = [] (порожній список ПРИСУТНІЙ) — товару немає на жодному
    #      складі → 0 (так само ставить apply_prices_to_cache для зниклих).
    stocks = p.get("stocks")
    if isinstance(stocks, list):
        return DEFAULT_STOCK if len(stocks) > 0 else 0
    if isinstance(stocks, (int, float)):
        return int(stocks) if stocks > 0 else 0
    if isinstance(stocks, str) and stocks.strip().isdigit():
        return int(stocks) if int(stocks) > 0 else 0

    # 3) Ні available, ні stocks акаунт не надав → вважаємо в наявності.
    return DEFAULT_STOCK


def vendor_name(p: dict) -> str:
    """
    Назва бренду для <vendor>.
    У списку/картці товару є тільки числовий `vendorID`, а не назва.
    Тому шукаємо назву серед характеристик (`options`): Brain зазвичай
    віддає характеристику «Виробник»/«Бренд» з текстовою назвою.
    Fallback: якщо колись у даних з'явиться текстове поле vendor — беремо його.
    """
    v = p.get("vendor")
    if v and not str(v).isdigit():
        return safe(str(v))
    for opt in p.get("options", []) or []:
        oname = str(opt.get("name") or opt.get("OptionName") or "").strip().lower()
        if oname in ("виробник", "бренд", "производитель", "торгова марка", "торговельна марка"):
            val = opt.get("value") or opt.get("ValueName")
            if val:
                return safe(str(val))
    return ""


# Характеристики, що несуть РОЗМІР (для групування і для KASTA <param name="Розмір">).
# Порядок = пріоритет: спершу явний «Розмір», далі дитячий «Зріст», потім взуття/шкарпетки.
SIZE_PARAM_NAMES = (
    "розмір", "размер",
    "зріст", "рост",
    "розмір взуття", "размер обуви",
    "розмір шкарпеток", "довжина ступні", "длина ступни",
)

KASTA_KIDS_HEIGHTS = [
    36, 38, 40, 42, 44, 46, 48, 50, 52, 54, 56, 62, 68, 74, 80, 86, 92, 98,
    104, 110, 116, 122, 128, 134, 140, 146, 152, 158, 164, 170, 176, 182, 188
]

KASTA_KIDS_AGE_MAP = {
    36: "0м.", 38: "0м.", 40: "0м.", 42: "0м.", 44: "0м.", 46: "0м.", 48: "0м.", 50: "0м.",
    52: "1м.", 54: "1м.", 56: "1м.",
    62: "3м.", 68: "6м.", 74: "9м.", 80: "12м.", 86: "18м.",
    92: "2р.", 98: "3р.", 104: "4р.", 110: "5р.", 116: "6р.", 122: "7р.", 128: "8р.",
    134: "9р.", 140: "10р.", 146: "11р.", 152: "12р.", 158: "13р.", 164: "14р.",
    170: "15р.", 176: "16р.", 182: "17р.", 188: "18р."
}


def standardize_kasta_size(size_str: str) -> str:
    """
    Стандартизує дитячі розміри під сітку Kasta:
      - Якщо є зріст, знаходить точний або найближчий МЕНШИЙ зріст.
      - Якщо немає зросту, але є вік (місяці/роки) — мапить до стандартів Kasta,
        а при діапазонах (наприклад "12-18 міс") обирає МЕНШИЙ зріз.
    """
    if not size_str:
        return ""
    val = size_str.strip().lower()
    
    # 1. Спроба витягти числовий зріст (наприклад, "152 см", "рост 76")
    nums = [int(n) for n in re.findall(r'\d+', val)]
    if nums:
        # Для зросту/діапазону беремо менше число (округлення до меншого, наприклад "104-110" -> 104)
        num = min(nums)
        # Якщо число схоже на зріст дитини (від 30 до 200 см)
        if 30 <= num <= 200:
            # ЗАПОБІЖНИК: Числа менше 80 (наприклад, 42, 44, 46) вважаються зростом в см
            # тільки якщо рядок явно містить "см", "cm", "рост" або "зріст".
            # Інакше це може бути звичайний розмір одягу (наприклад, дорослий 42).
            is_height = True
            if num < 80:
                is_height = any(h_word in val for h_word in ["см", "cm", "рост", "зріст"])
                
            if is_height:
                if num in KASTA_KIDS_HEIGHTS:
                    return f"{num} см"
                    
                # Правило неточного збігу: округлюємо до найближчого МЕНШОГО зросту
                smaller_heights = [h for h in KASTA_KIDS_HEIGHTS if h <= num]
                if smaller_heights:
                    best_h = max(smaller_heights)
                    return f"{best_h} см"
                return "36 см"
            
    # 2. Якщо числового зросту немає або це вік (наприклад, "2 роки", "3м", "12-18м")
    # Шукаємо місяці
    if any(m_word in val for m_word in ["міс", "мес", "місяц", "месяц"]):
        months = min(nums) if nums else 0
        # Округлюємо місяці до найближчого меншого дозволеного Kasta: 0, 1, 3, 6, 9, 12, 18
        allowed_months = [0, 1, 3, 6, 9, 12, 18]
        smaller_m = [m for m in allowed_months if m <= months]
        best_m = max(smaller_m) if smaller_m else 0
        return f"{best_m}м."
        
    # Шукаємо роки
    if any(y_word in val for y_word in ["р", "г", "років", "року", "лет", "y", "year"]):
        years = min(nums) if nums else 0
        if 2 <= years <= 18:
            return f"{years}р."
            
    # Якщо нічого не підійшло, повертаємо як є
    return size_str


def size_value(product: dict):
    """Значення розміру товару + назва характеристики-джерела (за пріоритетом)."""
    opts = product.get("options", []) or []
    for want in SIZE_PARAM_NAMES:
        for o in opts:
            if not isinstance(o, dict):
                continue
            n = str(o.get("OptionName") or o.get("name_ua") or o.get("name") or "").strip().lower()
            if n == want:
                v = safe(o.get("ValueName") or o.get("value_ua") or o.get("value") or "")
                if v:
                    return v, want
    return "", ""


def build_group_id(product: dict) -> str:
    """
    Групування варіантів одного товару (різні розміри/зрости однієї моделі)
    у ОДНУ картку маркетплейсу. Brain не дає офіційного поля групи, тож
    ключ = артикул із вирізаним РОЗМІРОМ, але збереженою моделлю+кольором+статтю.

    Розмір вирізаємо точково за РЕАЛЬНИМ значенням характеристики (Зріст/Розмір),
    а не «будь-яке число» — інакше з'їдається код моделі (напр. G-202-146B: 202=модель).
    Приклади:
      EAD6513.0-3 / .3-6            → EAD6513        (віковий суфікс)
      G-202-146B-blue / G-202-110B → G-202-B-blue   (зріст 146/110 вирізано)
      7075-152B-black              → 7075-B-black
      ISSA-10833-S / -M-yellow     → ISSA-10833-yellow (буквений розмір)
    """
    articul = safe(str(product.get("articul") or product.get("product_code") or ""))
    if not articul:
        return ""
    g = articul
    # 1) віковий суфікс після крапки: .0-3, .9-12
    g = re.sub(r'\.\d{1,2}\s*-\s*\d{1,2}(?=[-_./]|$)', '', g)

    sv, _ = size_value(product)
    nums = re.findall(r'\d{1,3}', sv)
    # 2) зріст-діапазон у характеристиці (110-116 см) → вирізаємо такий діапазон з артикула
    if len(nums) >= 2:
        g = re.sub(rf'([.\-_/]){nums[0]}\s*-\s*{nums[-1]}([BG]?)(?=[.\-_/]|$)',
                   r'\1\2', g, flags=re.I)
    # 3) кожне число розміру/зросту — як окремий токен (зберігаємо літеру статі B/G)
    for num in nums:
        g = re.sub(rf'([.\-_/]){num}([BG]?)(?=[.\-_/]|$)', r'\1\2', g, flags=re.I)
    # 4) буквений розмір одягу (дорослі): і як токен усередині, і як суфікс
    letter = sv.strip().upper()
    if re.fullmatch(r'(XS|S|M|L|XL|XXL|XXXL|XXXXL)', letter):
        g = re.sub(rf'([.\-_/]){letter}(?=[.\-_/]|$)', r'\1', g, flags=re.I)
    g = re.sub(r'[-/_\s]+(XS|S|M|L|XL|XXL|XXXL|XXXXL)$', '', g, flags=re.I)
    # 5) прибрати здвоєні роздільники, що лишились
    g = re.sub(r'[.\-_/]{2,}', '-', g).strip('-._/ ')

    return g if g and g != articul else ""


def load_kasta_colors_config() -> dict:
    """Завантажує конфігурацію кольорів для Kasta з kasta_colors.json."""
    if KASTA_COLORS_FILE.exists():
        try:
            return json.loads(KASTA_COLORS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"   ⚠️ Не вдалося завантажити {KASTA_COLORS_FILE}: {e}")
    return {"allowed_colors": [], "color_map": {}}


def standardize_kasta_color(color_val: str, kasta_cfg: dict) -> str:
    """Нормалізує колір під вимоги Kasta: мапить синоніми або повертає 'комбінований'."""
    if not color_val:
        return "комбінований"
    val = str(color_val).strip().lower()
    
    # 1. Застосувати кастомний мапінг синонімів
    cmap = kasta_cfg.get("color_map", {})
    if val in cmap:
        val = cmap[val]
        
    allowed = kasta_cfg.get("allowed_colors", [])
    
    # 2. Обробка списків кольорів через кому/слеш (наприклад "блакитний,кремовий", "білий / синій")
    if "," in val or "/" in val or " та " in val or " і " in val:
        parts = [p.strip() for p in re.split(r'[,/]| та | і ', val) if p.strip()]
        for p in parts:
            mapped_p = cmap.get(p, p)
            if allowed and mapped_p in allowed and mapped_p != "комбінований":
                return mapped_p
        return "комбінований"
        
    # 3. Перевірити чи дозволений колір
    if allowed:
        if val in allowed:
            return val
        for a in allowed:
            if a in val and a != "комбінований":
                return a
        return "комбінований"
        
    return val


RU_UA_CHAR_MAP = {
    'ы': 'и', 'Ы': 'И',
    'э': 'е', 'Э': 'Е',
    'ъ': "'", 'Ъ': "'",
    'ё': 'е', 'Ё': 'Е',
}

RU_UA_WORD_MAP = {
    "набор ": "набір ",
    "набор": "набір",
    "детской одежды": "дитячого одягу",
    "детской": "дитячої",
    "одежды": "одягу",
    "с рисунком": "з малюнком",
    "животных": "тварин",
    "для девочек": "для дівчаток",
    "для мальчиков": "для хлопчиків",
    "с надписями": "з написами",
    "голубой": "блакитний",
    "розовый": "рожевий",
    "белый": "білий",
    "красный": "червоний",
    "зеленый": "зелений",
    "выполнен": "виконаний",
    "высококачественных": "високоякісних",
    "материалов": "матеріалів",
    "качества": "якості",
    "мягкое": "м'яке",
    "мягкую": "м'яку",
    "комбинезон": "комбінезон",
    "одеяло": "ковдра",
    "полотенце": "рушник",
    "пижама": "піжама",
    "ночная сорочка": "нічна сорочка",
    "футболка": "футболка",
    "шорты": "шорти",
}


def sanitize_ukrainian_description(text: str, fallback_title: str = "") -> str:
    """Очищує опис від російських слів та літер (ы, э, ъ, ё) для проходження модерації Kasta."""
    if not text:
        return fallback_title or "Дитячий одяг високої якості."
    
    t = text
    t_lower = t.lower()
    for ru_w, ua_w in RU_UA_WORD_MAP.items():
        if ru_w in t_lower:
            pattern = re.compile(re.escape(ru_w), re.IGNORECASE)
            t = pattern.sub(ua_w, t)
            
    for ru_c, ua_c in RU_UA_CHAR_MAP.items():
        t = t.replace(ru_c, ua_c)
        
    if re.search(r'[ыэъёЫЭЪЁ]', t):
        return fallback_title or "Дитячий одяг високої якості."
        
    return t.strip()


def enhance_kasta_cat_name(cat_id: int, orig_name: str, cat_map: dict) -> str:
    """Додає уточнення 'для малюків' / 'дитячі' до назв категорій для запобігання помилки жіночого одягу."""
    name = orig_name.strip()
    name_lower = name.lower()
    if any(k in name_lower for k in ["дитяч", "малюк", "малят", "для немовлят"]):
        return name
    
    curr_id = cat_id
    is_kids = False
    while curr_id and curr_id in cat_map:
        if curr_id in (7456, 8138, 7731):
            is_kids = True
            break
        curr_id = cat_map[curr_id].get("parentID")
        
    if is_kids:
        if name_lower in ("боді", "чоловічки", "слинявчики", "спальний конверт", "крижма", "покривальця та ковдри"):
            return f"{name} для малюків"
        return f"{name} дитячі"
    return name


def load_kasta_characteristics_config() -> dict:
    """Завантажує конфігурацію характеристик для Kasta з kasta_characteristics.json."""
    if KASTA_CHARS_FILE.exists():
        try:
            return json.loads(KASTA_CHARS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"   ⚠️ Не вдалося завантажити {KASTA_CHARS_FILE}: {e}")
    return {}


def standardize_kasta_characteristics(p: dict, kasta_char_cfg: dict) -> list[dict]:
    """
    Повертає стандартизований список характеристик [{'name': '...', 'value': '...'}]
    для товару `p` відповідно до вимог Kasta.
    """
    options = p.get("options", []) or []
    out = []
    
    # Текстові джерела для пошуку ключових слів (візерунок)
    name_ua = str(p.get("name_ua") or p.get("name") or "").lower()
    desc_ua = str(p.get("description") or p.get("brief_description") or "").lower()
    search_text = f"{name_ua} {desc_ua}"
    
    # ── 1. Збираємо оригінальні параметри, які не потребують спеціальної заміни ──
    for opt in options:
        if not isinstance(opt, dict):
            continue
        oname = str(opt.get("OptionName") or opt.get("name_ua") or opt.get("name") or "").strip()
        oval = str(opt.get("ValueName") or opt.get("value_ua") or opt.get("value") or "").strip()
        if not (oname and oval):
            continue
            
        oname_lower = oname.lower()
        
        # Ігноруємо ті, які будемо генерувати заново за стандартами Kasta
        if oname_lower in ("сезон", "сезонність", "візерунок", "малюнок", "основний матеріал", "склад матеріалу"):
            continue
            
        # Застібка та декорування обробляються окремо
        if oname_lower in ("застібка", "застежка", "декорування"):
            continue
            
        # Обробка Довжини рукава під стандарти Kasta (без см)
        if oname_lower in ("довжина рукава", "длина рукава"):
            val_l = oval.lower()
            if any(w in val_l for w in ["без рукав", "безрукав"]):
                oval = "без рукавів"
            elif any(w in val_l for w in ["3/4", "три чверті"]):
                oval = "3/4"
            elif "коротк" in val_l:
                oval = "короткий"
            elif "довг" in val_l:
                oval = "довгий"
            else:
                nums = [int(n) for n in re.findall(r'\d+', oval)]
                if nums:
                    oval = "короткий" if nums[0] <= 15 else "довгий"
                else:
                    continue
            oname = "Довжина рукава"
            
        out.append({"name": oname, "value": oval})
        
    # ── 2. Сезонність ──
    orig_season = ""
    for opt in options:
        oname_lower = str(opt.get("OptionName") or opt.get("name_ua") or "").strip().lower()
        if oname_lower in ("сезон", "сезонність"):
            orig_season = str(opt.get("ValueName") or opt.get("value_ua") or "").strip().lower()
            break
            
    season_map = kasta_char_cfg.get("season_map", {})
    mapped_season = season_map.get(orig_season, "Всесезон")
    out.append({"name": "Сезонність", "value": mapped_season})
    
    # ── 3. Візерунок ──
    orig_pattern = ""
    for opt in options:
        oname_lower = str(opt.get("OptionName") or opt.get("name_ua") or "").strip().lower()
        if oname_lower in ("візерунок", "малюнок"):
            orig_pattern = str(opt.get("ValueName") or opt.get("value_ua") or "").strip().lower()
            break
            
    pattern_map = kasta_char_cfg.get("pattern_map", {})
    mapped_pattern = ""
    if orig_pattern in pattern_map:
        mapped_pattern = pattern_map[orig_pattern]
    elif orig_pattern:
        mapped_pattern = "Малюнок"
        
    if not mapped_pattern:
        pattern_keywords = kasta_char_cfg.get("pattern_keywords", {})
        for kasta_val, keywords in pattern_keywords.items():
            if any(kw in search_text for kw in keywords):
                mapped_pattern = kasta_val
                break
                
    if not mapped_pattern:
        mapped_pattern = "Однотонний"
        
    out.append({"name": "Візерунок", "value": mapped_pattern})
    
    # ── 4. Матеріал ──
    materials_found = set()
    materials_list = kasta_char_cfg.get("materials_list", [])
    
    for opt in options:
        oname_lower = str(opt.get("OptionName") or opt.get("name_ua") or "").strip().lower()
        if oname_lower in ("основний матеріал", "склад матеріалу"):
            val_text = str(opt.get("ValueName") or opt.get("value_ua") or "").strip().lower()
            for mat in materials_list:
                mat_lower = mat.lower()
                # Спеціальна перевірка для вовни, щоб вона не збігалася з бавовною
                if mat_lower == "вовна":
                    clean_val = val_text.replace("бавовна", "").replace("cotton", "").replace("хлопок", "")
                    if "вовна" in clean_val or "шерсть" in clean_val or "wool" in clean_val:
                        materials_found.add("Вовна")
                elif mat_lower in val_text:
                    if mat_lower == "вовна" and "бавовна" in val_text:
                        continue
                    materials_found.add(mat)
                elif mat_lower == "бавовна" and ("хлопок" in val_text or "cotton" in val_text):
                    materials_found.add("Бавовна")
                elif mat_lower == "еластан" and ("elastane" in val_text or "эластан" in val_text or "spandex" in val_text):
                    materials_found.add("Еластан")
                elif mat_lower == "поліестер" and ("polyester" in val_text or "полиэстер" in val_text):
                    materials_found.add("Поліестер")
                elif mat_lower == "віскоза" and ("viscose" in val_text or "вискоза" in val_text):
                    materials_found.add("Віскоза")
                elif mat_lower == "нейлон" and ("nylon" in val_text):
                    materials_found.add("Нейлон")
                    
    if materials_found:
        for mat in sorted(materials_found):
            out.append({"name": "Матеріал", "value": mat})
            
    # ── 5. Декорування ──
    orig_decor = ""
    for opt in options:
        oname_lower = str(opt.get("OptionName") or opt.get("name_ua") or "").strip().lower()
        if oname_lower == "декорування":
            orig_decor = str(opt.get("ValueName") or opt.get("value_ua") or "").strip().lower()
            break
    if orig_decor:
        decor_map = kasta_char_cfg.get("decor_map", {})
        if orig_decor in decor_map:
            out.append({"name": "Декор", "value": decor_map[orig_decor]})
            
    # ── 6. Застібка ──
    orig_fastener = ""
    for opt in options:
        oname_lower = str(opt.get("OptionName") or opt.get("name_ua") or "").strip().lower()
        if oname_lower in ("застібка", "застежка"):
            orig_fastener = str(opt.get("ValueName") or opt.get("value_ua") or "").strip().lower()
            break
    if orig_fastener:
        fastener_map = kasta_char_cfg.get("fastener_map", {})
        if orig_fastener in fastener_map:
            out.append({"name": "Застібка", "value": fastener_map[orig_fastener]})
            
    return out


# ══════════════════════════════════════════════════════════════════
#  СТАТЬ ДИТИНИ (для поділу дитячого одягу) + ЧИСТКА ДЛЯ KASTA
# ══════════════════════════════════════════════════════════════════

# Brain має ДВА типи характеристики статі:
#   • дитячий одяг:      «Стать дитини» / «Пол ребенка» → дівчинка / хлопчик
#   • дорослий одяг/взуття: «Стать» / «Пол» → жіноча / чоловіча / унісекс / дитячі
GENDER_PARAM_CHILD = {"стать дитини", "пол ребенка"}
GENDER_PARAM_ADULT = {"стать", "пол"}
GENDER_PARAM_NAMES = GENDER_PARAM_CHILD | GENDER_PARAM_ADULT  # для виключення з <param>

# Суфікси назв синтетичних підкатегорій + суфікс до ID категорії.
#   br8141 → br8141g (дівчатка) / br8141b (хлопці) / br8141w (жінки) / br8141m (чоловіки)
GENDER_SUFFIX = {
    "ua": {"girl": "для дівчаток", "boy": "для хлопців",
           "woman": "жіночі",      "man": "чоловічі"},
    "ru": {"girl": "для девочек",  "boy": "для мальчиков",
           "woman": "женские",     "man": "мужские"},
}
GENDER_CID_SUFFIX = {"girl": "g", "boy": "b", "woman": "w", "man": "m"}


def detect_gender(p: dict):
    """
    Стать товару → 'girl' | 'boy' | 'woman' | 'man' | None (унісекс).

    Дитячий параметр має пріоритет над дорослим. Унісекс (None), коли:
      • параметра статі немає, АБО
      • значення «унісекс» / «дитячі», АБО
      • вказані ОБИДВІ статі одночасно (товар для всіх).
    Значення матчимо підрядком, обома мовами.
    """
    girl = boy = woman = man = False
    for opt in p.get("options", []) or []:
        if not isinstance(opt, dict):
            continue
        oname = str(opt.get("OptionName") or opt.get("name_ua")
                    or opt.get("name") or "").strip().lower()
        oval = str(opt.get("ValueName") or opt.get("value_ua")
                   or opt.get("value") or "").strip().lower()
        if oname in GENDER_PARAM_CHILD:
            if "дівч" in oval or "девоч" in oval or "девич" in oval:
                girl = True
            elif "хлопч" in oval or "мальчик" in oval:
                boy = True
        elif oname in GENDER_PARAM_ADULT:
            if "жіноч" in oval or "женс" in oval:
                woman = True
            elif "чолов" in oval or "мужс" in oval:
                man = True
            # «унісекс» / «дитячі» / «детск» → не стать, лишаємо унісекс

    # Фолбек: якщо стать не визначена з характеристик, перевіряємо назву та опис (укр. та рос.)
    if not (girl or boy or woman or man):
        name_ua = str(p.get("name") or p.get("name_ua") or "").lower()
        desc_ua = str(p.get("description") or p.get("brief_description") or "").lower()
        name_ru = str(p.get("name_ru") or "").lower()
        desc_ru = str(p.get("description_ru") or p.get("brief_description_ru") or "").lower()
        full_text = f"{name_ua} {desc_ua} {name_ru} {desc_ru}"
        
        if any(w in full_text for w in ["для дівчаток", "для дівчинки", "для дівчат", "дівчинці", "дівчинка", "для девочек", "для девочки", "девочке", "девочка"]):
            girl = True
        elif any(w in full_text for w in ["для хлопчиків", "для хлопчика", "для хлопців", "хлопчику", "хлопчик", "для мальчиков", "для мальчика", "мальчику", "мальчик"]):
            boy = True
        elif any(w in full_text for w in ["жіноч", "для жінок", "женск", "для женщин"]):
            woman = True
        elif any(w in full_text for w in ["чоловіч", "для чоловіків", "мужск", "для мужчин"]):
            man = True

    # дитяча стать має пріоритет над дорослою
    if girl != boy:
        return "girl" if girl else "boy"
    if girl and boy:
        return None
    if woman != man:
        return "woman" if woman else "man"
    return None


def clean_html(text, limit: int = 5000) -> str:
    """
    KASTA не приймає HTML в описі і ріже до 5000 символів.
    Знімаємо теги, розкодовуємо сутності, схлопуємо пробіли, обрізаємо.
    """
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", str(text))
    t = (t.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&"))
    t = re.sub(r"\s+", " ", t).strip()
    return safe(t)[:limit]


# Стоп-слова KASTA у назві/бренді (конкуренти/маркетплейси) — прибираємо.
KASTA_STOPWORDS = ("rozetka", "розетка", "prom", "пром", "express",
                   "expres", "meest", "kasta", "каста")


def strip_stopwords(text) -> str:
    if not text:
        return ""
    out = str(text)
    for w in KASTA_STOPWORDS:
        out = re.sub(re.escape(w), "", out, flags=re.I)
    return re.sub(r"\s+", " ", out).strip()


# ══════════════════════════════════════════════════════════════════
#  XML ГЕНЕРАТОР (один фід)
# ══════════════════════════════════════════════════════════════════

def build_feed_xml(
    products: list, all_cats: list, feed: dict,
    shop_url: str, output: Path,
) -> dict:
    cat_map     = {c["categoryID"]: c for c in all_cats}
    by_parent: dict[int, list] = {}
    for c in all_cats:
        by_parent.setdefault(c.get("parentID", 1), []).append(c["categoryID"])

    # Які категорії потрібні цьому фіду (вибрані + усі нащадки)
    needed = set()
    for cid in feed["category_ids"]:
        if cid in cat_map:
            needed |= get_all_descendants(by_parent, cid)

    lang = feed["lang"]
    mp   = feed["markup_percent"]
    mf   = feed["markup_fixed"]
    # префікси ID per-feed (fallback на глобальний "br")
    po = feed.get("prefix_offer")    or ID_PREFIX
    pc = feed.get("prefix_category") or ID_PREFIX
    def oid(x): return f"{po}{x}"
    def cidx(x): return f"{pc}{x}"
    log(f"📝 Фід '{feed['id']}': категорій={len(needed)}, націнка +{mp}% +{mf}грн")

    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    root = Element("yml_catalog"); root.set("date", now)
    shop = SubElement(root, "shop")
    SubElement(shop, "name").text    = safe(feed["name"])
    SubElement(shop, "company").text = safe(feed["name"])
    SubElement(shop, "url").text     = safe(shop_url)
    cur = SubElement(SubElement(shop, "currencies"), "currency")
    cur.set("id", "UAH"); cur.set("rate", "1")

    # Категорії + усі батьки для коректної ієрархії
    full_needed = set(needed)
    for cid in list(needed):
        pid = cat_map.get(cid, {}).get("parentID", 1)
        while pid and pid != 1 and pid in cat_map:
            full_needed.add(pid)
            pid = cat_map.get(pid, {}).get("parentID", 1)

    cats_el = SubElement(shop, "categories")
    n_cats = 0
    for cat in all_cats:
        if cat["categoryID"] not in full_needed:
            continue
        el = SubElement(cats_el, "category")
        el.set("id", cidx(cat["categoryID"]))
        if cat.get("parentID", 1) != 1:
            el.set("parentId", cidx(cat["parentID"]))
        el.text = safe(cat["name"])
        n_cats += 1

    offers_el = SubElement(shop, "offers")
    added = skipped = errors = 0

    for p in products:
        try:
            pid = (p.get("productID") or p.get("product_id") or p.get("ID") or p.get("id"))
            if not pid:
                skipped += 1; continue
            pid = int(pid)

            # фільтр по категоріях фіда
            if p.get("categoryID") and int(p["categoryID"]) not in full_needed:
                skipped += 1; continue

            bp = base_price(p)
            if bp <= 0:
                skipped += 1; continue

            # ── ФІЛЬТР НАЯВНОСТІ: у XML потрапляють ТІЛЬКИ товари в наявності ──
            # (архівні та з нульовим залишком пропускаємо повністю)
            is_archive = is_true(p.get("is_archive", False))
            qty = stock_qty(p)
            if is_archive or qty == 0:
                skipped += 1; continue

            # Будуємо offer окремо; приєднаємо до дерева лише якщо зберемо
            # повністю без помилок (інакше биті товари лишали б огризки в XML).
            offer = Element("offer")
            offer.set("id", oid(pid))

            gid = build_group_id(p)
            if gid:
                offer.set("group_id", f"{po}{gid}")

            # повна картка тепер тягне обидві мови (ua_ru): для ru-фіда беремо
            # name_ru, інакше укр name. Так ru-фіди не ламаються.
            nm = (p.get("name_ru") if lang == "ru" else None) or p.get("name") or ""
            SubElement(offer, "name").text = safe(nm)

            # ── ЦІНА З НАЦІНКОЮ ФІДА: bp × (1 + %/100) + грн ──
            sell = round(bp * (1 + mp / 100) + mf, 0)
            SubElement(offer, "price").text      = str(int(sell))
            SubElement(offer, "currencyId").text = "UAH"

            # стара ціна — рекомендована роздрібна, якщо вона вища за нашу ціну
            try:
                rec = float(str(p.get("retail_price_uah") or p.get("recommendable_price") or 0).replace(",", "."))
            except Exception:
                rec = 0
            if rec > sell:
                SubElement(offer, "price_old").text = str(int(rec))

            if p.get("categoryID"):
                SubElement(offer, "categoryId").text = cidx(p["categoryID"])

            # ── ФОТО ──
            pics = p.get("pictures", [])
            if isinstance(pics, list) and pics:
                for pic in sorted(pics, key=lambda x: x.get("priority", 99) if isinstance(x, dict) else 99):
                    if not isinstance(pic, dict):
                        continue
                    url = pic.get("full_image") or pic.get("large_image") or pic.get("medium_image")
                    if url and "no-photo" not in url:
                        SubElement(offer, "picture").text = url
            else:
                for key in ["full_image", "large_image", "medium_image", "small_image"]:
                    if p.get(key) and "no-photo" not in str(p.get(key)):
                        SubElement(offer, "picture").text = p[key]; break

            # ── ВИРОБНИК (назва бренду, не числовий vendorID) ──
            vn = vendor_name(p)
            if vn:
                SubElement(offer, "vendor").text = vn

            articul = p.get("articul") or p.get("product_code", "")
            if articul:
                SubElement(offer, "article").text = safe(str(articul))
            if p.get("warranty"):
                SubElement(offer, "warranty").text = f"{p['warranty']} міс."
            if p.get("country"):
                SubElement(offer, "country_of_origin").text = safe(str(p["country"]))
            # код УКТЗЕД (приходить у повній картці товару)
            if p.get("koduktved"):
                SubElement(offer, "uktzed").text = safe(str(p["koduktved"]))
            if p.get("weight"):
                try:
                    w = round(float(str(p["weight"]).replace(",", ".")), 3)
                    if w > 0:
                        SubElement(offer, "weight").text = str(w)
                except Exception:
                    pass

            # ── НАЯВНІСТЬ (товар, що дійшов сюди, завжди в наявності) ──
            offer.set("available", "true")
            SubElement(offer, "stock_quantity").text = str(qty)

            # ── ОПИС: повний `description`, fallback на короткий (мовно-залежно) ──
            desc_src = ((p.get("description_ru") or p.get("brief_description_ru"))
                        if lang == "ru" else None)
            desc = safe(desc_src or p.get("description") or p.get("brief_description") or "")
            if desc:
                d = SubElement(offer, "description")
                d.text = f"__CDATA_OPEN__{desc}__CDATA_CLOSE__"

            # ── ХАРАКТЕРИСТИКИ ──
            for opt in p.get("options", []) or []:
                if not isinstance(opt, dict):
                    continue
                oname = safe(opt.get("OptionName") or opt.get("name_ua") or opt.get("name") or "")
                oval  = safe(opt.get("ValueName")  or opt.get("value_ua") or opt.get("value") or "")
                if oname and oval:
                    param = SubElement(offer, "param")
                    param.set("name", oname)
                    param.text = oval

            if is_true(p.get("is_new", False)):
                SubElement(offer, "is_new").text = "true"

            # Усе зібралось без помилок → додаємо товар у фід
            offers_el.append(offer)
            added += 1

        except Exception as e:
            # Один-два «биті» товари не повинні зривати весь фід —
            # просто пропускаємо їх і рахуємо в errors.
            errors += 1
            _pid = p.get("productID") or p.get("id") or "?"
            log(f"   ⚠️ Пропущено товар {_pid} через помилку: {e}")
            continue

    if errors:
        log(f"   ⚠️ Фід '{feed['id']}': пропущено через помилки — {errors} товар(ів)")

    # запис
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = tostring(root, encoding="unicode")
    try:
        pretty = parseString(f'<?xml version="1.0" encoding="UTF-8"?>{raw}').toprettyxml(indent="  ")
        lines = pretty.splitlines()
        if lines[0].startswith("<?xml"):
            lines[0] = '<?xml version="1.0" encoding="UTF-8"?>'
        final = "\n".join(lines)
    except Exception:
        final = f'<?xml version="1.0" encoding="UTF-8"?>\n{raw}'

    # реальні CDATA: ElementTree екранує весь текст (включно з HTML у описі).
    # Розгортаємо плейсхолдери у справжні CDATA і знімаємо екранування ВСЕРЕДИНІ них.
    def _unescape_cdata(m):
        inner = m.group(1)
        inner = (inner.replace("&lt;", "<").replace("&gt;", ">")
                      .replace("&quot;", '"').replace("&apos;", "'")
                      .replace("&amp;", "&"))
        return f"<![CDATA[{inner}]]>"

    final = final.replace("&lt;![CDATA[", "__CDATA_OPEN__").replace("]]&gt;", "__CDATA_CLOSE__")
    final = re.sub(r'__CDATA_OPEN__(.*?)__CDATA_CLOSE__', _unescape_cdata, final, flags=re.S)

    output.write_text(final, encoding="utf-8")
    size_mb = output.stat().st_size / 1024 / 1024
    return {"offers": added, "skipped": skipped, "categories": n_cats, "size_mb": round(size_mb, 2)}


# ══════════════════════════════════════════════════════════════════
#  XML ГЕНЕРАТОР — KASTA (name_ua, опис без HTML, old_price, поділ за статтю)
# ══════════════════════════════════════════════════════════════════

def build_kasta_feed_xml(
    products: list, all_cats: list, feed: dict,
    shop_url: str, output: Path,
) -> dict:
    kasta_cfg = load_kasta_colors_config()
    kasta_char_cfg = load_kasta_characteristics_config()
    cat_map = {c["categoryID"]: c for c in all_cats}
    by_parent: dict[int, list] = {}
    for c in all_cats:
        by_parent.setdefault(c.get("parentID", 1), []).append(c["categoryID"])

    needed = set()
    for cid in feed["category_ids"]:
        if cid in cat_map:
            needed |= get_all_descendants(by_parent, cid)

    lang = feed["lang"]
    mp   = feed["markup_percent"]
    mf   = feed["markup_fixed"]
    suffixes = GENDER_SUFFIX.get(lang, GENDER_SUFFIX["ua"])
    # префікси ID per-feed (fallback на глобальний "br")
    po = feed.get("prefix_offer")    or ID_PREFIX
    pc = feed.get("prefix_category") or ID_PREFIX
    def oid(x): return f"{po}{x}"
    def cidx(x): return f"{pc}{x}"

    # ── Категорії, де РОБИМО розбивку за статтю (вибрані батьки + їх нащадки) ──
    # Порожньо → розбивки немає взагалі (усі товари в рідних категоріях).
    split_scope = set()
    for cid in feed.get("split_category_ids", []):
        if cid in cat_map:
            split_scope |= get_all_descendants(by_parent, cid)

    # ── Завантажуємо закріплення статі ──
    gender_pins = load_gender_pins()
    new_pins: dict = {}  # нові визначення для збереження

    def gender_of(p):
        """Стать товару, але ТІЛЬКИ якщо його категорія в split_scope; інакше None.
        Спочатку перевіряє gender_pins.json — якщо є закріплення, використовує його.
        Інакше визначає автоматично і додає до new_pins для збереження."""
        try:
            if int(p.get("categoryID")) in split_scope:
                pid_key = str(pid_of(p))
                if pid_key in gender_pins:
                    return gender_pins[pid_key]  # закріплений результат
                detected = detect_gender(p)
                new_pins[pid_key] = detected  # запам'ятовуємо для збереження
                return detected
        except Exception:
            pass
        return None

    log(f"📝 KASTA-фід '{feed['id']}': категорій={len(needed)}, "
        f"націнка +{mp}% +{mf}грн, розбивка за статтю в {len(split_scope)} катег., "
        f"закріплено статей={len(gender_pins)}")

    def in_scope(p) -> bool:
        cid = p.get("categoryID")
        try:
            return bool(cid) and int(cid) in needed
        except Exception:
            return False

    # ── Пас 1: які пари (категорія, стать) реально зустрічаються —
    #    щоб створити рівно ті дочірні категорії за статтю, що потрібні ──
    gender_cats_used: set[tuple[int, str]] = set()
    for p in products:
        if not in_scope(p):
            continue
        g = gender_of(p)
        if g:
            gender_cats_used.add((int(p["categoryID"]), g))
    log(f"   👫 Підкатегорій за статтю: {len(gender_cats_used)} "
        f"(товари поза розбивкою лишаються в рідній категорії)")

    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    root = Element("yml_catalog"); root.set("date", now)
    shop = SubElement(root, "shop")
    SubElement(shop, "name").text    = safe(feed["name"])
    SubElement(shop, "company").text = safe(feed["name"])
    SubElement(shop, "url").text     = safe(shop_url)
    cur = SubElement(SubElement(shop, "currencies"), "currency")
    cur.set("id", "UAH"); cur.set("rate", "1")

    # повний набір категорій (вибрані + усі батьки)
    full_needed = set(needed)
    for cid in list(needed):
        pid = cat_map.get(cid, {}).get("parentID", 1)
        while pid and pid != 1 and pid in cat_map:
            full_needed.add(pid)
            pid = cat_map.get(pid, {}).get("parentID", 1)

    cats_el = SubElement(shop, "categories")
    n_cats = 0
    for cat in all_cats:
        if cat["categoryID"] not in full_needed:
            continue
        el = SubElement(cats_el, "category")
        el.set("id", cidx(cat["categoryID"]))
        if cat.get("parentID", 1) != 1:
            el.set("parentId", cidx(cat["parentID"]))
        el.text = safe(enhance_kasta_cat_name(cat["categoryID"], cat["name"], cat_map))
        n_cats += 1

    # ── синтетичні дочірні категорії за статтю ──
    # br8141 «Комбінезони» → br8141g «Комбінезони для дівчаток», br8141b «… для хлопців»
    for (orig_cid, g) in sorted(gender_cats_used):
        el = SubElement(cats_el, "category")
        el.set("id", cidx(orig_cid) + GENDER_CID_SUFFIX[g])
        el.set("parentId", cidx(orig_cid))
        base_name = safe(enhance_kasta_cat_name(orig_cid, cat_map.get(orig_cid, {}).get("name", ""), cat_map))
        el.text = f"{base_name} {suffixes[g]}".strip()
        n_cats += 1

    offers_el = SubElement(shop, "offers")
    added = skipped = errors = no_photo = 0

    for p in products:
        try:
            pid = (p.get("productID") or p.get("product_id") or p.get("ID") or p.get("id"))
            if not pid:
                skipped += 1; continue
            pid = int(pid)
            if not in_scope(p):
                skipped += 1; continue

            bp = base_price(p)
            if bp <= 0:
                skipped += 1; continue

            is_archive = is_true(p.get("is_archive", False))
            qty = stock_qty(p)
            if is_archive or qty == 0:
                skipped += 1; continue

            nm_ua = strip_stopwords(safe(p.get("name") or ""))
            nm_ru = strip_stopwords(safe(p.get("name_ru") or ""))
            if not (nm_ua or nm_ru):
                skipped += 1; continue  # KASTA вимагає назву

            offer = Element("offer")
            offer.set("id", oid(pid))
            offer.set("available", "true")

            gid = build_group_id(p)
            if gid:
                offer.set("group_id", f"{po}{gid}")

            SubElement(offer, "currencyId").text = "UAH"

            # ── КАТЕГОРІЯ: за статтю лише якщо категорія в split_scope, інакше рідна ──
            orig_cid = int(p["categoryID"])
            g = gender_of(p)
            cat_id_out = cidx(orig_cid) + (GENDER_CID_SUFFIX[g] if g else "")
            SubElement(offer, "categoryId").text = cat_id_out

            # ── ЦІНИ (KASTA: old_price СТРОГО > price) ──
            sell = round(bp * (1 + mp / 100) + mf, 0)
            SubElement(offer, "price").text = str(int(sell))
            try:
                rec = float(str(p.get("retail_price_uah")
                                or p.get("recommendable_price") or 0).replace(",", "."))
            except Exception:
                rec = 0
            if rec > sell:
                SubElement(offer, "old_price").text = str(int(rec))

            # ── ФОТО (мінімум 1, максимум 20) ──
            n_pics = 0
            pics = p.get("pictures", [])
            if isinstance(pics, list) and pics:
                for pic in sorted(pics, key=lambda x: x.get("priority", 99) if isinstance(x, dict) else 99):
                    if n_pics >= 20:
                        break
                    if not isinstance(pic, dict):
                        continue
                    url = pic.get("full_image") or pic.get("large_image") or pic.get("medium_image")
                    if url and "no-photo" not in url:
                        SubElement(offer, "picture").text = url; n_pics += 1
            if n_pics == 0:
                for key in ["full_image", "large_image", "medium_image", "small_image"]:
                    if p.get(key) and "no-photo" not in str(p.get(key)):
                        SubElement(offer, "picture").text = p[key]; n_pics += 1; break
            if n_pics == 0:
                no_photo += 1; skipped += 1; continue  # без фото KASTA відхилить

            vn = strip_stopwords(vendor_name(p))
            if vn:
                SubElement(offer, "vendor").text = safe(vn)

            articul = p.get("articul") or p.get("product_code", "")
            if articul:
                SubElement(offer, "article").text = safe(str(articul))

            # назва — обидві мови (KASTA воліє name_ua; name_ru — бонус)
            if nm_ua:
                SubElement(offer, "name_ua").text = nm_ua
            if nm_ru:
                SubElement(offer, "name_ru").text = nm_ru

            # опис — обидві мови, без HTML, ≤5000 символів, очищений від рос. літер
            desc_ua = clean_html(p.get("description") or p.get("brief_description") or "")
            desc_ua = sanitize_ukrainian_description(desc_ua, nm_ua)
            if desc_ua:
                SubElement(offer, "description_ua").text = desc_ua
            desc_ru = clean_html(p.get("description_ru") or p.get("brief_description_ru") or "")
            if desc_ru:
                SubElement(offer, "description_ru").text = desc_ru

            SubElement(offer, "stock_quantity").text = str(qty)

            # ── ХАРАКТЕРИСТИКИ (стать НЕ дублюємо — вона вже у категорії) ──
            has_rozmir = False
            kasta_options = standardize_kasta_characteristics(p, kasta_char_cfg)
            for opt in kasta_options:
                oname = opt["name"]
                oval = opt["value"]
                
                # Додатково фільтруємо стать
                if oname.strip().lower() in GENDER_PARAM_NAMES:
                    continue
                # Нормалізація кольору для Kasta
                if oname.strip().lower() in ("колір", "цвет"):
                    oval = standardize_kasta_color(oval, kasta_cfg)
                if oname.strip().lower() in ("розмір", "размер"):
                    has_rozmir = True
                    oval = standardize_kasta_size(oval)
                    
                param = SubElement(offer, "param")
                param.set("name", oname)
                param.text = oval

            # ── РОЗМІР для KASTA: якщо явного «Розмір» немає (дитячий одяг має
            # «Зріст», взуття — «Розмір взуття») — додаємо <param name="Розмір">
            # з відповідного джерела, інакше KASTA дасть SIZE_NOT_PROVIDED.
            if not has_rozmir:
                sv, _src = size_value(p)
                if sv:
                    pr = SubElement(offer, "param")
                    pr.set("name", "Розмір")
                    pr.text = standardize_kasta_size(sv)

            offers_el.append(offer)
            added += 1

        except Exception as e:
            errors += 1
            _pid = p.get("productID") or p.get("id") or "?"
            log(f"   ⚠️ KASTA: пропущено товар {_pid} через помилку: {e}")
            continue

    if no_photo:
        log(f"   ℹ️ KASTA-фід '{feed['id']}': без фото пропущено — {no_photo}")
    if errors:
        log(f"   ⚠️ KASTA-фід '{feed['id']}': пропущено через помилки — {errors}")

    output.parent.mkdir(parents=True, exist_ok=True)
    raw = tostring(root, encoding="unicode")
    try:
        pretty = parseString(f'<?xml version="1.0" encoding="UTF-8"?>{raw}').toprettyxml(indent="  ")
        lines = pretty.splitlines()
        if lines[0].startswith("<?xml"):
            lines[0] = '<?xml version="1.0" encoding="UTF-8"?>'
        final = "\n".join(lines)
    except Exception:
        final = f'<?xml version="1.0" encoding="UTF-8"?>\n{raw}'

    output.write_text(final, encoding="utf-8")
    size_mb = output.stat().st_size / 1024 / 1024

    # ── Зберігаємо нові закріплення статі (тільки нові/невідомі товари) ──
    if new_pins:
        merged_pins = {**new_pins, **gender_pins}  # gender_pins має пріоритет над new_pins
        save_gender_pins(merged_pins)
        log(f"   📌 Статей закріплено загалом: {len(merged_pins)} ({len(new_pins)} нових)")

    return {"offers": added, "skipped": skipped, "categories": n_cats, "size_mb": round(size_mb, 2)}


# ══════════════════════════════════════════════════════════════════
#  ГОЛОВНА ФУНКЦІЯ
# ══════════════════════════════════════════════════════════════════

def compute_needed(all_selected: set, all_cats: list) -> set:
    """Вибрані категорії + усі їх нащадки — повний набір для завантаження."""
    cat_map  = {c["categoryID"]: c for c in all_cats}
    by_parent: dict[int, list] = {}
    for c in all_cats:
        by_parent.setdefault(c.get("parentID", 1), []).append(c["categoryID"])
    needed = set()
    for cid in all_selected:
        if cid in cat_map:
            needed |= get_all_descendants(by_parent, cid)
    return needed


def get_root_selected_categories(all_selected: set, all_cats: list) -> set:
    """Повертає лише ті вибрані категорії, які не мають батьківських категорій серед вибраних (для уникнення дублюючих запитів)."""
    cat_map = {c["categoryID"]: c for c in all_cats}
    roots = set()
    for cid in all_selected:
        has_selected_ancestor = False
        pid = cat_map.get(cid, {}).get("parentID", 1)
        while pid and pid != 1:
            if pid in all_selected:
                has_selected_ancestor = True
                break
            pid = cat_map.get(pid, {}).get("parentID", 1)
        if not has_selected_ancestor:
            roots.add(cid)
    return roots


# Поріг попередження про розмір XML. git push відхиляє файли >100МБ, тому
# великі фіди ми віддаємо через GitHub Releases (до 2ГБ), а не commit у репо.
SIZE_WARN_MB = 95

def build_all_feeds(products: list, all_cats: list, feeds: list, shop_url: str) -> list:
    """Будує XML для кожного фіда, прибирає осиротілі файли. Повертає статистику."""
    log(f"\n{'─' * 55}")
    results = []
    current_ids = set()
    for f in feeds:
        out = OUTPUT_DIR / f"{f['id']}.xml"
        if f.get("format") == "kasta":
            stats = build_kasta_feed_xml(products, all_cats, f, shop_url, out)
        else:
            stats = build_feed_xml(products, all_cats, f, shop_url, out)
        stats["id"] = f["id"]; stats["file"] = str(out)
        results.append(stats)
        current_ids.add(f["id"])
        warn = f"  ⚠️ >{SIZE_WARN_MB}МБ — віддавати через Releases!" if stats["size_mb"] > SIZE_WARN_MB else ""
        log(f"  ✅ {f['id']}.xml — товарів {stats['offers']}, "
            f"категорій {stats['categories']}, {stats['size_mb']}МБ{warn}")

    # прибирання осиротілих фідів (видалені з feeds.json)
    for old in OUTPUT_DIR.glob("*.xml"):
        if old.stem not in current_ids:
            try:
                old.unlink()
                log(f"  🗑️  Видалено застарілий фід: {old.name}")
            except Exception as e:
                log(f"  ⚠️ Не вдалось видалити {old.name}: {e}")
    return results


def _export_link_block(results: list):
    log(f"\n{'=' * 55}")
    for r in results:
        log(f"   • {r['id']}: output/{r['id']}.xml ({r['size_mb']}МБ)")
    log(f"{'=' * 55}")


async def stage_setup(client, base, feeds):
    """SETUP: один auth + категорії. Віддає SID наступним джобам (через файл)."""
    sid = await ensure_sid(client)
    all_cats = await fetch_categories(client, sid, base["lang"])
    save_categories_json(all_cats)
    # передаємо SID shard-ам: у файл (workflow зчитає й замаскує) + у GITHUB_OUTPUT
    Path(".brain_sid").write_text(sid, encoding="utf-8")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"sid={sid}\n")
    log("✅ SETUP завершено: categories.json + SID готові для shard-ів")


async def stage_shard(client, base, feeds):
    """SHARD: качає СВОЮ частину категорій → products_cache.shard{N}.json."""
    sid = await ensure_sid(client)
    all_cats = load_categories_flat()
    if not all_cats:
        log("❌ SHARD: немає categories.json (setup не відпрацював?)"); sys.exit(1)
    all_selected = set()
    for f in feeds:
        all_selected |= set(f["category_ids"])
    needed = compute_needed(all_selected, all_cats)
    fetch_ids = get_root_selected_categories(all_selected, all_cats)
    log(f"📋 Категорій усього (оптимізовано/з нащадками): {len(fetch_ids)}/{len(needed)}")
    # resume: попередній спільний кеш у репо дозволяє не качати вже зроблене
    resume = load_cache(CACHE_FILE)
    out_file = shard_cache_file(SHARD_INDEX)
    await fetch_all_products_full(
        client, sid, list(fetch_ids), base["lang"],
        out_cache=out_file, resume_from=resume,
    )
    log(f"✅ SHARD {SHARD_INDEX}/{SHARD_TOTAL} завершено → {out_file.name}")


def stage_merge(base, feeds):
    """MERGE: збирає shard-кеші (+ попередній) в один кеш і будує XML."""
    all_cats = load_categories_flat()
    if not all_cats:
        log("❌ MERGE: немає categories.json"); sys.exit(1)

    # База — попередній спільний кеш: для зрізів, які якийсь shard не оновив
    # (таймаут/збій Brain), лишаються останні відомі дані (graceful degradation).
    combined: dict[int, dict] = {}
    for p in load_cache(CACHE_FILE):
        pid = pid_of(p)
        if pid is not None:
            combined[pid] = p
    fresh = 0
    for idx in range(SHARD_TOTAL):
        sc = load_cache(shard_cache_file(idx))
        for p in sc:
            pid = pid_of(p)
            if pid is not None:
                combined[pid] = p   # свіжі дані shard-а авторитетні
                fresh += 1
    products = list(combined.values())
    log(f"🧩 MERGE: {len(products)} товарів у спільному кеші (свіжих із shard-ів: {fresh})")
    save_cache(products, CACHE_FILE)

    results = build_all_feeds(products, all_cats, feeds, base["shop_url"])
    _export_link_block(results)


async def stage_solo(client, base, feeds, mode):
    """SOLO: усе в одному процесі (локально або малий каталог)."""
    sid = await ensure_sid(client)
    try:
        all_cats = await fetch_categories(client, sid, base["lang"])
        save_categories_json(all_cats)
        all_selected = set()
        for f in feeds:
            all_selected |= set(f["category_ids"])
        needed = compute_needed(all_selected, all_cats)
        fetch_ids = get_root_selected_categories(all_selected, all_cats)
        log(f"📋 Категорій для завантаження (оптимізовано/з нащадками): {len(fetch_ids)}/{len(needed)}")

        if mode == "full":
            products = await fetch_all_products_full(
                client, sid, list(fetch_ids), base["lang"], resume_from=load_cache(CACHE_FILE))
        else:
            log("⚡ Quick: кеш + оновлення цін...")
            products = load_cache()
            if not products:
                log("⚠️  Кеш порожній → FULL")
                products = await fetch_all_products_full(client, sid, list(fetch_ids), base["lang"])
            else:
                prices = await fetch_prices_only(client, sid, list(fetch_ids), base["lang"])
                
                # 1. Визначаємо нові товари, яких немає у кеші
                existing_pids = {int(p.get("productID") or p.get("id") or 0) for p in products}
                todo_new = []
                for pid, p in prices.items():
                    if pid not in existing_pids:
                        is_archive = is_true(p.get("is_archive", False))
                        has_stock_field = ("stocks" in p) or ("available" in p)
                        if not (is_archive or (has_stock_field and stock_qty(p) == 0)):
                            todo_new.append(p)
                
                # 2. Збагачуємо нові товари (Фаза 2)
                if todo_new:
                    log(f"🆕 Знайдено {len(todo_new)} нових товарів у QUICK. Завантажуємо повні дані...")
                    current_sid = _SID_STATE["sid"] or sid
                    enriched_new = await enrich_products(client, current_sid, todo_new)
                    products.extend(enriched_new)
                
                # 3. Оновлюємо ціни для старих товарів
                products = apply_prices_to_cache(products, prices)
                
                # 4. Зберігаємо свіжий кеш
                save_cache(products)

        results = build_all_feeds(products, all_cats, feeds, base["shop_url"])
        _export_link_block(results)
    finally:
        await logout(client, sid)


async def main():
    base  = load_base_config()
    feeds = load_feeds(base)
    mode  = base.get("mode", "quick")

    # MERGE — єдина стадія без мережі до Brain (працює з файлами)
    if EXPORT_STAGE == "merge":
        log("=" * 55); log("  Brain Exporter — стадія MERGE"); log("=" * 55)
        stage_merge(base, feeds)
        return

    login    = base.get("login", "")
    password = base.get("password", "")
    if not login or not password:
        log("❌ Не задані BRAIN_LOGIN / BRAIN_PASSWORD"); sys.exit(1)
    _SID_STATE["login"], _SID_STATE["password"] = login, password
    # спільний SID із setup-джоба (shard-и не логіняться повторно)
    if os.environ.get("BRAIN_SID"):
        _SID_STATE["sid"] = os.environ["BRAIN_SID"].strip()
        log("🔑 Використовую спільний BRAIN_SID із setup-джоба")

    all_selected = set()
    for f in feeds:
        all_selected |= set(f["category_ids"])
    if EXPORT_STAGE in ("solo",) and not all_selected:
        log("❌ Жоден фід не має вибраних категорій"); sys.exit(1)

    log("=" * 55)
    log(f"  Brain API → XML Exporter v5.0 (sharded)")
    log(f"  Стадія: {EXPORT_STAGE.upper()} | Режим: {'FULL' if mode=='full' else 'QUICK'} "
        f"| Shard: {SHARD_INDEX}/{SHARD_TOTAL} | Фідів: {len(feeds)}")
    log("=" * 55)

    if EXPORT_STAGE == "solo" and mode == "quick" and not CACHE_FILE.exists():
        log("⚠️  Кеш не знайдено — перемикаємось на FULL")
        mode = "full"

    async with httpx.AsyncClient(
        timeout=30,
        limits=httpx.Limits(max_connections=15, max_keepalive_connections=10),
    ) as client:
        if EXPORT_STAGE == "setup":
            await stage_setup(client, base, feeds)
        elif EXPORT_STAGE == "shard":
            await stage_shard(client, base, feeds)
        else:
            await stage_solo(client, base, feeds, mode)


if __name__ == "__main__":
    asyncio.run(main())
