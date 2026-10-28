"""
Günlük seri toplayıcı: izleme panelinin ham verisi.

Eski panel (meta-ads-sheets-sync) sabit dönem toplamları yazıyordu; bu panel
hedef takibi ve günlük grafikler için GÜN GÜN seri istiyor. Kaynaklar aynı:
- Trendyol / Hepsiburada: CPAS hesaplarının Meta catalog_segment metrikleri
- D2C: Meta (web hesabı) + Google Ads (betiğin Sheet sekmesi) harcaması,
  Shopify sipariş ve cirosu

Günler Europe/Istanbul takvimine göre. Panel son 3 ayı gösterir ve her ayı bir
önceki ayla karşılaştırır, bu yüzden pencere 3 ay önceki ayın ilk gününden
bugüne (bugün dahil, yarım gün) uzanır.

Shopify token'ı (read_orders) yalnız son 60 günü okuyabiliyor. Daha eski günler
Sheet'teki "Shopify Günlük" sekmesinde biriken kayıttan gelir: her çalışma son
59 günü canlı çekip o sekmeye yazar, yani bir gün 60 günü geçince değeri
kaybolmaz, son çekildiği hâliyle kalır. Hiç kaydı olmayan gün None'dır (0 değil).

Hedefler Sheet'teki "Hedefler" sekmesinden okunur (panel, Apps Script
üzerinden oraya yazar) — derlenen sayfa en son kaydedilen hedeflerle açılır.
"""

import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import gspread
import requests

import sync
from sync import (AD_ACCOUNTS, D2C_META_ACCOUNTS, D2C_SHOPIFY_ENV_VARS, GRAPH_API_VERSION,
                  ISTANBUL, SHOPIFY_API_VERSION, _find_action_value)

SHOPIFY_MAX_DAYS = 59
SHOPIFY_CACHE_TAB = "Shopify Günlük"
SHOPIFY_CACHE_HEADER = ["Tarih", "Marka", "Ciro", "Sipariş", "Güncelleme"]
MONTHS_BACK = 3  # gösterilen en eski ayın da bir önceki ayı
TARGETS_TAB = "Hedefler"
TARGETS_HEADER = ["Ay", "Kanal", "Marka", "Ciro Hedefi", "Sipariş Hedefi", "Güncelleme"]


def window():
    """[ilk gün, bugün] — ikisi de dahil, İstanbul takvimi."""
    today = datetime.now(ISTANBUL).date()
    first = today.replace(day=1)
    for _ in range(MONTHS_BACK):
        first = (first - timedelta(days=1)).replace(day=1)
    return first, today


def day_list(first, last):
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def meta_daily(ad_account_id, token, first, last, fields):
    """Graph API'den time_increment=1 ile gün başına satırlar: {gün: satır}."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/act_{ad_account_id}/insights"
    params = {
        "fields": ",".join(fields),
        "time_range": f'{{"since":"{first}","until":"{last}"}}',
        "time_increment": 1,
        "limit": 100,
        "access_token": token,
    }
    out = {}
    while url:
        resp = requests.get(url, params=params, timeout=60)
        resp.raise_for_status()
        body = resp.json()
        for row in body.get("data", []):
            out[row["date_start"]] = row
        url = body.get("paging", {}).get("next")
        params = None  # next URL parametreleri taşıyor
    return out


def fnum(v):
    return float(v) if v not in (None, "") else 0.0


def cpas_series(days, first, last):
    """{"trendyol"|"hepsiburada": {marka: {seri: [gün gün]}}}"""
    out = {"trendyol": {}, "hepsiburada": {}}
    fields = ["spend", "impressions", "inline_link_clicks",
              "catalog_segment_actions", "catalog_segment_value"]
    for brand, channels in AD_ACCOUNTS.items():
        token = sync.resolve_access_token(brand)
        for channel, account in channels.items():
            print(f"CPAS günlük: {brand} / {channel}")
            rows = meta_daily(account, token, first, last, fields)
            s = defaultdict(list)
            for d in days:
                r = rows.get(d, {})
                s["spend"].append(round(fnum(r.get("spend")), 2))
                s["impressions"].append(int(fnum(r.get("impressions"))))
                s["clicks"].append(int(fnum(r.get("inline_link_clicks"))))
                acts, vals = r.get("catalog_segment_actions"), r.get("catalog_segment_value")
                s["orders"].append(int(_find_action_value(acts, ["omni_purchase", "purchase"]) or 0))
                s["revenue"].append(round(_find_action_value(vals, ["omni_purchase", "purchase"]) or 0, 2))
            out[channel.lower()][brand] = dict(s)
    return out


def shopify_daily(shop, token, first, last):
    """İptal edilmemiş siparişler, İstanbul gününe göre: {gün: [ciro, adet]}."""
    since = datetime.combine(first, datetime.min.time(), ISTANBUL)
    until = datetime.combine(last + timedelta(days=1), datetime.min.time(), ISTANBUL)
    url = f"https://{shop}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    params = {
        "status": "any",
        "created_at_min": since.isoformat(),
        "created_at_max": (until - timedelta(seconds=1)).isoformat(),
        "fields": "created_at,total_price,cancelled_at",
        "limit": 250,
    }
    headers = {"X-Shopify-Access-Token": token}
    out = defaultdict(lambda: [0.0, 0])
    while url:
        resp = requests.get(url, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        for o in resp.json().get("orders", []):
            if o.get("cancelled_at"):
                continue
            day = datetime.fromisoformat(o["created_at"]).astimezone(ISTANBUL).date().isoformat()
            out[day][0] += float(o["total_price"])
            out[day][1] += 1
        url = None
        for part in resp.headers.get("Link", "").split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
        params = None
    return out


def d2c_series(spreadsheet, days, first, last):
    """{marka: {metaSpend, googleSpend|None, revenue|None, orders|None, clicks, impressions}}
    None = o kaynak bağlı değil (0 ile karışmasın)."""
    out = {}
    cache = read_shopify_cache(spreadsheet)
    for brand, account in D2C_META_ACCOUNTS.items():
        print(f"D2C günlük: {brand}")
        token = sync.resolve_d2c_meta_token(brand, account)
        meta = meta_daily(account, token, first, last, ["spend", "impressions", "clicks"])
        s = {
            "metaSpend": [round(fnum(meta.get(d, {}).get("spend")), 2) for d in days],
            "impressions": [int(fnum(meta.get(d, {}).get("impressions"))) for d in days],
            "clicks": [int(fnum(meta.get(d, {}).get("clicks"))) for d in days],
        }

        google = sync.read_google_ads_daily(spreadsheet, brand)
        s["googleSpend"] = None if google is None else [
            round(google.get(datetime.fromisoformat(d).date(), 0.0), 2) for d in days]

        _, shop_var = D2C_SHOPIFY_ENV_VARS[brand]
        shop = os.environ.get(shop_var)
        try:
            shop_token = sync.resolve_shopify_token(brand, shop) if shop else None
        except requests.HTTPError as exc:
            print(f"  HATA (Shopify token): {brand}: {exc.response.text[:200]}", file=sys.stderr)
            shop_token = None
        if shop_token:
            stamp = datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M")
            live_first = max(first, last - timedelta(days=SHOPIFY_MAX_DAYS))
            live = shopify_daily(shop, shop_token, live_first, last)
            # Canlı penceredeki her gün kayda girer — siparişsiz gün 0 olarak.
            for d in day_list(live_first, last):
                cache[(d, brand)] = (round(live[d][0], 2), live[d][1], stamp) if d in live else (0.0, 0, stamp)
            s["revenue"] = [cache[(d, brand)][0] if (d, brand) in cache else None for d in days]
            s["orders"] = [cache[(d, brand)][1] if (d, brand) in cache else None for d in days]
        else:
            print(f"  UYARI: {brand} için Shopify erişimi yok; ciro boş.", file=sys.stderr)
            s["revenue"] = s["orders"] = None
        out[brand] = s
    write_shopify_cache(spreadsheet, cache)
    return out


def read_shopify_cache(spreadsheet):
    """{(gün, marka): (ciro, sipariş, son çekiliş)} — sekme yoksa boş."""
    try:
        ws = spreadsheet.worksheet(SHOPIFY_CACHE_TAB)
    except gspread.WorksheetNotFound:
        return {}
    out = {}
    for row in ws.get_all_values(value_render_option="UNFORMATTED_VALUE")[1:]:
        if len(row) >= 4 and str(row[0]).strip():
            stamp = str(row[4]) if len(row) > 4 else ""
            out[(str(row[0]).strip(), str(row[1]).strip())] = (float(row[2] or 0), int(row[3] or 0), stamp)
    return out


def write_shopify_cache(spreadsheet, cache) -> None:
    if not cache:
        return
    try:
        ws = spreadsheet.worksheet(SHOPIFY_CACHE_TAB)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SHOPIFY_CACHE_TAB, rows=2000, cols=len(SHOPIFY_CACHE_HEADER))
    rows = [SHOPIFY_CACHE_HEADER] + [[d, b, rev, n, st] for (d, b), (rev, n, st) in sorted(cache.items())]
    if ws.row_count < len(rows):
        ws.resize(rows=len(rows) + 500)
    ws.update(range_name="A1", values=rows, value_input_option="RAW")


def month_key(cell) -> str:
    """Hedefler sekmesindeki ay hücresi: metin "2026-10" ya da Sheets'in tarihe
    çevirdiği hâli (biçimlenmemiş okumada 1899-12-30'dan beri gün sayısı)."""
    if isinstance(cell, (int, float)):
        return (datetime(1899, 12, 30) + timedelta(days=cell)).strftime("%Y-%m")
    return str(cell).strip()


def read_targets(spreadsheet):
    """{"2026-10|trendyol|Frime": {"revenue": 500000, "orders": 300}}"""
    try:
        ws = spreadsheet.worksheet(TARGETS_TAB)
    except gspread.WorksheetNotFound:
        return {}
    out = {}
    for row in ws.get_all_values(value_render_option="UNFORMATTED_VALUE")[1:]:
        if len(row) < 5 or not str(row[0]).strip():
            continue
        key = f"{month_key(row[0])}|{str(row[1]).strip()}|{str(row[2]).strip()}"
        out[key] = {
            "revenue": float(row[3]) if row[3] not in ("", None) else None,
            "orders": float(row[4]) if row[4] not in ("", None) else None,
        }
    return out


def collect():
    first, last = window()
    days = day_list(first, last)
    spreadsheet = sync.open_spreadsheet()
    cpas = cpas_series(days, first, last)
    return {
        "updated": datetime.now(ISTANBUL).strftime("%Y-%m-%d %H:%M"),
        "today": last.isoformat(),
        "days": days,
        "trendyol": cpas["trendyol"],
        "hepsiburada": cpas["hepsiburada"],
        "d2c": d2c_series(spreadsheet, days, first, last),
        "targets": read_targets(spreadsheet),
    }
