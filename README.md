# PT API Tracking Online

Paket FastAPI untuk PostgreSQL online dan Vercel. Mulai dari **PANDUAN_VERCEL.md**, lalu ikuti **PATCH_APPS_SCRIPT.md**. Kode sudah disesuaikan, tetapi paket ini belum dideploy ke akun Anda.

Google Sheets menjadi sumber impor master serta penerima salinan perubahan; PostgreSQL adalah sumber transaksi operasional, akun, dan log. Jangan memakai SQLite sebagai database persisten Vercel. Vercel Hobby hanya untuk penggunaan pribadi nonkomersial; sistem perusahaan memerlukan paket yang sesuai.

Konfigurasi memakai environment, bukan kredensial di kode. .env.example hanya contoh. Tidak ada database asli, password, atau executable dalam paket.

Perubahan penting dan batas trial: lihat CATATAN_PERUBAHAN.md. Stok/akun/log lama SQLite tidak otomatis dipindahkan.
