# GitHub → Neon PostgreSQL → Vercel → uji online

## Sebelum mulai

Paket ini dipersiapkan dari kode terbaru untuk deployment; belum diunggah ke repository atau dideploy ke akun Anda. Tes lokal tidak menggantikan uji Neon/Vercel dan Apps Script asli.

Vercel Hobby hanya untuk penggunaan pribadi nonkomersial. Sistem operasional PT Autoplastik Indonesia memerlukan paket yang mengizinkan penggunaan perusahaan, misalnya Pro. Cek biaya/ketentuan dan batas penggunaan di dashboard sebelum memilih paket. Jangan menganggap semua langkah ini menghasilkan layanan gratis tanpa batas.

**Penyimpanan versi ini:** PostgreSQL menyimpan stok operasional, akun dan log. Google Sheets tetap dibaca saat impor master dan menerima salinan perubahan melalui Apps Script. Sheets bukan satu-satunya database. SQLite lokal tidak cocok untuk penyimpanan persisten di Vercel.

Impor pertama mengambil ID, nama, nomor, vendor dan status dari tab DATABASE. Impor berikutnya memperbarui metadata dan menambah ID baru, tetapi **tidak menimpa status ID yang sudah ada**. Perubahan status/NG dilakukan lewat aplikasi. Menghapus baris Sheet tidak menghapus stok online. Akun serta log historis SQLite/LOG_SCAN lama tidak otomatis dimigrasikan. Simpan cadangan lama dan buat akun pekerja baru; untuk riwayat lama perlu migrasi terpisah sebelum menjadikan versi ini catatan operasional utama.

## 1. Upload ulang GitHub

1. Ekstrak ZIP ini di komputer. Jangan mengunggah file ZIP/RAR sebagai pengganti source.
2. Buka repository yang sudah ada → tab Code → branch utama (biasanya main) → Add file → Upload files.
3. Dari **dalam folder hasil ekstrak**, pilih semua file dan folder `static`. Seret ke area upload. File `main.py` harus berada langsung di root repository, bukan di folder pembungkus tambahan.
4. File dengan nama DAN lokasi yang sama mengganti versi lama. Pastikan `main.py`, `database.py`, `sync_sheets.py`, `index.html`, `requirements.txt`, `vercel.json` dan `static/logo_api.jpg` sesuai paket ini.
5. Tampilkan hidden files di Windows (View → Show → Hidden items) agar `.gitignore`, `.env.example`, `.python-version` ikut. `.env.example` dalam paket hanya berisi contoh kosong, aman di-upload. **Jangan upload `.env`, database `.db`, atau kredensial asli.** Upload web tidak otomatis menerapkan .gitignore sebagai filter.
6. Isi commit message: `Prepare online tracking with PostgreSQL and Vercel`. Pilih commit ke branch utama jika repository milik sendiri dan tidak mewajibkan PR. Klik Commit changes.
7. Dokumentasi dalam paket ikut diupload agar petunjuk lama yang melarang Vercel tidak membingungkan. Jangan menghapus repository atau riwayat commit. Jika ada entrypoint/config lama tambahan di luar paket ini, periksa agar tidak bersaing dengan `main.py` dan preset FastAPI.

## 2. Buat database online di Neon

1. Buka https://console.neon.tech dan buat akun/login.
2. Create project, misalnya `pt-api-tracking`. Pilih region yang dekat pengguna dan nantinya sedekat mungkin dengan region function Vercel. Gunakan database PostgreSQL **baru/kosong**, bukan database proyek lain.
3. Buka **Connect**. Pilih branch/database/role proyek itu, aktifkan **Connection pooling**, lalu salin connection string `postgresql://...` dengan SSL dari dashboard. Jangan salin perintah shell `psql '...'`, hanya URL di dalamnya.
4. Simpan URL tersebut untuk `DATABASE_URL`. URL mengandung password: jangan taruh di chat, screenshot, atau GitHub. Jangan hilangkan parameter SSL dari URL.
5. Tidak perlu membuat tabel manual: aplikasi membuat tabel dan admin awal saat pertama dijalankan.

## 3. Siapkan Apps Script

Ikuti `PATCH_APPS_SCRIPT.md` sampai deploy **New version** selesai. Ini menambahkan pemeriksaan key pada doGet dan doPost yang sudah Anda perbaiki. Salin URL `/exec` dan simpan `SYNC_API_KEY` yang sama untuk Vercel.

Buat dua rahasia berbeda di terminal VS Code, jalankan perintah ini dua kali:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Hasil pertama untuk SESSION_SECRET; hasil kedua untuk SYNC_API_KEY. Simpan secara pribadi; jangan commit hasilnya.

## 4. Import repository ke Vercel

1. Buka https://vercel.com → login dengan GitHub yang memiliki repository tadi. Pilih akun/team dan paket yang mengizinkan penggunaan ini.
2. Dashboard → **Add New → Project**.
3. Di Import Git Repository, pilih repository → **Import**. Jika tidak muncul, atur izin GitHub App Vercel pada akun yang benar dan berikan akses repository tersebut.
4. Project Name misalnya `pt-api-tracking`. Nama domain akhir mengikuti ketersediaan.
5. Framework Preset: **FastAPI**. Root Directory: `./` jika main.py berada di root.
6. Biarkan Build Command, Install Command, Output Directory pada default preset. Jangan mengisi `uvicorn main:app` sebagai Build Command. Python 3.12 ditentukan oleh `.python-version`; dependensi ada di requirements.txt.
7. Buka **Environment Variables**, masukkan nilai berikut untuk **Production**. Jangan gunakan database produksi pada deployment Preview; untuk Preview buat database dan key terpisah jika diperlukan.

| Nama | Nilai yang dimasukkan |
|---|---|
| DATABASE_URL | URL PostgreSQL pooled dari Neon, lengkap dengan SSL |
| SESSION_SECRET | Rahasia acak pertama, minimal 32 karakter |
| INITIAL_ADMIN_USERNAME | Username admin awal, misalnya admin_api; 3–50 huruf/angka/titik/underscore/minus |
| INITIAL_ADMIN_PASSWORD | Password baru 12–256 karakter; jangan gunakan contoh atau password lama yang dibagikan |
| INITIAL_ADMIN_NAME | Nama tampilan admin |
| APPS_SCRIPT_URL | URL Web app Apps Script berakhir /exec |
| SYNC_API_KEY | Rahasia acak kedua, sama persis dengan Script Properties |
| GOOGLE_SHEET_URL | URL spreadsheet untuk tombol buka Sheet; boleh kosong bila tombol tidak diperlukan |

8. Klik **Deploy**, tunggu status **Ready**. Jika gagal, baca Build Logs. Jangan membagikan secret saat mengirim screenshot error.
9. Buka **Visit** atau domain Production pada Settings → Domains. Gunakan alamat `https://...vercel.app` yang diberikan Vercel, bukan nama contoh dalam panduan.
10. Jika environment diperbaiki setelah deployment: Settings → Environment Variables → Save → Deployments → pilih deployment Production → **Redeploy**. Nilai environment baru hanya dipakai deployment baru.

## 5. Uji aplikasi sampai dapat dipakai pekerja

1. Buka `https://DOMAIN-ANDA/api/health`. Target respons: `{"status":"ok","database":"postgresql"}`. Jika error, cek Runtime Logs, DATABASE_URL, dan admin awal.
2. Buka halaman utama, login memakai INITIAL_ADMIN_USERNAME/PASSWORD. Admin dibuat hanya saat tabel users kosong. Mengganti env password setelah itu **tidak mereset password akun yang sudah ada**.
3. Menu Master Data → **Refresh & Sync dari Sheets**. Cocokkan beberapa ID dan total dengan DATABASE. Data NG tetap berstatus NG dan tidak dihitung stok aktif.
4. Menu kelola akun → buat akun pekerja dengan role **member/operator**; gunakan guest untuk baca saja. Password tidak bisa dilihat kembali di tabel.
5. Buat 2 unit uji dengan prefix unik, misalnya `TRIAL2026`, tanpa mengubah ID asli. Penambahan harus muncul pada master, overview, dan log `TAMBAH STOK`.
6. Scan satu unit dalam urutan PT API Out → Vendor In → Vendor Out → PT API In. Scan urutan salah/duplikat harus ditolak.
7. Hapus salah satu unit uji karena NG dengan alasan `Uji sistem`. Total aktif berkurang; master menyimpan status DELETED_NG; log mencatat DIHAPUS dan alasan. Tindakan ini soft delete, bukan menghapus riwayat.
8. Pada Overview admin, tunggu indikator **seluruh pengiriman terkonfirmasi**. Cocokkan status unit uji di DATABASE dan peristiwa scan/NG di LOG_SCAN. Log tambah pada PostgreSQL tersedia; salinan log tambah Sheets mengikuti kemampuan Apps Script revisi yang Anda gunakan.
9. Buka domain yang sama dari HP/jaringan lain dan login akun operator. Biarkan Overview terbuka di perangkat pertama. Perubahan terlihat pada refresh berikutnya (sekitar 10 detik ditambah waktu jaringan), bukan push realtime.
10. Izinkan kamera pada HTTPS dan uji QR fisik. Jika kamera tidak tersedia, gunakan input ID manual. UI kamera dan pencetakan perlu diuji pada perangkat kerja sebenarnya.
11. Refresh browser/logout-login dan lakukan satu **Redeploy**. Stok/akun/log harus tetap ada karena tersimpan di Neon. Jangan mengosongkan atau membuat ulang database saat redeploy.
12. Bagikan **domain Production yang sama** kepada pekerja beserta akun masing-masing. Bila muncul login Vercel alih-alih login aplikasi, periksa Settings → Deployment Protection dan kebijakan akses Production. Sesuaikan agar pekerja yang dituju dapat masuk, sambil mempertahankan login aplikasi. Preview boleh tetap dibatasi.

## 6. Jika salinan Sheets tertunda

Transaksi stok dan log utama disimpan bersama di PostgreSQL. Pengiriman salinan diproses satu per satu setelah perubahan, dan dilanjutkan saat admin membuka/memperbarui Overview/Master. Tidak ada worker/cron yang selalu berjalan saat aplikasi ditutup. Antrean tersimpan di database dan tidak hilang saat redeploy.

Status `pending`: belum dikirim. `sending`: sedang dikirim atau belum memperoleh hasil pasti. `review`: respons gagal/tidak pasti. Bila status tidak pasti, antrean berikutnya berhenti agar urutan status tidak terbalik. Stok online tetap dapat digunakan, tetapi Sheets belum tentu terbaru.

Gunakan **Periksa pengiriman** ketika tersedia. Periksa payload, baris DATABASE dan LOG_SCAN secara langsung. Bila sebagian tersimpan, lengkapi/rekonsiliasi terlebih dahulu. Pilih **Sudah lengkap di Sheets** hanya setelah keduanya benar. Pilih **Belum tersimpan; kirim ulang** hanya bila seluruh transaksi belum ditulis; pengulangan sembarang dapat menghasilkan log ganda. Untuk status sending yang macet, tombol tersedia setelah 10 menit agar pengiriman lama sempat selesai. Ini pemeriksaan manual, bukan jaminan transaksi atomik lintas dua layanan.

## Batas trial ini

- Fitur penutupan unloading massal dinonaktifkan karena versi awal menghitung unit lintas semua pengiriman; perlu ID pengiriman sebelum digunakan beberapa truk/lokasi. Scan penerimaan tetap berfungsi.
- Overview menampilkan paling banyak 500 log terbaru; riwayat lain tetap tersimpan di PostgreSQL.
- Tidak ada impor otomatis akun/log SQLite lama, reset password mandiri, atau pemulihan NG. Pengaturan INITIAL_ADMIN hanya untuk bootstrap database baru.
- Import master menambah data dan metadata, bukan sinkronisasi dua arah status yang diedit langsung di Sheet.
- Login, tambah/scan/NG, role dan antrean diuji lokal; deploy Vercel, koneksi Neon/Apps Script asli, kamera dan beban pekerja harus diverifikasi dengan langkah di atas sebelum operasional penuh.

## Menjalankan lokal

Gunakan folder baru agar tidak menimpa DB SQLite lama. Python 3.12 → `python -m pip install -r requirements.txt` → salin .env.example menjadi .env dan isi admin/password/secret → `python -m uvicorn main:app --reload`. DATABASE_URL kosong hanya untuk tes SQLite lokal. Jangan hubungkan tes eksperimen ke database operasional tanpa sengaja.

## Sumber platform

- https://vercel.com/docs/plans/hobby
- https://vercel.com/docs/frameworks/backend/fastapi
- https://vercel.com/docs/environment-variables
- https://vercel.com/docs/functions/runtimes/python
- https://vercel.com/kb/guide/is-sqlite-supported-in-vercel
- https://neon.com/docs/get-started/connect-neon
- https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository
