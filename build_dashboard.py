"""
Google Sheet -> dashboard.html üretici.

dashboard_template.html içindeki /*__DATA__*/ yer tutucusunu, Sheet'teki güncel
rakamlarla doldurup dashboard.html'i yazar. Sayfa yayınlandıktan sonra dış bir
adrese istek atamadığı için veri HTML'in içine gömülür; bu yüzden panel her
tazelenmek istendiğinde bu script yeniden çalıştırılır.

Kullanım:
    python build_dashboard.py
"""

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "dashboard_template.html")
OUTPUT = os.path.join(HERE, "dashboard.html")

# Sheet sütun sırası (sync.py'deki SHEET_HEADER ile aynı olmalı)
COL = {
    "period": 0, "order": 1, "channel": 2, "account": 3, "spend": 4,
    "views": 5, "atc": 6, "purchases": 7, "value": 8, "roas": 9,
    "clicks": 10, "updated": 11,
}

# sync.py'deki D2C_SHEET_HEADER ile aynı olmalı
D2C_COL = {
    "period": 0, "order": 1, "brand": 2, "reach": 3, "impressions": 4,
    "clicks": 5, "spend": 6, "revenue": 7, "orders": 8, "updated": 9,
}

# sync.py'deki karşılaştırma dönemi sıra numarası kaydırması
COMPARE_OFFSET = 10

MARGINS_FILE = os.path.join(HERE, "marjlar.json")

# Marj verilmediğinde panel eski davranışına döner: ciro brüt kabul edilir,
# başabaş 1,00×. Gerçek katkı marjı girildiğinde başabaş 1/marj'a çıkar.
DEFAULT_BREAK_EVEN = 1.0


def break_even_map() -> dict:
    """marjlar.json'daki katkı marjlarını başabaş ROAS'a çevirir:
    başabaş = 1 / marj. Dosya yoksa ya da bozuksa herkes varsayılana düşer."""
    try:
        with open(MARGINS_FILE, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return {"_default": DEFAULT_BREAK_EVEN}

    def to_be(margin):
        # 0 ve negatif marj başabaşı tanımsız yapar; sessizce varsayılana düşer.
        if not isinstance(margin, (int, float)) or margin <= 0 or margin > 1:
            return None
        return round(1 / margin, 4)

    out = {"_default": to_be(cfg.get("varsayilan")) or DEFAULT_BREAK_EVEN}
    for brand, margin in (cfg.get("markalar") or {}).items():
        be = to_be(margin)
        if be:
            out[brand] = be
    return out


AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
         "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def gun(d: date) -> str:
    return f"{d.day} {AYLAR[d.month - 1]} {d.year}"


def period_ranges(last_full_day: date, mtd_day: date) -> dict:
    """Sheet'teki dönem etiketlerinin hangi takvim aralığına denk geldiğini
    hesaplar. Dün / Son 7 Gün / Geçen Ay son tam günde biter; "Bu Ay" ise
    MTD olduğu için senkron gününü de kapsar (sync.py'deki period_bounds)."""
    d = last_full_day
    week_start = d - timedelta(days=6)
    month_start = mtd_day.replace(day=1)
    prev_month_end = month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    return {
        "Dün": f"1 gün · {gun(d)}",
        "Son 7 Gün": f"7 gün · {gun(week_start)} – {gun(d)}",
        "Bu Ay": (f"Ay başından bugüne (MTD) · {gun(month_start)} – {gun(mtd_day)}"
                  if month_start != mtd_day else f"Ayın ilk günü · {gun(mtd_day)}"),
        "Geçen Ay": f"Tam ay · {gun(prev_month_start)} – {gun(prev_month_end)}",
    }


def num(raw):
    """Sheet hücresini sayıya çevirir. Boş hücre = veri yok = None.
    Sıfır ile 'veri yok' farklı şeyler; boşu 0'a düşürmüyoruz."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_sheet():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"],
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(os.environ["GOOGLE_SHEET_ID"])

    rows, updated, labels = [], None, {}
    d2c_rows = []

    for worksheet in spreadsheet.worksheets():
        values = worksheet.get_all_values()
        if len(values) < 2 or values[0][COL["period"]] != "Dönem":
            continue  # veri sekmesi değil

        if worksheet.title == "D2C":
            for row in values[1:]:
                if not row or not row[D2C_COL["period"]].strip():
                    continue
                order = num(row[D2C_COL["order"]])
                if order is None:
                    continue
                d2c_rows.append([
                    row[D2C_COL["brand"]].strip(),
                    int(order),
                    int(num(row[D2C_COL["reach"]]) or 0),
                    int(num(row[D2C_COL["impressions"]]) or 0),
                    int(num(row[D2C_COL["clicks"]]) or 0),
                    num(row[D2C_COL["spend"]]) or 0,
                    # Boş = Shopify bağlı değil (None); 0 ile karıştırılmasın.
                    num(row[D2C_COL["revenue"]]),
                    None if num(row[D2C_COL["orders"]]) is None else int(num(row[D2C_COL["orders"]])),
                ])
            continue  # CPAS satırı gibi işlenmesin

        brand = worksheet.title

        for row in values[1:]:
            if not row or not row[COL["period"]].strip():
                continue
            order = num(row[COL["order"]])
            if order is None:
                continue
            labels[int(order)] = row[COL["period"]].strip()
            updated = updated or row[COL["updated"]].strip()

            rows.append([
                brand,
                row[COL["channel"]].strip(),
                int(order),
                num(row[COL["spend"]]) or 0,
                int(num(row[COL["views"]]) or 0),
                int(num(row[COL["atc"]]) or 0),
                None if num(row[COL["purchases"]]) is None else int(num(row[COL["purchases"]])),
                num(row[COL["value"]]),
                int(num(row[COL["clicks"]]) or 0),
            ])

    if not rows:
        sys.exit("Sheet'te veri satırı bulunamadı — sync.py çalıştı mı?")

    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    d2c_rows.sort(key=lambda r: (r[0], r[1]))
    return rows, d2c_rows, updated or "", labels


def main():
    rows, d2c_rows, updated, labels = read_sheet()
    break_even = break_even_map()

    # Veri dünle biter; "dün"ü senkron damgasından türetiyoruz. Damga UTC ama
    # sync.py dönemleri İstanbul saatine göre kesiyor. 21:00–24:00 UTC arası
    # İstanbul'da ertesi gün olduğu için UTC tarihini doğrudan kullanmak
    # etiketleri bir gün geride gösteriyordu (veri 15 Eylül, etiket 14 Eylül).
    m = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})", updated)
    if m:
        synced_utc = datetime(*map(int, m.groups()), tzinfo=timezone.utc)
        synced = synced_utc.astimezone(ZoneInfo("Europe/Istanbul")).date()
    else:
        synced = datetime.now(ZoneInfo("Europe/Istanbul")).date()
    ranges = period_ranges(synced - timedelta(days=1), synced)

    # 10 ve üzeri sıra numaraları karşılaştırma dönemleri (sync.py'ye bak):
    # satırları D/D2C içinde duruyor ama dönem seçicide düğme olarak çıkmıyor.
    periods = [
        {"id": o, "label": labels[o], "range": ranges.get(labels[o], "")}
        for o in sorted(labels) if o < COMPARE_OFFSET
    ]

    data_block = (
        f"const UPDATED = {json.dumps(updated)};\n"
        f"const PERIODS = {json.dumps(periods, ensure_ascii=False)};\n"
        f"const COMPARE_OFFSET = {COMPARE_OFFSET};\n"
        f"const BREAK_EVEN = {json.dumps(break_even, ensure_ascii=False)};\n"
        f"const D = {json.dumps(rows, ensure_ascii=False)};\n"
        f"const D2C = {json.dumps(d2c_rows, ensure_ascii=False)};"
    )

    with open(TEMPLATE, encoding="utf-8") as fh:
        template = fh.read()

    if "/*__DATA__*/" not in template:
        sys.exit("Şablonda /*__DATA__*/ yer tutucusu yok.")

    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write(template.replace("/*__DATA__*/", data_block))

    accounts = len({(r[0], r[1]) for r in rows})
    be_note = ", ".join(f"{k}={v}×" for k, v in sorted(break_even.items()))
    print(f"dashboard.html yazıldı — {len(rows)} satır, {accounts} hesap, "
          f"{len(periods)} dönem, başabaş {be_note}, senkron {updated}")


if __name__ == "__main__":
    main()
