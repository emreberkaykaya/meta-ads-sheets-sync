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
from datetime import date, timedelta

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

AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
         "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def gun(d: date) -> str:
    return f"{d.day} {AYLAR[d.month - 1]} {d.year}"


def period_ranges(last_full_day: date) -> dict:
    """Sheet'teki dönem etiketlerinin hangi takvim aralığına denk geldiğini,
    verinin bittiği son tam günden geriye doğru hesaplar."""
    d = last_full_day
    week_start = d - timedelta(days=6)
    month_start = d.replace(day=1)
    prev_month_end = month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    return {
        "Dün": f"1 gün · {gun(d)}",
        "Son 7 Gün": f"7 gün · {gun(week_start)} – {gun(d)}",
        "Bu Ay": (f"Ayın geçen kısmı · {gun(month_start)} – {gun(d)}"
                  if month_start != d else f"Ayın ilk günü · {gun(d)}"),
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
                    num(row[D2C_COL["revenue"]]) or 0,
                    int(num(row[D2C_COL["orders"]]) or 0),
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

    # Veri dünle biter; "dün"ü senkron damgasından türetiyoruz.
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", updated)
    synced = date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else date.today()
    ranges = period_ranges(synced - timedelta(days=1))

    periods = [
        {"id": o, "label": labels[o], "range": ranges.get(labels[o], "")}
        for o in sorted(labels)
    ]

    data_block = (
        f"const UPDATED = {json.dumps(updated)};\n"
        f"const PERIODS = {json.dumps(periods, ensure_ascii=False)};\n"
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
    print(f"dashboard.html yazıldı — {len(rows)} satır, {accounts} hesap, "
          f"{len(periods)} dönem, senkron {updated}")


if __name__ == "__main__":
    main()
