# Tambahkan autentikasi pada Apps Script revisi yang sudah berjalan

Paket Python ini memakai Apps Script revisi yang sudah mendukung `update_status`, `delete`, `add_batch`, dan log. Jangan kembali ke script awal yang hanya menangani dua aksi. Jangan mengganti seluruh Kode.gs dengan potongan di bawah.

1. Buka Extensions → Apps Script dari spreadsheet. Simpan salinan Kode.gs lama sebagai cadangan lokal.
2. Tambahkan helper berikut di bagian paling bawah Kode.gs, di luar fungsi lain:

```javascript
function ptapiAuthorized_(e) {
  var expected = PropertiesService.getScriptProperties().getProperty("SYNC_API_KEY");
  if (!expected || expected.length < 32) return false;
  var supplied = e && e.parameter ? e.parameter.sync_key : "";
  if (e && e.postData && e.postData.contents) {
    try {
      supplied = JSON.parse(e.postData.contents)._sync_key || supplied;
    } catch (err) {
      return false;
    }
  }
  return typeof supplied === "string" && supplied === expected;
}

function ptapiDenied_() {
  return ContentService.createTextOutput(JSON.stringify({
    status: "error", message: "Unauthorized"
  })).setMimeType(ContentService.MimeType.JSON);
}
```

3. Di **baris pertama di dalam** fungsi `doGet(e)`, tambahkan satu baris ini:

```javascript
if (!ptapiAuthorized_(e)) return ptapiDenied_();
```

Hasil susunannya:

```javascript
function doGet(e) {
  if (!ptapiAuthorized_(e)) return ptapiDenied_();
  // Seluruh isi doGet yang sudah ada tetap di bawah baris ini.
}
```

4. Tambahkan baris yang sama di awal `doPost(e)`, sebelum `try` atau operasi apa pun:

```javascript
function doPost(e) {
  if (!ptapiAuthorized_(e)) return ptapiDenied_();
  // Seluruh isi doPost yang sudah ada tetap di bawah baris ini.
}
```

5. Buka Project Settings (ikon roda gigi) → Script Properties → Add script property. Nama: `SYNC_API_KEY`. Nilai: rahasia acak minimal 32 karakter. Gunakan nilai yang SAMA di Environment Variables Vercel. Jangan taruh nilai asli di GitHub.
6. Pastikan script menunjuk tab `DATABASE` secara eksplisit (`getSheetByName`), bukan tab yang kebetulan aktif. Tab `LOG_SCAN` harus sesuai kode revisi. Jangan mengubah header log menjadi susunan lain.
7. Save → Deploy → Manage deployments → pilih Web app aktif → Edit (pensil) → Version: New version → Deploy. Execute as: pemilik script; Who has access: Anyone, jika kebijakan akun perusahaan mengizinkan. Aplikasi memerlukan akses mesin-ke-mesin; helper di atas memeriksa key. Jika opsi Anyone diblokir organisasi, minta admin Workspace menyiapkan akses yang diizinkan; jangan mengakalinya dengan akun pribadi.
8. Salin URL **Web app yang berakhir `/exec`** ke `APPS_SCRIPT_URL`. Bukan URL editor `/edit` dan bukan `/dev`.
9. Nonaktifkan deployment lama lain yang masih memakai kode tanpa pemeriksaan key. Memperbarui satu deployment tidak memperbarui semua deployment lain.
10. Membuka `/exec` langsung tanpa key sekarang semestinya menghasilkan `Unauthorized`; ini normal. Uji koneksi melalui tombol impor dalam aplikasi setelah login admin.

Kode ini tidak mengubah sharing spreadsheet. Key Apps Script tidak melindungi akses langsung ke Sheet yang dibagikan publik; atur akses spreadsheet sesuai pekerja yang memang membutuhkan akses langsung.

Apabila Python lokal lama masih dipakai, ia juga harus mengirim `_sync_key` di JSON POST dan `sync_key` pada query GET, atau gunakan paket Python ini.
