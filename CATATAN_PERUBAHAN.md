# Perubahan untuk trial online

- FastAPI main.py sebagai entrypoint Vercel; dependency dan Python 3.12 disertakan.
- PostgreSQL persisten via DATABASE_URL, lock transaksi untuk update/nomor unit serentak. SQLite hanya fallback lokal.
- Login cookie HttpOnly, pemeriksaan role pada server, password PBKDF2, throttle gagal login, pemeriksaan header mutasi. Password tidak lagi ditampilkan/dikirim pada daftar akun.
- Admin awal dari environment; pengguna dan riwayat database lama tidak diimpor otomatis.
- Tambah, scan dan NG mencatat log operasional dalam transaksi database yang sama. NG tidak masuk total stok aktif atau daftar cetak QR.
- Queue salinan Sheets persisten, indikator pending/sending/review, penanganan manual hasil tidak pasti; bukan transaksi atomik lintas PostgreSQL dan Google Sheets.
- Impor Sheets hanya menimpa metadata ID lama. Status operasional tidak ditimpa. ID duplikat/format tidak valid ditolak.
- Patch key Apps Script, URL/secret lewat environment. Tidak memakai token di HTML.
- Overview auto-refresh 10 detik; maksimal 500 log terakhir ditampilkan dengan waktu WIB.
- Penutupan unloading global dinonaktifkan: belum ada pengelompokan pengiriman, sehingga truk lain bisa salah ditandai hilang. Scan penerimaan tetap tersedia.
- Tidak menjamin deploy atau realtime push: uji akun platform, perangkat kamera, sinkronisasi script nyata dan beban operasional masih diperlukan.
