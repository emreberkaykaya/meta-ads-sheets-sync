/**
 * Hedef kaydı — panelin Sheet'ine bağlı Apps Script web uygulaması.
 *
 * Panel hedefleri buraya POST eder, açılışta GET ile okur; kalıcı kayıt
 * Sheet'in "Hedefler" sekmesi. veri.py aynı sekmeyi okuyup derlenen sayfaya
 * da gömer, yani API'ye ulaşılamasa bile son kaydedilen hedefler görünür.
 *
 * Kurulum: panel Sheet'i → Uzantılar → Apps Script → bu dosyayı yapıştır →
 * Dağıt → Yeni dağıtım → Web uygulaması → "Ben olarak
 * çalıştır", erişim "Herkes" → Yetkilendir → çıkan /exec adresi
 * HEDEF_API_URL secret'ına.
 *
 * Yazma bilerek anahtarsız: paneli yalnız ekip kullanıyor, hedefi kim açarsa
 * girebilsin. Sadece biçim doğrulanır (ay, kanal, marka, negatif olmayan sayı).
 */

const SEKME = "Hedefler";
const BASLIK = ["Ay", "Kanal", "Marka", "Ciro Hedefi", "Sipariş Hedefi", "Güncelleme"];
const KANALLAR = ["trendyol", "d2c"];
const MARKALAR = ["Aqua di Polo", "Frime", "Pierre Cardin", "MissLiva"];

function sekme_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(SEKME);
  if (!sh) {
    sh = ss.insertSheet(SEKME);
    sh.appendRow(BASLIK);
    sh.getRange("A:A").setNumberFormat("@"); // "2026-10" tarihe dönmesin
  }
  return sh;
}

// Sheets "2026-10"u tarihe çevirebiliyor; hücre ne olursa olsun "yyyy-MM" döner.
// getValues()'un Date'leri `instanceof Date` testini geçmiyor, tür etiketine bakılır.
function ay_(v) {
  return Object.prototype.toString.call(v) === "[object Date]"
    ? Utilities.formatDate(v, "Europe/Istanbul", "yyyy-MM") : String(v);
}

function oku_() {
  const out = {};
  sekme_().getDataRange().getValues().slice(1).forEach(r => {
    if (!r[0]) return;
    out[`${ay_(r[0])}|${r[1]}|${r[2]}`] = {
      revenue: r[3] === "" ? null : Number(r[3]),
      orders: r[4] === "" ? null : Number(r[4]),
    };
  });
  return out;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function sayiMi_(v) {
  return v === null || (typeof v === "number" && isFinite(v) && v >= 0);
}

function doGet() {
  return json_({ ok: true, targets: oku_() });
}

function doPost(e) {
  let b;
  try {
    b = JSON.parse(e.postData.contents);
  } catch (err) {
    return json_({ ok: false, error: "Geçersiz istek" });
  }
  if (!/^\d{4}-\d{2}$/.test(b.month) || KANALLAR.indexOf(b.channel) < 0 ||
      MARKALAR.indexOf(b.brand) < 0 || !sayiMi_(b.revenue) || !sayiMi_(b.orders)) {
    return json_({ ok: false, error: "Geçersiz hedef" });
  }

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const sh = sekme_();
    const rows = sh.getDataRange().getValues();
    const stamp = Utilities.formatDate(new Date(), "Europe/Istanbul", "yyyy-MM-dd HH:mm");
    const row = [b.month, b.channel, b.brand,
                 b.revenue === null ? "" : b.revenue,
                 b.orders === null ? "" : b.orders, stamp];
    const i = rows.findIndex((r, k) => k > 0 && ay_(r[0]) === b.month &&
                                       r[1] === b.channel && r[2] === b.brand);
    const at = i > 0 ? i + 1 : sh.getLastRow() + 1;
    sh.getRange(at, 1).setNumberFormat("@"); // ay metin olarak yazılsın
    sh.getRange(at, 1, 1, row.length).setValues([row]);
  } finally {
    lock.releaseLock();
  }
  return json_({ ok: true, targets: oku_() });
}
