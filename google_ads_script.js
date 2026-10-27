/**
 * Google Ads Script — D2C paneline Google Ads harcamasını taşır.
 *
 * Her Google Ads hesabına (Frime, Aqua di Polo) ayrı ayrı kurulur; yalnızca
 * BRAND değişir. Son DAYS günün günlük harcamasını panelin Sheet'inde
 * "Google Ads - <BRAND>" sekmesine yazar (her çalışmada sekmeyi temizleyip
 * baştan yazar). Dönem toplamlarını sync.py, Meta ile aynı period_bounds()
 * sınırlarıyla hesaplar — dönem tanımı tek yerde kalsın diye burada toplam
 * yapılmıyor.
 *
 * Kurulum: Google Ads → Araçlar → Toplu işlemler → Komut dosyaları → + →
 * bu dosyayı yapıştır → BRAND'i ayarla → Yetkilendir → Önizle → zamanlamayı
 * "Saatlik" yap. Sheet'e, betiği yetkilendiren Google hesabının editör
 * erişimi olmalı.
 */

const SHEET_ID = "1REPLACE_WITH_GOOGLE_SHEET_ID";
const BRAND = "Frime"; // ya da "Aqua di Polo" — sync.py'deki D2C marka adıyla birebir aynı
// İzleme paneli 3 ay + karşılaştırma için 4 ay geriye bakar (en fazla ~123 gün).
const DAYS = 130;

function main() {
  const tz = AdsApp.currentAccount().getTimeZone();
  const fmt = d => Utilities.formatDate(d, tz, "yyyy-MM-dd");
  const end = new Date();
  const start = new Date(end.getTime() - (DAYS - 1) * 24 * 3600 * 1000);

  const query =
    "SELECT segments.date, metrics.cost_micros, metrics.clicks, " +
    "metrics.impressions, metrics.conversions_value FROM customer " +
    `WHERE segments.date BETWEEN '${fmt(start)}' AND '${fmt(end)}'`;

  const stamp = Utilities.formatDate(new Date(), "UTC", "yyyy-MM-dd HH:mm") + " UTC";
  const rows = [["Tarih", "Harcama", "Tıklama", "Gösterim", "Dönüşüm Değeri",
                 "Saat Dilimi", "Son Güncelleme (UTC)"]];
  const it = AdsApp.search(query);
  while (it.hasNext()) {
    const r = it.next();
    rows.push([
      r.segments.date,
      Number(r.metrics.costMicros || 0) / 1e6,
      Number(r.metrics.clicks || 0),
      Number(r.metrics.impressions || 0),
      Number(r.metrics.conversionsValue || 0),
      tz,
      stamp,
    ]);
  }
  // Hiç gösterim olmayan dönemde de sekme "betik çalıştı" desin: sync.py
  // güncelleme damgasını bu satırdan okuyabiliyor.
  if (rows.length === 1) rows.push(["", 0, 0, 0, 0, tz, stamp]);

  const ss = SpreadsheetApp.openById(SHEET_ID);
  const name = "Google Ads - " + BRAND;
  const sheet = ss.getSheetByName(name) || ss.insertSheet(name);
  sheet.clearContents();
  // Tarih metin kalsın; Sheets "2026-10-25"i tarihe çevirip yerel biçimde
  // döndürürse sync.py okuyamaz.
  sheet.getRange(1, 1, rows.length, 1).setNumberFormat("@");
  sheet.getRange(1, 1, rows.length, rows[0].length).setValues(rows);
  Logger.log(`${name}: ${rows.length - 1} gün yazıldı (${tz}).`);
}
