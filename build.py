#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генератор лендинга ОПИФ «Матрёшка а-ля Рус».

Собирает самодостаточный index.html из:
  - данных доходности        — ../matryoshka-tracker/matryoshka.db (пай, СЧА, индекс MCFTR)
  - структуры портфеля       — ../onepager-matryoshka/входные-данные/structure.json
  - параметров выпуска        — ../onepager-matryoshka/входные-данные/параметры.yaml
  - ассетов (лого/фото/QR)    — ./ассеты/
  - PDF-отчётов              — ./документы/  (секция «Документы» заполняется автоматически)

Все картинки встраиваются base64 — файл открывается офлайн и переносится одним куском.
Запуск:  python3 build.py   →   index.html
"""
import base64
import datetime as dt
import json
import mimetypes
import os
import re
import sqlite3
import urllib.request
from pathlib import Path

# ── пути ────────────────────────────────────────────────────────────────────
# Пути можно переопределить переменными окружения (нужно для сборки в CI, где
# соседних папок matryoshka-tracker/onepager-matryoshka нет). Без переменных —
# работают прежние значения относительно этой папки, локальная сборка не меняется.
ROOT = Path(__file__).resolve().parent


def _path(env_name, default):
    v = os.environ.get(env_name)
    return Path(v).expanduser() if v else default


# каталог с входными данными one-pager (structure.json, параметры.yaml).
# В репозитории лендинга их копия лежит в данные-фонда/; локально по умолчанию
# берём из соседнего проекта one-pager.
_DATA_DEFAULT = (ROOT / "данные-фонда") if (ROOT / "данные-фонда").exists() \
    else (ROOT.parent / "onepager-matryoshka" / "входные-данные")
DATA_DIR = _path("ONEPAGER_DIR", _DATA_DEFAULT)

DB = _path("MATRYOSHKA_DB", ROOT.parent / "matryoshka-tracker" / "matryoshka.db")
STRUCTURE = DATA_DIR / "structure.json"
PARAMS = DATA_DIR / "параметры.yaml"
ASSETS = _path("ASSETS_DIR", ROOT / "ассеты")
DOCS = _path("DOCS_DIR", ROOT / "документы")
OUT = _path("OUT", ROOT / "index.html")

MONTHS_RU = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]

# Место фонда в рейтинге доходных фондов акций investfunds.ru.
# Страница серверная (без JS), позиция берётся из атрибута data-index-number —
# при сборке подтягивается автоматически (fetch_rank). RANK_EQUITY — запасное
# значение на случай, если сайт недоступен или сменил разметку.
RANK_URL = ("https://investfunds.ru/funds/?qual=on&scroll_to_table=1&page=1&limit=50"
            "&sort=delta_pay_1y&order=desc&type=0-3m&obj=0-2&stat=0-2"
            "&spec=1-b3jrb4.2-2yiqi2.3-2e5on6")
FUND_ID = "12035"      # id «Матрёшка а-ля Рус» на investfunds.ru
RANK_EQUITY = "№1"    # запасное значение


# ── утилиты форматирования ───────────────────────────────────────────────────
def ruf(x, digits=2, sign=False):
    """Число по-русски: разделитель — запятая, тысячи — узкий пробел."""
    s = f"{abs(x):,.{digits}f}".replace(",", " ").replace(".", ",")
    if sign:
        return ("+" if x >= 0 else "−") + s
    return ("−" if x < 0 else "") + s


def ru_date(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {MONTHS_RU[d.month]} {d.year}"


def data_uri(path):
    path = Path(path)
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


# ── чтение параметров.yaml (мини-парсер «ключ: значение») ─────────────────────
def read_params():
    p = {}
    if not PARAMS.exists():
        return p
    for line in PARAMS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.split("#", 1)[0].strip()  # отрезаем инлайн-комментарий
        p[key.strip()] = val
    return p


# ── место в рейтинге фондов акций (investfunds.ru) ───────────────────────────
def fetch_rank(timeout=15):
    """Позиция фонда в рейтинге доходных фондов акций investfunds.ru → "№ N".
    Таблица серверная: позиция = data-index-number строки с нужным /funds/ID/.
    При любой сетевой ошибке или отсутствии фонда → None (сработает RANK_EQUITY)."""
    try:
        req = urllib.request.Request(
            RANK_URL, headers={"User-Agent": "matryoshka-landing/1.0 (+build.py)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            h = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    for idx, fid in re.findall(r'data-index-number="(\d+)".*?/funds/(\d+)/', h, re.S):
        if fid == FUND_ID:
            return f"№{int(idx) + 1}"
    return None


# ── доходности из БД ─────────────────────────────────────────────────────────
def load_series():
    con = sqlite3.connect(DB)
    fund = con.execute(
        "SELECT date, share_cost, nav FROM fund ORDER BY date").fetchall()
    index = con.execute(
        "SELECT date, close FROM index_value WHERE secid='MCFTR' ORDER BY date"
    ).fetchall()
    con.close()
    return fund, index


def compute_returns(fund, index):
    """Список строк (label, fund%, index%, diff pp) по интервалам."""
    f = {r[0]: r[1] for r in fund}
    i = {r[0]: r[1] for r in index}
    fdates = [r[0] for r in fund]
    common = [d for d in fdates if d in i]  # общие торговые дни
    last = fdates[-1]
    ld = dt.date.fromisoformat(last)

    def first_on_or_after(dates, start):
        for d in dates:
            if d >= start:
                return d
        return None

    def last_before(dates, start):
        prev = None
        for d in dates:
            if d < start:
                prev = d
            else:
                break
        return prev

    # prior=True — база берётся из последней точки ДО start (закрытие пред. периода).
    # Для YTD это последнее значение прошлого года, т.е. рост от закрытия 31.12,
    # а не от первого торгового дня нового года. См. build_chart.py трекера.
    def ret(series, dates, start, prior=False):
        s = last_before(dates, start) if prior else first_on_or_after(dates, start)
        if not s:
            return None, None
        return (series[dates[-1]] / series[s] - 1) * 100, s

    specs = [
        ("1 месяц", str(ld - dt.timedelta(days=30)), False),
        ("3 месяца", str(ld - dt.timedelta(days=91)), False),
        ("6 месяцев", str(ld - dt.timedelta(days=182)), False),
        ("За год", str(ld.replace(year=ld.year - 1)), False),
        ("С начала года", f"{ld.year}-01-01", True),
        ("С запуска", fdates[0], False),
    ]
    rows = []
    for label, start, prior in specs:
        fr, _ = ret(f, fdates, start, prior)
        ir, _ = ret(i, common, start, prior)  # индекс — по общим торговым дням фонда
        if fr is None or ir is None:
            continue
        rows.append((label, fr, ir, fr - ir))
    return rows, last


# ── сборка HTML ──────────────────────────────────────────────────────────────
def build():
    params = read_params()
    struct = json.loads(STRUCTURE.read_text(encoding="utf-8")) if STRUCTURE.exists() else {}
    fund, index = load_series()
    rows, last = compute_returns(fund, index)

    live_rank = fetch_rank()
    rank = live_rank or RANK_EQUITY
    print(f"  место в рейтинге: {rank}"
          f"{' (онлайн)' if live_rank else ' (запасное — investfunds недоступен)'}")

    manager = params.get("управляющий", "Управляющий фонда")
    position = params.get("должность", "Портфельный управляющий")
    stocks = params.get("доля_акций") or next(
        (str(round(t["percent"])) for t in struct.get("types", []) if t["name"] == "Акции"), "—")
    bonds = params.get("доля_облигаций") or "—"
    try:
        stocks_n = float(str(stocks).replace(",", "."))
        bonds_n = float(str(bonds).replace(",", "."))
    except ValueError:
        stocks_n, bonds_n = 92.0, 8.0

    isin = struct.get("isin", "RU000A10B917")
    reg = struct.get("registrationNumber", "6931")
    struct_date = struct.get("structureDate", last)
    nav_rub = struct.get("nav", {}).get("nav")
    share = dict((r[0], r[1]) for r in fund)[last]

    # KPI для hero — из строки «С запуска»
    launch = next((r for r in rows if r[0] == "С запуска"), rows[0])
    ytd = next((r for r in rows if r[0] == "С начала года"), None)
    ltm = next((r for r in rows if r[0] == "За год"), launch)  # доходность за 12 мес (LTM)

    def dot(iso):
        d = dt.date.fromisoformat(iso)
        return f"{d.day:02d}.{d.month:02d}.{d.year}"

    ld = dt.date.fromisoformat(last)
    ltm_from = dot(str(ld.replace(year=ld.year - 1)))
    ltm_to = dot(last)

    # ── таблица доходностей ──
    tr = []
    for label, fr, ir, diff in rows:
        fc = "pos" if fr >= 0 else "neg"
        ic = "pos" if ir >= 0 else "neg"
        rl = f'{label} ({dot(fund[0][0])})' if label == "С запуска" else label
        tr.append(
            f'<tr><td class="rl">{rl}</td>'
            f'<td class="{fc}">{ruf(fr, 2, True)}%</td>'
            f'<td class="{ic}">{ruf(ir, 2, True)}%</td>'
            f'<td class="diff">{ruf(diff, 2, True)} п.п.</td></tr>'
        )
    returns_table = "\n".join(tr)

    # ── топ-10 эмитентов ──
    issuers = struct.get("issuers", [])
    maxp = max((x["percent"] for x in issuers), default=1)
    top_rows = []
    for it in issuers:
        w = it["percent"] / maxp * 100
        top_rows.append(
            f'<li><span class="hn">{it["name"]}</span>'
            f'<span class="hbar"><span style="width:{w:.1f}%"></span></span>'
            f'<span class="hv">{ruf(it["percent"], 2)}%</span></li>'
        )
    top10 = "\n".join(top_rows)

    # ── сектора ──
    sec_rows = []
    for s in struct.get("sectors", []):
        sec_rows.append(
            f'<li><span class="sn">{s["name"]}</span>'
            f'<span class="sv">{ruf(s["percent"], 2)}%</span></li>')
    sectors = "\n".join(sec_rows)

    # ── документы (авто из папки) ──
    # имена вида "ГГГГ-ММ-ДД Заголовок.pdf": префикс задаёт порядок (свежие
    # первыми) и в заголовке не показывается.
    pdfs = sorted(DOCS.glob("*.pdf"), reverse=True) if DOCS.exists() else []
    if pdfs:
        doc_items = []
        for p in pdfs:
            title = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", p.stem)
            size = p.stat().st_size / 1024
            unit = f"{size/1024:.1f} МБ" if size > 1024 else f"{size:.0f} КБ"
            doc_items.append(
                f'<a class="doc" href="документы/{p.name}" target="_blank" rel="noopener">'
                f'<span class="doc-ic">PDF</span>'
                f'<span class="doc-body"><span class="doc-t">{title}</span>'
                f'<span class="doc-m">{unit}</span></span></a>')
        docs_html = "\n".join(doc_items)
    else:
        docs_html = (
            '<div class="placeholder">Отчёты появятся здесь. Чтобы добавить документ, '
            'положите PDF в папку <code>документы/</code> и пересоберите страницу '
            '(<code>python3 build.py</code>).</div>')

    # ── данные для графика ──
    chart_data = json.dumps({
        "fund": [{"d": r[0], "v": r[1], "nav": r[2]} for r in fund],
        "index": [{"d": r[0], "v": r[1]} for r in index],
    }, ensure_ascii=False, separators=(",", ":"))

    # ── ассеты base64 ──
    logo = data_uri(ASSETS / "znak.png")
    photo = data_uri(ASSETS / "AuthorPhoto.jpg")
    qr_fin = data_uri(ASSETS / "qrFinuslugi.png")
    qr_alfa = data_uri(ASSETS / "qrAlfa.png")
    logo_fin = data_uri(ASSETS / "Finuslugi.png")
    logo_alfa = data_uri(ASSETS / "Alfacapital.png")

    repl = {
        "%%LOGO%%": logo,
        "%%PHOTO%%": photo,
        "%%QR_FIN%%": qr_fin,
        "%%QR_ALFA%%": qr_alfa,
        "%%LOGO_FIN%%": logo_fin,
        "%%LOGO_ALFA%%": logo_alfa,
        "%%CHART_DATA%%": chart_data,
        "%%MANAGER%%": manager,
        "%%POSITION%%": position,
        "%%ISIN%%": isin,
        "%%REG%%": reg,
        "%%STRUCT_DATE%%": ru_date(struct_date),
        "%%ASOF%%": ru_date(last),
        "%%RANK%%": rank,
        "%%SHARE%%": ruf(share, 2),
        "%%NAV%%": ruf((fund[-1][2] or nav_rub) / 1_000_000, 1),  # СЧА из БД (ежедневно), снимок онепейджера — запасной
        "%%KPI_FUND%%": ruf(launch[1], 2, True),
        "%%KPI_INDEX%%": ruf(launch[2], 2, True),
        "%%KPI_DIFF%%": ruf(launch[3], 1, True),
        "%%KPI_YTD%%": ruf(ytd[1], 2, True) if ytd else "—",
        "%%LTM%%": ruf(ltm[1], 2, True),
        "%%LTM_FROM%%": ltm_from,
        "%%LTM_TO%%": ltm_to,
        "%%RETURNS_TABLE%%": returns_table,
        "%%TOP10%%": top10,
        "%%SECTORS%%": sectors,
        "%%DOCS%%": docs_html,
        "%%STOCKS%%": ruf(stocks_n, 0),
        "%%BONDS%%": ruf(bonds_n, 0),
        "%%STOCKS_W%%": f"{stocks_n:.1f}",
        "%%BONDS_W%%": f"{bonds_n:.1f}",
        "%%GENERATED%%": ru_date(last),
    }

    html = TEMPLATE
    for k, v in repl.items():
        html = html.replace(k, str(v))
    OUT.write_text(html, encoding="utf-8")
    print(f"✓ {OUT}  ({OUT.stat().st_size/1024:.0f} КБ)  данные на {ru_date(last)}")


# ── HTML-шаблон ──────────────────────────────────────────────────────────────
TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ОПИФ «Матрёшка а-ля Рус» — активно управляемый фонд российских акций</title>
<meta name="description" content="Активно управляемый открытый ПИФ российских дивидендных акций. С запуска фонд опережает индекс МосБиржи полной доходности на %%KPI_DIFF%% п.п.">
<link rel="canonical" href="https://www.matreshkarusfund.ru/">
<!-- лого на вкладке браузера (встроено base64, как и остальные картинки) -->
<link rel="icon" type="image/png" href="%%LOGO%%">
<link rel="apple-touch-icon" href="%%LOGO%%">
<!-- превью при отправке ссылки в мессенджеры/соцсети (Open Graph + Twitter).
     og:image — обязательно абсолютный URL реального файла (data-URI краулеры не читают). -->
<meta property="og:type" content="website">
<meta property="og:site_name" content="ОПИФ «Матрёшка а-ля Рус»">
<meta property="og:locale" content="ru_RU">
<meta property="og:url" content="https://www.matreshkarusfund.ru/">
<meta property="og:title" content="ОПИФ «Матрёшка а-ля Рус» — активно управляемый фонд российских акций">
<meta property="og:description" content="Активно управляемый открытый ПИФ российских дивидендных акций. С запуска фонд опережает индекс МосБиржи полной доходности на %%KPI_DIFF%% п.п.">
<meta property="og:image" content="https://www.matreshkarusfund.ru/og-image.png">
<meta property="og:image:type" content="image/png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="ОПИФ «Матрёшка а-ля Рус» — активно управляемый фонд российских акций">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="ОПИФ «Матрёшка а-ля Рус» — активно управляемый фонд российских акций">
<meta name="twitter:description" content="Активно управляемый открытый ПИФ российских дивидендных акций. С запуска фонд опережает индекс МосБиржи полной доходности на %%KPI_DIFF%% п.п.">
<meta name="twitter:image" content="https://www.matreshkarusfund.ru/og-image.png">
<style>
:root{
  --purple:#442B77; --purple-mid:#624997; --purple-lt:#9380BA; --tint:#F4F0FF;
  --orange:#F49C2D; --ink:#1F232D; --gray:#57595B; --gray-lt:#B9BCC2;
  --border:#E7E3F0; --bg:#FBFAFF; --card:#FFFFFF;
  --pos:#0F9140; --neg:#D03B3B;
  --shadow:0 1px 2px rgba(68,43,119,.06),0 8px 30px rgba(68,43,119,.08);
  --radius:18px;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{font-family:"Nunito",ui-rounded,"SF Pro Rounded","Segoe UI",Arial,sans-serif;
  color:var(--ink);background:var(--bg);line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
img{max-width:100%;display:block}
.wrap{max-width:1120px;margin:0 auto;padding:0 22px}
section{padding:76px 0;scroll-margin-top:74px}
.eyebrow{font-size:13px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:var(--purple-lt)}
h2.sec{font-size:clamp(26px,3.4vw,38px);color:var(--purple);line-height:1.08;margin:8px 0 14px;font-weight:800;letter-spacing:-.01em}
.lead{font-size:17px;color:var(--gray);max-width:680px}
.grid{display:grid;gap:18px}
@media(min-width:720px){.g2{grid-template-columns:repeat(2,minmax(0,1fr))}.g3{grid-template-columns:repeat(3,minmax(0,1fr))}.g4{grid-template-columns:repeat(4,minmax(0,1fr))}}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:26px;box-shadow:var(--shadow)}
.btn{display:inline-flex;align-items:center;gap:9px;font-weight:800;font-size:16px;
  padding:14px 26px;border-radius:999px;border:0;cursor:pointer;transition:.18s transform,.18s box-shadow}
.btn-primary{background:linear-gradient(135deg,var(--orange),#ffb454);color:#3a2500;
  box-shadow:0 8px 22px rgba(244,156,45,.4)}
.btn-primary:hover{transform:translateY(-2px);box-shadow:0 12px 28px rgba(244,156,45,.5)}
.btn-ghost{background:#fff;color:var(--purple);border:1.5px solid var(--border)}
.btn-ghost:hover{border-color:var(--purple-lt)}

/* ── NAV ── */
.nav{position:sticky;top:0;z-index:50;background:rgba(251,250,255,.86);backdrop-filter:blur(12px);
  border-bottom:1px solid var(--border)}
.nav .wrap{display:flex;align-items:center;gap:22px;height:64px}
.brand{display:flex;align-items:center;gap:11px;font-weight:800;color:var(--purple);font-size:17px}
.brand img{width:34px;height:34px}
.nav-links{display:none;gap:22px;margin-left:auto;font-size:15px;font-weight:700;color:var(--gray)}
.nav-links a:hover{color:var(--purple)}
.nav .btn{margin-left:auto;padding:9px 20px;font-size:14px}
@media(min-width:940px){.nav-links{display:flex}.nav .btn{margin-left:22px}}

/* ── HERO (структура — с референса, палитра — брендовая) ── */
.hero{position:relative;overflow:hidden;padding:58px 0 44px;
  background:radial-gradient(1100px 480px at 82% -12%,var(--tint),transparent 62%),var(--bg)}
.hero-grid{display:grid;gap:34px;align-items:center}
@media(min-width:900px){.hero-grid{grid-template-columns:1.08fr .92fr}}
.hero .pill{display:inline-block;background:#fff;border:1px solid var(--border);border-radius:999px;
  padding:9px 20px;font-weight:800;font-size:15px;color:var(--purple);box-shadow:var(--shadow)}
.hero-return{display:flex;align-items:flex-start;gap:14px;margin:14px 0 14px}
.hero-rank{display:flex;align-items:flex-start;gap:13px;margin:0}
.hero-rank .big{font-size:clamp(34px,5vw,54px);font-weight:800;color:var(--purple);line-height:.9;letter-spacing:-.02em}
.hero-rank .cap{font-size:14px;color:var(--gray);line-height:1.35;max-width:210px;padding-top:6px}
.hero-cta{display:flex;align-items:center;flex-wrap:wrap;gap:28px;margin:26px 0 0}
.hero-return .big{font-size:clamp(46px,7.4vw,78px);font-weight:800;color:var(--pos);line-height:.9;letter-spacing:-.02em}
.hero-return .rub{font-size:clamp(38px,5.6vw,60px);font-weight:700;color:var(--gray-lt);line-height:1}
.hero-return .per{font-size:14px;color:var(--gray);line-height:1.4;padding-top:6px}
.hero-return .per b{color:var(--ink);font-weight:800;white-space:nowrap}
.hero h1{font-size:clamp(32px,5vw,58px);line-height:1.02;color:var(--purple);font-weight:800;letter-spacing:-.02em;margin:18px 0 10px;white-space:nowrap}
.hero p.tag{font-size:19px;color:var(--gray);max-width:520px;margin-bottom:6px}
.hero-visual{position:relative;display:flex;justify-content:center;align-items:center}
.hero-visual::before{content:"";position:absolute;width:74%;aspect-ratio:1;border-radius:50%;
  background:radial-gradient(circle,var(--tint),transparent 70%)}
.hero-logo{width:100%;max-width:340px;height:auto;position:relative;
  filter:drop-shadow(0 22px 46px rgba(68,43,119,.22))}

/* ── обзорная лента фактов ── */
.facts{display:grid;gap:1px;background:var(--border);border:1px solid var(--border);
  border-radius:var(--radius);overflow:hidden;margin-top:8px}
@media(min-width:640px){.facts{grid-template-columns:repeat(4,1fr)}}
.facts div{background:#fff;padding:20px 22px}
.facts .v{font-size:22px;font-weight:800;color:var(--purple)}
.facts .l{font-size:13px;color:var(--gray)}

/* ── управляющий ── */
.mgr{display:grid;gap:34px;align-items:center}
@media(min-width:780px){.mgr{grid-template-columns:340px 1fr}}
.mgr-photo{border-radius:22px;overflow:hidden;box-shadow:var(--shadow);border:1px solid var(--border)}
.mgr-photo img{width:100%;aspect-ratio:2/3;object-fit:cover;object-position:center top;display:block}
.mgr-list{list-style:none;margin:18px 0 0;padding:0;display:grid;gap:10px}
.mgr-list li{position:relative;background:var(--tint);border:1px solid var(--border);border-radius:14px;
  padding:13px 16px 13px 48px;font-size:15px;line-height:1.5;color:var(--ink)}
.mgr-list li::before{content:"✓";position:absolute;left:14px;top:12px;width:22px;height:22px;border-radius:50%;
  background:var(--purple);color:#fff;font-size:12px;font-weight:800;display:flex;align-items:center;justify-content:center}
.mgr h3{font-size:26px;color:var(--purple);font-weight:800}
.mgr .role{color:var(--orange);font-weight:800;margin-bottom:14px}
.mgr p{color:var(--gray);margin-bottom:12px}
.quote{border-left:3px solid var(--orange);padding:6px 0 6px 18px;font-size:18px;color:var(--ink);font-weight:600;font-style:italic;margin:16px 0}

/* ── карточки-иконки ── */
.feature{display:grid;grid-template-columns:auto 1fr;align-items:center;column-gap:14px;row-gap:10px}
.feature .ic{width:46px;height:46px;border-radius:13px;background:var(--tint);color:var(--purple);
  display:flex;align-items:center;justify-content:center;font-size:22px}
.feature h3{font-size:18px;color:var(--purple);font-weight:800;margin:0}
.feature p{grid-column:1 / -1;font-size:15px;color:var(--gray);margin:0}

/* ── стратегия / аллокация ── */
.split{height:26px;border-radius:9px;overflow:hidden;display:flex;font-size:13px;font-weight:800;color:#fff;margin:6px 0 4px}
.split .st{background:linear-gradient(90deg,var(--purple),var(--purple-mid));display:flex;align-items:center;padding-left:12px}
.split .bo{background:var(--orange);display:flex;align-items:center;padding-left:12px}
.holdings{list-style:none}
.holdings li{display:grid;grid-template-columns:1fr 90px 62px;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--border);font-size:14px}
.holdings li:last-child{border:0}
.holdings .hn{font-weight:600}
.holdings .hbar{height:8px;background:var(--tint);border-radius:5px;overflow:hidden}
.holdings .hbar span{display:block;height:100%;background:linear-gradient(90deg,var(--purple-mid),var(--purple-lt))}
.holdings .hv{text-align:right;font-weight:800;color:var(--purple)}
.sectors{list-style:none;margin-top:6px}
.sectors li{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border);font-size:14px}
.sectors li:last-child{border:0}
.sectors .sv{font-weight:800;color:var(--purple)}

/* ── стратегия: идея / анализ / аллокация ── */
.sub-h{font-size:clamp(20px,2.4vw,26px);color:var(--purple);font-weight:800;margin:46px 0 8px}
.idea .idea-top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px}
.idea .idea-top h3{font-size:20px;color:var(--purple);font-weight:800}
.idea .chk{color:var(--purple-mid);font-size:22px;line-height:1}
.idea p{color:var(--gray);font-size:15px}
.funnel-grid{display:grid;gap:28px;align-items:center;margin-top:14px}
@media(min-width:780px){.funnel-grid{grid-template-columns:240px 1fr}}
.funnel-doll{display:flex;flex-direction:column;align-items:center;gap:8px}
.funnel-doll svg{width:200px;max-width:100%;height:auto;filter:drop-shadow(0 14px 30px rgba(68,43,119,.18))}
.funnel-doll .dir{font-size:13px;font-weight:800;color:var(--purple-mid)}
.steps{list-style:none;display:grid;gap:14px}
.steps li{position:relative;padding-left:44px;color:var(--gray);font-size:15px;line-height:1.5}
.steps li b{color:var(--ink)}
.steps li .n{position:absolute;left:0;top:0;width:30px;height:30px;border-radius:9px;color:#fff;
  font-weight:800;display:flex;align-items:center;justify-content:center;font-size:15px}
.alloc-c{display:flex;flex-direction:column}
.alloc-head{display:flex;align-items:center;gap:16px;margin-bottom:14px}
.alloc-head .doll-mini{height:104px;display:flex;align-items:center;flex:0 0 auto}
.alloc-head .doll{width:auto}
.alloc-head .doll.big{height:104px}.alloc-head .doll.mid{height:88px}.alloc-head .doll.sm{height:72px}
.alloc-meta{flex:1 1 auto;min-width:0;text-align:center}
.alloc-meta h4{font-size:clamp(18px,2vw,22px);color:var(--purple);font-weight:800;margin:0 0 6px;line-height:1.1}
.alloc-meta .pct{font-size:clamp(26px,3vw,32px);font-weight:800;color:var(--purple);line-height:1}
.alloc-meta .pct span{display:block;font-size:13px;color:var(--gray);font-weight:700;margin-top:3px}
.alloc-c p{font-size:14px;color:var(--gray)}

/* ── график ── */
.chart-card{padding:24px}
.chart-top{display:flex;flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between;margin-bottom:14px}
.seg{display:inline-flex;background:var(--tint);border-radius:11px;padding:4px;gap:2px}
.seg button{border:0;background:transparent;font:inherit;font-weight:800;font-size:13px;color:var(--purple-mid);
  padding:7px 13px;border-radius:8px;cursor:pointer}
.seg button.on{background:#fff;color:var(--purple);box-shadow:0 1px 3px rgba(68,43,119,.18)}
.legend{display:flex;gap:18px;font-size:13px;font-weight:700;color:var(--gray);margin-bottom:8px}
.legend i{display:inline-block;width:14px;height:4px;border-radius:2px;margin-right:6px;vertical-align:middle}
.chart-box{position:relative;width:100%}
#chart{width:100%;height:auto;display:block;touch-action:none}
.tip{position:absolute;pointer-events:none;background:#fff;border:1px solid var(--border);border-radius:10px;
  padding:8px 11px;font-size:12px;box-shadow:var(--shadow);opacity:0;transition:opacity .1s;white-space:nowrap;z-index:5}
.tip b{color:var(--purple)}
.results-grid{display:grid;gap:18px;margin-top:18px}
@media(min-width:860px){.results-grid{grid-template-columns:1.35fr 1fr}}
table.ret{width:100%;border-collapse:collapse;font-size:14px}
table.ret th,table.ret td{padding:11px 10px;text-align:right;border-bottom:1px solid var(--border)}
table.ret th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--gray);font-weight:800}
table.ret td.rl,table.ret th:first-child{text-align:left;font-weight:700}
table.ret .pos{color:var(--pos);font-weight:800}
table.ret .neg{color:var(--neg);font-weight:800}
table.ret .diff{color:var(--purple);font-weight:800}
.mini{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.mini .m{background:var(--tint);border-radius:14px;padding:16px}
.mini .m .l{font-size:12px;color:var(--gray);font-weight:700}
.mini .m .v{font-size:26px;font-weight:800;color:var(--purple)}

/* ── инвестировать ── */
.ways{counter-reset:w}
.way{display:grid;gap:20px;align-items:center;padding:26px;border:1px solid var(--border);border-radius:var(--radius);
  background:#fff;box-shadow:var(--shadow);margin-bottom:16px}
@media(min-width:720px){.way{grid-template-columns:120px 1fr auto}}
.way .num{counter-increment:w;position:relative}
.way .num::before{content:counter(w);position:absolute;top:-8px;left:-6px;font-size:70px;font-weight:800;color:var(--tint);line-height:1;z-index:0}
.way .brandbox{width:120px;height:120px;border:1px solid var(--border);border-radius:14px;background:#fff;position:relative;z-index:1;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:9px;padding:10px;text-align:center}
.way .brandbox .mark{width:54px;height:54px;border-radius:13px;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:31px;line-height:1}
.way .brandbox .mark svg{width:30px;height:30px;display:block}
.way .brandbox .lbl{font-size:12px;font-weight:800;line-height:1.12;letter-spacing:.005em}
.way .brandbox .brandlogo{max-width:100%;max-height:74px;object-fit:contain;display:block}
.way h3{font-size:20px;color:var(--purple);font-weight:800;margin-bottom:4px}
.way p{color:var(--gray);font-size:15px}
.way ol{margin:8px 0 0 18px;color:var(--gray);font-size:14px}
.way .isin{display:inline-block;margin-top:8px;font-weight:800;color:var(--purple);background:var(--tint);
  padding:5px 12px;border-radius:8px;letter-spacing:.04em;font-size:14px}
.way .act{display:grid;gap:12px;justify-items:center;white-space:nowrap}
.way .act .btn{font-size:13px;padding:11px 21px;gap:7px}
.way .qrmini{display:grid;gap:6px;justify-items:center}
.way .qrmini img{width:96px;height:96px;display:block;border:1px solid var(--border);border-radius:12px;padding:6px;background:#fff}
.way .qrmini .cap{font-size:11px;color:var(--gray);white-space:normal;max-width:110px;line-height:1.25}

/* ── СМИ ── */
.press{display:grid;gap:22px}
@media(min-width:820px){.press{grid-template-columns:1.35fr 1fr;align-items:start}}
.press-video{position:relative;aspect-ratio:16/9;border-radius:var(--radius);overflow:hidden;background:#000;box-shadow:var(--shadow)}
.press-video iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
.press-list{display:grid;gap:14px}
.press-list .p{display:block;border:1px solid var(--border);border-radius:14px;padding:16px 18px;background:#fff;box-shadow:var(--shadow);text-decoration:none;transition:.18s transform,.18s box-shadow}
.press-list .p:hover{transform:translateY(-2px);box-shadow:0 12px 28px rgba(0,0,0,.09)}
.press-list .p .src{font-size:13px;font-weight:800;color:var(--purple-lt)}
.press-list .p .t{font-weight:700;margin:6px 0 8px;color:var(--ink);line-height:1.35}
.press-list .p .lnk{font-size:13px;color:var(--purple);font-weight:700}

/* ── документы ── */
.docs{display:grid;gap:14px}
@media(min-width:720px){.docs{grid-template-columns:repeat(2,1fr)}}
.doc{display:flex;gap:14px;align-items:center;border:1px solid var(--border);border-radius:14px;padding:16px;background:#fff;box-shadow:var(--shadow);transition:.15s}
.doc:hover{border-color:var(--purple-lt);transform:translateY(-1px)}
.doc-ic{flex:0 0 auto;width:44px;height:44px;border-radius:10px;background:var(--tint);color:var(--purple);
  display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px}
.doc-t{font-weight:700;color:var(--ink)}
.doc-m{font-size:13px;color:var(--gray)}
.placeholder{border:1px dashed var(--gray-lt);border-radius:var(--radius);padding:26px;color:var(--gray);background:#fff}
.placeholder code{background:var(--tint);padding:2px 7px;border-radius:6px;color:var(--purple);font-weight:700}

/* ── FAQ ── */
.faq details{border:1px solid var(--border);border-radius:14px;background:#fff;margin-bottom:12px;overflow:hidden}
.faq summary{list-style:none;cursor:pointer;padding:18px 22px;font-weight:800;color:var(--purple);
  display:flex;justify-content:space-between;align-items:center;gap:16px;font-size:16px}
.faq summary::-webkit-details-marker{display:none}
.faq summary::after{content:"+";font-size:24px;color:var(--purple-lt);font-weight:700;transition:.2s}
.faq details[open] summary::after{transform:rotate(45deg)}
.faq details p{padding:0 22px 20px;color:var(--gray);font-size:15px}

/* ── контакты ── */
.contacts{display:grid;gap:16px}
@media(min-width:720px){.contacts{grid-template-columns:1fr 1fr}}
.contact-item{display:flex;gap:14px;align-items:flex-start;padding:18px;border:1px solid var(--border);border-radius:14px;background:#fff}
.contact-item .ic{width:42px;height:42px;border-radius:11px;background:var(--tint);color:var(--purple);display:flex;align-items:center;justify-content:center;font-size:20px;flex:0 0 auto}
.contact-item .l{font-size:13px;color:var(--gray)}
.contact-item .v{font-weight:800;color:var(--purple);font-size:17px}
.contact-item .v a{color:inherit;text-decoration:none}
.contact-item .v a:hover{text-decoration:underline}
.contact-item .v.ph{color:var(--gray-lt)}

/* ── footer / disclaimer ── */
footer{background:var(--purple);color:#e9e2fb;padding:52px 0 34px;margin-top:20px}
footer .wrap{display:grid;gap:26px}
footer .top{display:flex;flex-wrap:wrap;gap:20px;align-items:center;justify-content:space-between}
footer .brand{color:#fff}
footer .disc{font-size:12px;line-height:1.6;color:#b9a9df;border-top:1px solid rgba(255,255,255,.14);padding-top:22px}
footer .disc b{color:#fff}
footer .disc p{margin:10px 0 0}
.tag-line{color:#ccbdf0;font-size:14px;max-width:560px}
.note{font-size:12px;color:var(--gray);margin-top:12px}
</style>
</head>
<body>

<!-- ═══ NAV ═══ -->
<nav class="nav">
  <div class="wrap">
    <a class="brand" href="#top"><img src="%%LOGO%%" alt="">Матрёшка&nbsp;а-ля&nbsp;Рус</a>
    <div class="nav-links">
      <a href="#manager">Управляющий</a>
      <a href="#strategy">Стратегия</a>
      <a href="#results">Результаты</a>
      <a href="#invest">Как купить</a>
      <a href="#faq">Вопросы</a>
    </div>
    <a class="btn btn-primary" href="#invest">Инвестировать</a>
  </div>
</nav>

<!-- ═══ 1. HERO ═══ -->
<header class="hero" id="top">
  <div class="wrap hero-grid">
    <div>
      <span class="pill">Открытый паевой инвестиционный фонд</span>
      <h1>Матрёшка&nbsp;а-ля&nbsp;Рус</h1>
      <p class="tag">Активно управляемый фонд на акции компаний в России</p>
      <div class="hero-return">
        <span class="big">%%LTM%%%</span>
        <span class="rub">₽</span>
        <span class="per">за 12 месяцев<br><b>%%LTM_FROM%% — %%LTM_TO%%</b></span>
      </div>
      <div class="hero-cta">
        <div class="hero-rank">
          <span class="big">%%RANK%%</span>
          <span class="cap">по доходности среди публичных фондов акций</span>
        </div>
        <a class="btn btn-primary" href="#invest">Инвестировать</a>
      </div>
    </div>
    <div class="hero-visual">
      <img class="hero-logo" src="%%LOGO%%" alt="Логотип фонда «Матрёшка а-ля Рус»">
    </div>
  </div>
</header>

<!-- ═══ ДЛЯ КОГО ЭТОТ ФОНД (сразу после шапки) ═══ -->
<section id="forwhom">
  <div class="wrap">
    <div class="eyebrow">Для кого этот фонд</div>
    <h2 class="sec">Подойдёт, если вы&nbsp;—</h2>
    <div class="grid g3">
      <div class="card feature"><div class="ic">🏖️</div><h3>Поняли, что хороший доход на работе — это не навсегда</h3>
        <p>А на заслуженном отдыхе пригодится накопленный капитал.</p></div>
      <div class="card feature"><div class="ic">🇷🇺</div><h3>Хотите сделать аллокацию части капитала в акции РФ</h3>
        <p>Но нет желания или времени на самостоятельный анализ — готовы доверить выбор управляющему.</p></div>
      <div class="card feature"><div class="ic">🎢</div><h3>Готовы к колебаниям стоимости пая</h3>
        <p>Понимаете, что акции — высокорисковый актив: стоимость пая может как расти, так и снижаться на десятки процентов.</p></div>
      <div class="card feature"><div class="ic">⏳</div><h3>Инвестируете вдолгую</h3>
        <p>Горизонт от 3 лет и цель — защитить накопления от инфляции и приумножить на дистанции. Не ждёте сверхдоходности «здесь и сейчас» от спекуляций.</p></div>
      <div class="card feature"><div class="ic">🛡️</div><h3>Хотите надёжную инфраструктуру и контроль рисков</h3>
        <p>УК «Альфа-Капитал» — одна из ведущих в РФ, работу УК контролирует Банк России. Стратегия фонда и действия управляющего строго регламентированы.</p></div>
      <div class="card feature"><div class="ic">🔁</div><h3>Готовы инвестировать регулярно небольшими суммами</h3>
        <p>Купить паи фонда можно от нескольких тысяч рублей онлайн, без походов в офис.</p></div>
    </div>
  </div>
</section>

<!-- ═══ 2. УПРАВЛЯЮЩИЙ ═══ -->
<section id="manager">
  <div class="wrap">
    <div class="mgr">
      <div class="mgr-photo"><img src="%%PHOTO%%" alt="%%MANAGER%%"></div>
      <div>
        <div class="eyebrow">Об управляющем фондом</div>
        <h3>%%MANAGER%%</h3>
        <div class="role">Частный инвестор, управляющий активами</div>
        <ul class="mgr-list">
          <li>ТОП-1 пайщик фонда по размеру внесённых средств: у управляющего «шкура в игре» — он сам заинтересован в отличных результатах фонда.</li>
          <li>Окончил Московский физико-технический институт (Физтех) с отличием в 2012 году.</li>
          <li>10 лет в банках и ИТ (Сбер, ВТБ, Яндекс). Большой опыт управления и оптимизации продуктов с крупными бюджетами.</li>
          <li>Инвестор на фондовом рынке с 2015 года. Основная специализация — акции.</li>
          <li>Соавтор блога «Спроси Василича», посвящённого авторскому подходу к анализу и выбору активов в портфель.</li>
        </ul>
      </div>
    </div>
  </div>
</section>

<!-- ═══ 4. СТРАТЕГИЯ ═══ -->
<section id="strategy">
  <div class="wrap">
    <div class="eyebrow">Стратегия фонда</div>
    <h2 class="sec">Идея и логика формирования портфеля фонда</h2>

    <!-- идея + задача управляющего -->
    <div class="grid g2" style="margin-top:24px">
      <div class="card idea">
        <div class="idea-top"><h3>Ключевая идея</h3><span class="chk">☑</span></div>
        <p>Фонд ориентирован на долгосрочное владение бизнесами компаний. Качественные бизнесы способны
          решать проблемы и имеют потенциал для опережения инфляции и наращивания возврата своим владельцам.</p>
      </div>
      <div class="card idea">
        <div class="idea-top"><h3>Задача управляющего</h3><span class="chk">☑</span></div>
        <p>Не только выбрать такие активы, но и приобрести их по привлекательным ценам. Подход к анализу
          от общего к частному: анализ макроусловий, выделение привлекательных отраслей и тенденций,
          оценка риск-доходности и выбор конкретных активов в портфель.</p>
      </div>
    </div>

    <!-- подход к аллокации (3 матрёшки) -->
    <h3 class="sub-h">Подход к формированию портфеля</h3>
    <div class="grid g3 alloc">
      <div class="card alloc-c">
        <div class="alloc-head">
          <div class="doll-mini"><svg viewBox="0 0 200 300" class="doll big" aria-hidden="true"><path d="M100,16 C136,16 156,52 156,84 C156,104 144,112 140,120 C176,132 192,184 192,224 C192,264 164,288 132,288 L68,288 C36,288 8,264 8,224 C8,184 24,132 60,120 C56,112 44,104 44,84 C44,52 64,16 100,16 Z" fill="#6E58A8"/><ellipse cx="100" cy="70" rx="26" ry="30" fill="#fff"/></svg></div>
          <div class="alloc-meta"><h4>Основа</h4><div class="pct">60–70%<span>портфеля</span></div></div>
        </div>
        <p>Преимущественно акции компаний с устойчивой бизнес-моделью и потенциалом роста возврата
          акционерам на горизонте от 1 года. Обеспечивают потенциальный долгосрочный рост отдачи от инвестиций.</p>
      </div>
      <div class="card alloc-c">
        <div class="alloc-head">
          <div class="doll-mini"><svg viewBox="0 0 200 300" class="doll mid" aria-hidden="true"><path d="M100,16 C136,16 156,52 156,84 C156,104 144,112 140,120 C176,132 192,184 192,224 C192,264 164,288 132,288 L68,288 C36,288 8,264 8,224 C8,184 24,132 60,120 C56,112 44,104 44,84 C44,52 64,16 100,16 Z" fill="#9683C4"/><ellipse cx="100" cy="70" rx="26" ry="30" fill="#fff"/></svg></div>
          <div class="alloc-meta"><h4>Активная</h4><div class="pct">20–30%<span>портфеля</span></div></div>
        </div>
        <p>Акции и облигации, получающие преимущества «здесь и сейчас» и имеющие потенциал к переоценке
          в течение года. Нацелены на создание дополнительной доходности фонду внутри года.</p>
      </div>
      <div class="card alloc-c">
        <div class="alloc-head">
          <div class="doll-mini"><svg viewBox="0 0 200 300" class="doll sm" aria-hidden="true"><path d="M100,16 C136,16 156,52 156,84 C156,104 144,112 140,120 C176,132 192,184 192,224 C192,264 164,288 132,288 L68,288 C36,288 8,264 8,224 C8,184 24,132 60,120 C56,112 44,104 44,84 C44,52 64,16 100,16 Z" fill="#C3B7E0"/><ellipse cx="100" cy="70" rx="26" ry="30" fill="#fff"/></svg></div>
          <div class="alloc-meta"><h4>Стабилизирующая</h4><div class="pct">0–10%<span>портфеля</span></div></div>
        </div>
        <p>Высоколиквидные активы с потенциальной доходностью, привязанной к ставке ЦБ.
          Балансируют портфель и дают гибкость в периоды волатильности.</p>
      </div>
    </div>

  </div>
</section>

<!-- ═══ 5. РЕЗУЛЬТАТЫ ═══ -->
<section id="results" style="background:linear-gradient(180deg,var(--bg),#fff)">
  <div class="wrap">
    <div class="eyebrow">Текущие результаты фонда</div>
    <h2 class="sec">Доходность фонда vs индикатор</h2>
    <p class="lead" style="max-width:none">Индикатором для сравнения доходности фонда является индекс Мосбиржи российских акций полной доходности (MCFTR). Доходность фонда указана уже с учётом комиссии за управление.</p>

    <div class="card chart-card" style="margin-top:22px">
      <div class="chart-top">
        <div class="seg" id="modeSeg">
          <button data-mode="ret" class="on">Доходность, %</button>
          <button data-mode="nav">Стоимость пая, ₽</button>
        </div>
        <div class="seg" id="rangeSeg">
          <button data-range="1m">1М</button><button data-range="3m">3М</button>
          <button data-range="6m">6М</button><button data-range="1y" class="on">1Г</button>
          <button data-range="ytd">YTD</button><button data-range="all">Всё</button>
        </div>
      </div>
      <div class="legend" id="legend">
        <span><i style="background:var(--purple-mid)"></i>Фонд «Матрёшка а-ля Рус»</span>
        <span><i style="background:var(--orange)"></i>Индикатор (Индекс МосБиржи полной доходности)</span>
      </div>
      <div class="chart-box">
        <svg id="chart" viewBox="0 0 1000 420" preserveAspectRatio="none" role="img" aria-label="График доходности фонда против индекса"></svg>
        <div class="tip" id="tip"></div>
      </div>
    </div>

    <div class="results-grid">
      <div class="card">
        <h3 style="color:var(--purple);font-weight:800;margin-bottom:10px">Доходность по периодам</h3>
        <table class="ret">
          <thead><tr><th>Период</th><th>Фонд</th><th>Индикатор</th><th>Разница</th></tr></thead>
          <tbody>%%RETURNS_TABLE%%</tbody>
        </table>
        <p class="note">Данные на %%ASOF%%.</p>
      </div>
      <div class="card">
        <h3 style="color:var(--purple);font-weight:800;margin-bottom:12px">Ключевые показатели</h3>
        <div class="mini">
          <div class="m"><div class="l">Стоимость пая</div><div class="v">%%SHARE%%&nbsp;₽</div></div>
          <div class="m"><div class="l">СЧА фонда</div><div class="v">%%NAV%%&nbsp;млн&nbsp;₽</div></div>
          <div class="m" style="grid-column:1 / -1"><div class="l">Место в рейтинге фондов акций</div><div class="v">%%RANK%%<sup>*</sup></div></div>
        </div>
        <p class="note">Данные на %%ASOF%%.</p>
        <p class="note"><sup>*</sup> Рейтинг investfunds.ru доходности публичных фондов акций за последние 12 месяцев.</p>
      </div>
    </div>
  </div>
</section>

<!-- ═══ 6. ПРЕИМУЩЕСТВА ═══ -->
<section id="advantages">
  <div class="wrap">
    <div class="eyebrow">Преимущества инвестиций через ОПИФ</div>
    <h2 class="sec">Налоговые преимущества фонда</h2>
    <div class="grid g2">
      <div class="card feature"><div class="ic"><svg viewBox="0 0 40 40" width="30" height="30" aria-hidden="true"><circle cx="20" cy="20" r="17" fill="none" stroke="currentColor" stroke-width="3.4"/><text x="20" y="24.5" text-anchor="middle" font-size="12" font-weight="800" fill="currentColor" font-family="system-ui,Arial,sans-serif">TAX</text><line x1="8" y1="8" x2="32" y2="32" stroke="currentColor" stroke-width="3.4" stroke-linecap="round"/></svg></div><h3>Фонд освобожден от уплаты налогов</h3><p>Приходящие дивиденды и купоны, прибыль от купли-продажи внутри фонда не облагаются налогом. Налог платит только пайщик с прибыли от выдачи-погашения пая</p></div>
      <div class="card feature"><div class="ic">🎁</div><h3>Пайщики вправе получить налоговый вычет при владении от 3 лет</h3><p>Размер ЛДВ<sup>1</sup>: до 3 млн руб. прибыли за каждый год владения освобождаются от налогов. ЛДВ рассчитывается с даты приобретения паев по методу FIFO<sup>2</sup>.</p></div>
    </div>
    <p class="note" style="margin-top:14px">
      <sup>1</sup>&nbsp;ЛДВ — льгота долгосрочного владения.<br>
      <sup>2</sup>&nbsp;FIFO — first in, first out. Паи продаются в том же порядке, в каком они были куплены.
    </p>
  </div>
</section>

<!-- ═══ 7. КАК КУПИТЬ / ИНВЕСТИРОВАТЬ ═══ -->
<section id="invest" style="background:var(--tint)">
  <div class="wrap">
    <div class="eyebrow">Как инвестировать</div>
    <h2 class="sec">Способы покупки паёв фонда</h2>
    <p class="lead" style="max-width:none">Выберите удобный для вас способ покупки. Онлайн-покупка занимает несколько минут.</p>
    <div class="ways" style="margin-top:26px">

      <div class="way">
        <div class="num">
          <div class="brandbox"><img class="brandlogo" src="%%LOGO_FIN%%" alt="Финуслуги"></div>
        </div>
        <div>
          <h3>Онлайн на платформе Финуслуги</h3>
          <p>Госмаркетплейс финансовых продуктов. Покупка полностью онлайн за 5 минут, вход через Госуслуги.</p>
          <ol><li>Откройте страницу фонда или отсканируйте QR-код.</li><li>Авторизуйтесь через Госуслуги.</li><li>Укажите сумму, оформите заявку и подтвердите покупку.</li></ol>
        </div>
        <div class="act">
          <a class="btn btn-primary" href="https://finuslugi.ru/invest/funds/93f882f5-f205-42bc-83ce-c069cc9027bf" target="_blank" rel="noopener">Открыть →</a>
          <div class="qrmini"><img src="%%QR_FIN%%" alt="QR — Финуслуги"></div>
        </div>
      </div>

      <div class="way">
        <div class="num">
          <div class="brandbox"><img class="brandlogo" src="%%LOGO_ALFA%%" alt="Альфа-Капитал"></div>
        </div>
        <div>
          <h3>Онлайн через УК «Альфа-Капитал»</h3>
          <p>Покупка напрямую у управляющей компании — в приложении или личном кабинете УК «Альфа-Капитал».</p>
          <ol><li>Перейдите на сайт УК или отсканируйте QR-код</li><li>Пройдите быструю идентификацию или авторизуйтесь через Госуслуги</li><li>Подайте заявку на приобретение паёв фонда «Матрёшка а-ля Рус»</li></ol>
        </div>
        <div class="act">
          <a class="btn btn-primary" href="https://promo.alfacapital.ru/matryoshka-rus" target="_blank" rel="noopener">Открыть →</a>
          <div class="qrmini"><img src="%%QR_ALFA%%" alt="QR — Альфа-Капитал"></div>
        </div>
      </div>

      <div class="way" style="display:none">
        <div class="num">
          <div class="brandbox">
            <div class="mark" style="background:linear-gradient(135deg,#624997,#8b6fc0)">
              <svg viewBox="0 0 32 32" fill="none" aria-hidden="true">
                <rect x="5.5" y="16" width="4.4" height="9" rx="1" fill="#fff"/>
                <rect x="13.8" y="10" width="4.4" height="15" rx="1" fill="#fff"/>
                <rect x="22.1" y="6" width="4.4" height="19" rx="1" fill="#fff"/>
              </svg>
            </div>
            <div class="lbl" style="color:var(--purple)">Биржа&nbsp;/&nbsp;брокер</div>
          </div>
        </div>
        <div>
          <h3>У своего брокера</h3>
          <p>Если у вас есть брокерский счёт, купите паи по тикеру / ISIN как обычную ценную бумагу.</p>
          <ol><li>Откройте торговый терминал брокера</li><li>Найдите бумагу по ISIN</li><li>Выставьте заявку на покупку нужного объёма</li></ol>
          <span class="isin">ISIN %%ISIN%%</span>
        </div>
        <div class="act"><a class="btn btn-ghost" href="#faq">Подробнее в FAQ</a></div>
      </div>

    </div>
    <p class="note">Приобретение паёв возможно у уполномоченных агентов и УК. Перед покупкой ознакомьтесь с правилами доверительного управления фондом.</p>
  </div>
</section>

<!-- ═══ 8. СМИ О ФОНДЕ ═══ -->
<section id="press">
  <div class="wrap">
    <div class="eyebrow">СМИ о фонде</div>
    <h2 class="sec">Публикации о фонде и управляющем</h2>
    <p class="lead">Упоминания в новостях, прессе и видео.</p>
    <div class="press" style="margin-top:22px">
      <div class="press-video">
        <iframe src="https://vkvideo.ru/video_ext.php?oid=-203057439&id=456239411&hash=2d0dfd9d729c8315&hd=4" title="10 лет инвестиций и ВЫВОД, который должен услышать каждый" allow="autoplay; encrypted-media; fullscreen; picture-in-picture; screen-wake-lock;" frameborder="0" allowfullscreen></iframe>
      </div>
      <div class="press-list">
        <a class="p" href="https://investfunds.ru/news/172453/" target="_blank" rel="noopener"><div class="src">Investfunds · 11 августа 2026</div><div class="t">Успех авторских стратегий в июле — вернут ли они веру пайщиков в фонды акций</div><span class="lnk">Читать материал →</span></a>
        <a class="p" href="https://www.forbes.ru/investicii/545073-tri-blogera-na-mosbirze-kak-avtory-telegram-kanalov-zapuskaut-sobstvennye-fondy" target="_blank" rel="noopener"><div class="src">Forbes · 3 сентября 2025</div><div class="t">Три блогера на Мосбирже: как авторы Telegram-каналов запускают собственные фонды</div><span class="lnk">Читать материал →</span></a>
        <a class="p" href="https://www.rbc.ru/newspaper/2025/04/23/680671ea9a79471799084b4b" target="_blank" rel="noopener"><div class="src">РБК · 23 апреля 2025</div><div class="t">Пай по авторскому рецепту</div><span class="lnk">Читать материал →</span></a>
      </div>
    </div>
  </div>
</section>

<!-- ═══ 9. ДОКУМЕНТЫ ═══ -->
<section id="docs" style="background:linear-gradient(180deg,#fff,var(--bg))">
  <div class="wrap">
    <div class="eyebrow">Документы и отчёты</div>
    <h2 class="sec">Отчёты о работе фонда</h2>
    <p class="lead" style="max-width:none">Правила ДУ и другая обязательная к раскрытию отчётность о работе фонда доступны по <a href="https://www.alfacapital.ru/disclosure/pifs/opif-matryoshka/cost" target="_blank" rel="noopener">ссылке</a> на сайте УК «Альфа-Капитал».</p>
    <div class="docs" style="margin-top:22px">%%DOCS%%</div>
  </div>
</section>

<!-- ═══ 10. FAQ ═══ -->
<section id="faq">
  <div class="wrap">
    <div class="eyebrow">Вопросы и ответы</div>
    <h2 class="sec">Частые вопросы</h2>
    <div class="faq" style="margin-top:22px">
      <details open><summary>«Матрёшка а-ля Рус» — это?</summary><p>Открытый паевой инвестиционный фонд. Проще говоря, это инструмент коллективных инвестиций. За работу инфраструктуры фонда отвечает управляющая компания, при этом она не имеет прямого доступа к активам фонда. Каждый инвестор покупает долю в общем портфеле активов — пай. Формирует и пересматривает портфель активов управляющий, он же автор фонда.</p></details>
      <details><summary>Какие комиссии у фонда?</summary><p>Совокупная годовая комиссия составляет 2,6%, других комиссий нет. Комиссия ежедневно учитывается в стоимости пая, поэтому стоимость пая и доходность фонда указаны уже с учётом комиссии.</p></details>
      <details><summary>Какие минимальные сумма инвестиций и срок инвестирования?</summary><p>Минимальная сумма инвестиций — 5000 руб. Фонд не имеет ограничений по сроку инвестирования. Однако рекомендуется инвестировать на срок от 3 лет, чтобы получить дополнительные положительные эффекты от налоговых льгот.</p></details>
      <details><summary>Как продать (погасить) паи?</summary><p>Паи фонда можно погасить в любой рабочий день через тот же канал, где покупали. Деньги поступают на счёт в срок, установленный правилами фонда.</p></details>
      <details><summary>Чем фонд лучше автоследования?</summary><p>Это разные инструменты, но их часто сравнивают, поскольку автоследование — тоже авторский продукт. Наиболее популярные стратегии автоследования берут не менее 3% в год и дополнительно 10–20% от прибыли ежемесячно. В ОПИФ «Матрёшка а-ля Рус» комиссия фиксированная — 2,6% в год. Кроме того, действия управляющего в фонде строго регламентированы правилами доверительного управления, за выполнением которых следит Банк России.</p></details>
      <details><summary>Мне нравится подход управляющего, но я хочу оставить сбережения у своего брокера. Что делать?</summary><p>Для каждого индивидуального случая можно подобрать своё решение. Поэтому со всеми возникающими вопросами смело обращайтесь напрямую к управляющему. Личные контакты управляющего указаны в разделе «<a href="#contacts">Свяжитесь с нами</a>».</p></details>
    </div>
  </div>
</section>

<!-- ═══ 11. КОНТАКТЫ ═══ -->
<section id="contacts" style="background:var(--tint)">
  <div class="wrap">
    <div class="eyebrow">Контакты</div>
    <h2 class="sec">Свяжитесь с нами</h2>
    <p class="lead">По всем возникшим вопросам обращайтесь по контактам, указанным ниже.</p>
    <div class="contacts" style="margin-top:22px">
      <div class="contact-item"><div class="ic">✈️</div><div><div class="l">Личный Telegram управляющего фондом</div><div class="v"><a href="https://t.me/kudrik_vasili4" target="_blank" rel="noopener">@kudrik_vasili4</a></div></div></div>
      <div class="contact-item"><div class="ic">✉️</div><div><div class="l">E-mail</div><div class="v"><a href="mailto:MatreshkaRusFund@yandex.ru">MatreshkaRusFund@yandex.ru</a></div></div></div>
      <div class="contact-item"><div class="ic">📣</div><div><div class="l">Канал фонда в Telegram</div><div class="v"><a href="https://t.me/MatreshkaRusFund" target="_blank" rel="noopener">@MatreshkaRusFund</a></div></div></div>
      <div class="contact-item"><div class="ic">📞</div><div><div class="l">Горячая линия УК «Альфа-Капитал»</div><div class="v"><a href="tel:+78002002828">+7 (800) 200-28-28</a></div></div></div>
    </div>
    <div style="margin-top:22px"><a class="btn btn-primary" href="#invest">Перейти к покупке паёв →</a></div>
  </div>
</section>

<!-- ═══ FOOTER ═══ -->
<footer>
  <div class="wrap">
    <div class="top">
      <a class="brand" href="#top"><img src="%%LOGO%%" alt="" style="width:34px;height:34px">ОПИФ «Матрёшка&nbsp;а-ля&nbsp;Рус»</a>
      <p class="tag-line">ОПИФ рыночных финансовых инструментов «Матрёшка а-ля Рус», ISIN %%ISIN%%. Правила доверительного управления № %%REG%% зарегистрированы Банком России 27.03.2025.</p>
    </div>
    <div class="disc">
      <b>Раскрытие обязательной информации.</b>
      <p>СТОИМОСТЬ ИНВЕСТИЦИОННЫХ ПАЕВ МОЖЕТ УВЕЛИЧИВАТЬСЯ И УМЕНЬШАТЬСЯ, РЕЗУЛЬТАТЫ ИНВЕСТИРОВАНИЯ В ПРОШЛОМ НЕ ОПРЕДЕЛЯЮТ ДОХОДОВ В БУДУЩЕМ, ГОСУДАРСТВО НЕ ГАРАНТИРУЕТ ДОХОДНОСТЬ ИНВЕСТИЦИЙ В ИНВЕСТИЦИОННЫЕ ФОНДЫ. ПРЕЖДЕ ЧЕМ ПРИОБРЕСТИ ИНВЕСТИЦИОННЫЙ ПАЙ, СЛЕДУЕТ ВНИМАТЕЛЬНО ОЗНАКОМИТЬСЯ С ПРАВИЛАМИ ДОВЕРИТЕЛЬНОГО УПРАВЛЕНИЯ ПАЕВЫМ ИНВЕСТИЦИОННЫМ ФОНДОМ.</p>
      <p>Услуга не является банковским вкладом, накопительным счётом. Инвестиции не подразумевают возврат основной инвестированной суммы. Лицензия на осуществление деятельности по управлению инвестиционными фондами, паевыми инвестиционными фондами и негосударственными пенсионными фондами № 21—000—1—00028 от 22 сентября 1998 года выдана ФСФР России, без ограничения срока действия. Лицензия на осуществление деятельности по управлению ценными бумагами № 077—08158—001000, выдана ФСФР России 30 ноября 2004 года, без ограничения срока действия. Подробную информацию о деятельности ООО УК «Альфа-Капитал» и паевых инвестиционных фондов, находящихся под её управлением, включая тексты правил доверительного управления, всех изменений и дополнений к ним, а также сведения о местах приёма заявок на приобретение, погашение и обмен инвестиционных паёв вы можете получить по адресу 123001, Москва, ул. Садовая-Кудринская, д. 32, стр. 1, телефоны: +7 (495) 783-4-783, 8 (800) 200-28-28, а также на сайте ООО УК «Альфа-Капитал» в сети Internet по адресу www.alfacapital.ru.</p>
      <p>Не является индивидуальной инвестиционной рекомендацией и побуждением к приобретению определённых ценных бумаг и (или) заключению определённых договоров, в том числе являющихся производными финансовыми инструментами.</p>
    </div>
  </div>
</footer>

<script>
const DATA = %%CHART_DATA%%;
(function(){
  const svg = document.getElementById('chart');
  const tip = document.getElementById('tip');
  const legend = document.getElementById('legend');
  const NS='http://www.w3.org/2000/svg';
  const W=1000,H=420,padL=58,padR=84,padT=22,padB=34;
  const COL={fund:'#624997',index:'#F49C2D'};

  const parse = s=>{const[y,m,d]=s.split('-').map(Number);return Date.UTC(y,m-1,d);};
  const fund = DATA.fund.map(p=>({t:parse(p.d),v:p.v,nav:p.nav,d:p.d}));
  const index = DATA.index.map(p=>({t:parse(p.d),v:p.v,d:p.d}));
  const idxMap = new Map(index.map(p=>[p.d,p.v]));
  // общие торговые дни (для сравнения доходности)
  const common = fund.filter(p=>idxMap.has(p.d)).map(p=>({t:p.t,d:p.d,f:p.v,i:idxMap.get(p.d)}));

  const DMAX = fund[fund.length-1].t;
  let mode='ret', range='1y';

  function rangeStart(){
    if(range==='all') return -Infinity;
    if(range==='ytd'){const y=new Date(DMAX).getUTCFullYear();return Date.UTC(y,0,1);}
    const days={'1m':30,'3m':91,'6m':182,'1y':365}[range];
    return DMAX-days*86400000;
  }
  function niceStep(span,target){
    const raw=span/target;const p=Math.pow(10,Math.floor(Math.log10(raw)));
    const c=[1,2,2.5,5,10];for(const k of c){if(p*k>=raw)return p*k;}return p*10;
  }
  const fmtPct=v=>v.toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2,signDisplay:'exceptZero'})+'%';
  const fmtRub=v=>v.toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2})+' ₽';
  const fmtDate=t=>{const d=new Date(t);const p=n=>String(n).padStart(2,'0');return `${p(d.getUTCDate())}.${p(d.getUTCMonth()+1)}.${d.getUTCFullYear()}`;};

  let plot={};  // сохраняем геометрию для тултипа

  function render(){
    const start=rangeStart();
    // формируем серии
    let series=[]; // [{name,color,pts:[{t,y,label}]}]
    if(mode==='ret'){
      legend.style.display='flex';
      const seg=common.filter(p=>p.t>=start);
      if(seg.length<2){svg.innerHTML='';return;}
      // YTD: база — последнее значение прошлого года (рост от закрытия 31.12),
      // а не первый торговый день нового года.
      let bf=seg[0].f, bi=seg[0].i;
      if(range==='ytd'){const before=common.filter(p=>p.t<start);if(before.length){const b=before[before.length-1];bf=b.f;bi=b.i;}}
      series=[
        {name:'fund',color:COL.fund,pts:seg.map(p=>({t:p.t,val:(p.f/bf-1)*100}))},
        {name:'index',color:COL.index,pts:seg.map(p=>({t:p.t,val:(p.i/bi-1)*100}))},
      ];
    }else{
      legend.style.display='none';
      const seg=fund.filter(p=>p.t>=start);
      if(seg.length<2){svg.innerHTML='';return;}
      series=[{name:'fund',color:COL.fund,pts:seg.map(p=>({t:p.t,val:p.v}))}];
    }
    const t0=series[0].pts[0].t, t1=series[0].pts[series[0].pts.length-1].t;
    let lo=Infinity,hi=-Infinity;
    series.forEach(s=>s.pts.forEach(p=>{if(p.val<lo)lo=p.val;if(p.val>hi)hi=p.val;}));
    if(mode==='ret'){lo=Math.min(lo,0);hi=Math.max(hi,0);}
    const pad=(hi-lo)*0.12||1; lo-=pad; hi+=pad;
    const X=t=>padL+(t1===t0?0:(t-t0)/(t1-t0))*(W-padL-padR);
    const Y=v=>padT+(hi===lo?0:(hi-v)/(hi-lo))*(H-padT-padB);

    let g='';
    // горизонтальная сетка
    const step=niceStep(hi-lo,5);
    const g0=Math.ceil(lo/step)*step;
    for(let v=g0;v<=hi+1e-9;v+=step){
      const y=Y(v);const zero=(mode==='ret'&&Math.abs(v)<1e-9);
      g+=`<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W-padR}" y2="${y.toFixed(1)}" stroke="${zero?'#c9bfe0':'#eee7f7'}" stroke-width="${zero?1.4:1}"/>`;
      const lab=mode==='ret'?fmtPct(v):Math.round(v);
      g+=`<text x="${padL-8}" y="${(y+4).toFixed(1)}" text-anchor="end" font-size="11" fill="#8a869a">${lab}</text>`;
    }
    // вертикальные метки дат (5 шт)
    const ticks=5;
    for(let k=0;k<=ticks;k++){
      const t=t0+(t1-t0)*k/ticks;const x=X(t);
      g+=`<line x1="${x.toFixed(1)}" y1="${padT}" x2="${x.toFixed(1)}" y2="${H-padB}" stroke="#f2edfa" stroke-width="1"/>`;
      g+=`<text x="${x.toFixed(1)}" y="${H-padB+18}" text-anchor="middle" font-size="10.5" fill="#8a869a">${fmtDate(t)}</text>`;
    }
    // линии
    plot={series:[],X,Y,t0,t1};
    const endLabels=[];
    series.forEach(s=>{
      let d='';const geo=[];
      s.pts.forEach((p,i)=>{const x=X(p.t),y=Y(p.val);d+=(i?'L':'M')+x.toFixed(1)+' '+y.toFixed(1)+' ';geo.push({x,y,t:p.t,val:p.val});});
      g+=`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/>`;
      const last=geo[geo.length-1];
      g+=`<circle cx="${last.x.toFixed(1)}" cy="${last.y.toFixed(1)}" r="4" fill="${s.color}"/>`;
      endLabels.push({y:last.y,color:s.color,lab:mode==='ret'?fmtPct(last.val):fmtRub(last.val)});
      plot.series.push({name:s.name,color:s.color,geo});
    });
    // подписи значений — в правом поле, чтобы не перекрывать линии; при близости концов разводим по вертикали
    endLabels.sort((a,b)=>a.y-b.y);
    const GAP=15;
    for(let k=1;k<endLabels.length;k++){if(endLabels[k].y-endLabels[k-1].y<GAP)endLabels[k].y=endLabels[k-1].y+GAP;}
    for(let k=endLabels.length-1;k>0;k--){if(endLabels[k].y>H-padB){endLabels[k].y=H-padB;if(endLabels[k].y-endLabels[k-1].y<GAP)endLabels[k-1].y=endLabels[k].y-GAP;}}
    const lx=W-padR+8;
    endLabels.forEach(e=>{g+=`<text x="${lx.toFixed(1)}" y="${(e.y+4).toFixed(1)}" text-anchor="start" font-size="13" font-weight="800" fill="${e.color}">${e.lab}</text>`;});
    // слой для крестика
    g+=`<line id="cross" x1="0" y1="${padT}" x2="0" y2="${H-padB}" stroke="#b9b2cc" stroke-width="1" stroke-dasharray="4 4" style="opacity:0"/>`;
    svg.innerHTML=g;
  }

  function onMove(ev){
    if(!plot.series||!plot.series.length){return;}
    const r=svg.getBoundingClientRect();
    const cx=ev.touches?ev.touches[0].clientX:ev.clientX;
    const px=(cx-r.left)/r.width*W;
    const g0=plot.series[0].geo;
    let idx=0,best=Infinity;
    g0.forEach((p,i)=>{const dd=Math.abs(p.x-px);if(dd<best){best=dd;idx=i;}});
    const cross=document.getElementById('cross');
    const xline=g0[idx].x;
    if(cross){cross.setAttribute('x1',xline);cross.setAttribute('x2',xline);cross.style.opacity=.9;}
    let rows='';
    plot.series.forEach(s=>{
      const p=s.geo[idx];if(!p)return;
      const lab=mode==='ret'?fmtPct(p.val):fmtRub(p.val);
      const nm=s.name==='fund'?'Фонд':'Индикатор';
      rows+=`<div><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${s.color};margin-right:6px"></i>${nm}: <b>${lab}</b></div>`;
    });
    const t=g0[idx].t;
    tip.innerHTML=`<div style="margin-bottom:3px;color:#8a869a">${fmtDate(t)}</div>${rows}`;
    tip.style.opacity=1;
    const boxr=svg.parentElement.getBoundingClientRect();
    let left=(xline/W)*boxr.width+12;
    if(left>boxr.width-140)left=(xline/W)*boxr.width-tip.offsetWidth-12;
    tip.style.left=left+'px';
    tip.style.top='14px';
  }
  function onLeave(){tip.style.opacity=0;const c=document.getElementById('cross');if(c)c.style.opacity=0;}

  svg.addEventListener('mousemove',onMove);
  svg.addEventListener('mouseleave',onLeave);
  svg.addEventListener('touchmove',onMove,{passive:true});
  svg.addEventListener('touchend',onLeave);

  document.getElementById('rangeSeg').addEventListener('click',e=>{
    const b=e.target.closest('button');if(!b)return;
    range=b.dataset.range;
    [...e.currentTarget.children].forEach(x=>x.classList.toggle('on',x===b));
    render();
  });
  document.getElementById('modeSeg').addEventListener('click',e=>{
    const b=e.target.closest('button');if(!b)return;
    mode=b.dataset.mode;
    [...e.currentTarget.children].forEach(x=>x.classList.toggle('on',x===b));
    render();
  });
  window.addEventListener('resize',()=>{clearTimeout(window.__rt);window.__rt=setTimeout(render,120);});
  render();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    build()
