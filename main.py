import datetime
from fastapi import FastAPI, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import os
import requests
import re

from database import SessionLocal, Sarana, LogScan, User, init_db
from sync_sheets import import_data

app = FastAPI(title="System Tracking Stok PT. Autoplastik Indonesia")
app.mount("/static", StaticFiles(directory="."), name="static")
APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbwkYxvFb2L7HwlChAk_WpEAc6XqE5ueGM_h6_dp0pn0MjHiP6eaTGgwbskhm_uIcPwSXQ/exec"

def post_to_google_sheets(payload: dict):
    if not APPS_SCRIPT_URL or "PASTE_URL" in APPS_SCRIPT_URL:
        raise RuntimeError("URL Apps Script belum diatur.")

    response = requests.post(
        APPS_SCRIPT_URL,
        json=payload,
        timeout=30,
        allow_redirects=True
    )

    response.raise_for_status()

    try:
        result = response.json()
    except ValueError as error:
        raise RuntimeError(
            "Apps Script tidak membalas JSON. "
            "Periksa URL deployment dan pengaturan akses."
        ) from error

    if not isinstance(result, dict):
        raise RuntimeError("Format balasan Apps Script tidak valid.")

    if result.get("status") != "success":
        message = result.get(
            "message",
            "Google Sheets menolak perubahan."
        )

        if result.get("needs_review"):
            message += (
                " Sebagian penulisan mungkin sudah terjadi; "
                "periksa DATABASE dan LOG_SCAN sebelum mengulang."
            )

        raise RuntimeError(message)

    return result

ALUR_DISPLAY_NAME = {
    "1": "PT API Out ➔ In Truck",
    "2": "In Truck ➔ Vendor In",
    "3": "Vendor Out ➔ In Truck",
    "4": "In Truck ➔ PT API In"
}

STATUS_MAP = {
    "1": "IN_TRANSIT_OUT",
    "2": "AT_VENDOR",
    "3": "IN_TRANSIT_IN",
    "4": "AT_API"
}

@app.on_event("startup")
def startup_event():
    init_db()
    try:
        import_data()
    except Exception as e:
        print(f"[STARTUP WARNING] {e}")

def send_to_google_sheets(id_sarana: str, status: str):
    try:
        post_to_google_sheets({
            "action": "update_status",
            "id_sarana": id_sarana,
            "status": status
        })
    except Exception as error:
        print(f"[SYNC STATUS ERROR] {error}")

@app.get("/", response_class=HTMLResponse)
def read_root():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>File index.html tidak ditemukan!</h1>"

@app.post("/api/login")
def login(username: str = Form(...), password: str = Form(...)):
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(username=username, password=password).first()
        if not user:
            return JSONResponse(status_code=401, content={"status": "error", "message": "Username atau Password salah!"})
        
        return {
            "status": "success",
            "user": {
                "username": user.username,
                "role": user.role,
                "nama_lengkap": user.nama_lengkap
            }
        }
    finally:
        session.close()

@app.get("/api/admin/database")
def get_admin_database():
    session = SessionLocal()
    try:
        master = session.query(Sarana).all()
        logs = session.query(LogScan).order_by(LogScan.id.desc()).all()
        
        master_list = [
            {
                "ID_Sarana": s.ID_Sarana, 
                "Nama_Sarana": s.Nama_Sarana, 
                "No_Sarana": s.No_Sarana, 
                "Vendor": s.Vendor or "-", 
                "Status_Saat_Ini": s.Status_Saat_Ini,
                "Tujuan_Akhir": ""
            } for s in master
        ]
        
        log_list = []
        for l in logs:
            waktu_str = l.Waktu_Scan.strftime("%Y-%m-%d %H:%M:%S") if getattr(l, 'Waktu_Scan', None) else "-"
            log_list.append({
                "id": l.id,
                "ID_Sarana": l.ID_Sarana,
                "Alur_Scan": l.Alur_Scan,
                "Vendor": l.Vendor or "-",
                "Tujuan_Akhir": "",
                "Waktu_Scan": waktu_str
            })
        
        return {"master": master_list, "logs": log_list}
    finally:
        session.close()

# Fungsi pembantu untuk menambah baris ke Google Sheets secara asynchronous/background
def append_batch_to_google_sheets(items: list):
    try:
        post_to_google_sheets({
            "action": "add_batch",
            "items": items
        })
    except Exception as error:
        print(f"[SYNC ADD ERROR] {error}")

@app.post("/api/admin/sarana/add-batch")
def add_batch_sarana(
    background_tasks: BackgroundTasks,
    prefix_id: str = Form(...),
    nama_sarana: str = Form(...),
    qty: int = Form(...),
    vendor: str = Form(...)
):
    session = SessionLocal()
    try:
        prefix_clean = prefix_id.strip().upper()
        nama_clean = nama_sarana.strip()
        vendor_clean = vendor.strip().upper() or "-"

        if qty <= 0:
            return JSONResponse(status_code=400, content={"status": "error", "message": "Jumlah QTY harus lebih dari 0!"})

        # Cari nomor urut terakhir di database lokal untuk prefix ini
        existing_saranas = session.query(Sarana).filter(Sarana.ID_Sarana.like(f"{prefix_clean}-%")).all()
        
        max_num = 0
        for s in existing_saranas:
            # Ambil angka setelah tanda `-` terakhir (contoh: BOX-A-003 -> 3)
            match = re.search(r'-(\d+)$', s.ID_Sarana)
            if match:
                num = int(match.group(1))
                if num > max_num:
                    max_num = num

        new_saranas = []
        items_for_sheets = []

        for i in range(1, qty + 1):
            next_num = max_num + i
            no_sarana_str = str(next_num)
            id_sarana_str = f"{prefix_clean}-{next_num:03d}" # Format 3 digit, misal: BOX-A-001

            new_sarana = Sarana(
                ID_Sarana=id_sarana_str,
                Nama_Sarana=nama_clean,
                No_Sarana=no_sarana_str,
                Vendor=vendor_clean,
                Status_Saat_Ini="AT_API"
            )
            new_saranas.append(new_sarana)
            items_for_sheets.append({
                "ID_Sarana": id_sarana_str,
                "Nama_Sarana": nama_clean,
                "No_Sarana": no_sarana_str,
                "Vendor": vendor_clean,
                "Status_Saat_Ini": "AT_API"
            })

        session.add_all(new_saranas)
        session.commit()

        # Kirim data ke Google Sheets di background
        background_tasks.add_task(append_batch_to_google_sheets, items_for_sheets)

        return {
            "status": "success",
            "message": f"Berhasil menambahkan {qty} unit '{nama_clean}' ({items_for_sheets[0]['ID_Sarana']} s/d {items_for_sheets[-1]['ID_Sarana']})!"
        }
    except Exception as e:
        session.rollback()
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    finally:
        session.close()

@app.get("/api/admin/users")
def get_admin_users():
    session = SessionLocal()
    try:
        users = session.query(User).all()
        user_list = [
            {
                "id": u.id,
                "username": u.username,
                "password": u.password or "-",
                "role": u.role,
                "nama_lengkap": u.nama_lengkap,
                "npk": u.npk or "-",
                "jabatan": u.jabatan or "-"
            } for u in users
        ]
        return {"users": user_list}
    finally:
        session.close()

@app.post("/api/admin/users/create")
def create_user(
    nama_lengkap: str = Form(...),
    username: str = Form(...),
    npk: str = Form(...),
    jabatan: str = Form(...),
    role: str = Form(...),
    password: str = Form(...)
):
    session = SessionLocal()
    try:
        existing_user = session.query(User).filter_by(username=username).first()
        if existing_user:
            return JSONResponse(status_code=400, content={"status": "error", "message": f"Username '{username}' sudah digunakan!"})

        new_user = User(
            nama_lengkap=nama_lengkap,
            username=username,
            npk=npk,
            jabatan=jabatan,
            role=role,
            password=password
        )
        session.add(new_user)
        session.commit()
        return {"status": "success", "message": f"Akun pengguna '{nama_lengkap}' ({username}) berhasil dibuat!"}
    except Exception as e:
        session.rollback()
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    finally:
        session.close()

@app.delete("/api/admin/users/delete/{user_id}")
def delete_user(user_id: int):
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            return JSONResponse(status_code=404, content={"status": "error", "message": "Akun pengguna tidak ditemukan!"})

        if user.username == "najib_rzq":
            return JSONResponse(status_code=400, content={"status": "error", "message": "Akun Utama Admin ('najib_rzq') tidak dapat dihapus!"})

        username_deleted = user.username
        session.delete(user)
        session.commit()
        return {"status": "success", "message": f"Akun '{username_deleted}' berhasil dihapus!"}
    except Exception as e:
        session.rollback()
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    finally:
        session.close()

@app.get("/api/sync")
def sync_google_sheets():
    try:
        import_data()
        return {"status": "success", "message": "Berhasil melakukan sinkronisasi data dari Google Sheets!"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.post("/api/scan")
def record_scan(background_tasks: BackgroundTasks, id_sarana: str = Form(...), alur: str = Form(...)):
    session = SessionLocal()
    try:
        sarana = session.query(Sarana).filter_by(ID_Sarana=id_sarana).first()
        if not sarana:
            return JSONResponse(status_code=404, content={"status": "error", "message": f"Barcode '{id_sarana}' tidak terdaftar!"})

        current_status = sarana.Status_Saat_Ini if sarana.Status_Saat_Ini else "AT_API"
        is_recovered = False

        if current_status == "LOST_IN_TRANSIT":
            is_recovered = True
            new_status = STATUS_MAP.get(alur, "AT_API")
        else:
            if alur == "1":
                if current_status in ["IN_TRANSIT_OUT", "IN_TRANSIT_IN"]: return JSONResponse(status_code=400, content={"status": "error", "message": f"Unit '{id_sarana}' sudah di Truk!"})
                elif current_status == "AT_VENDOR": return JSONResponse(status_code=400, content={"status": "error", "message": f"Unit '{id_sarana}' masih di L2 Vendor!"})
                new_status = "IN_TRANSIT_OUT"
            elif alur == "2":
                if current_status == "AT_VENDOR": return JSONResponse(status_code=400, content={"status": "error", "message": f"Unit '{id_sarana}' sudah di L2 Vendor!"})
                elif current_status == "AT_API": return JSONResponse(status_code=400, content={"status": "error", "message": f"Unit '{id_sarana}' masih di PT API!"})
                new_status = "AT_VENDOR"
            elif alur == "3":
                if current_status in ["IN_TRANSIT_IN", "IN_TRANSIT_OUT"]: return JSONResponse(status_code=400, content={"status": "error", "message": f"Unit '{id_sarana}' sudah di Truk Balik!"})
                elif current_status == "AT_API": return JSONResponse(status_code=400, content={"status": "error", "message": f"Unit '{id_sarana}' ada di PT API!"})
                new_status = "IN_TRANSIT_IN"
            elif alur == "4":
                if current_status == "AT_API": return JSONResponse(status_code=400, content={"status": "error", "message": f"Unit '{id_sarana}' sudah di PT API!"})
                elif current_status == "AT_VENDOR": return JSONResponse(status_code=400, content={"status": "error", "message": f"Unit '{id_sarana}' masih di L2 Vendor!"})
                new_status = "AT_API"

        sarana.Status_Saat_Ini = new_status
        nama_alur_display = ALUR_DISPLAY_NAME.get(alur, new_status)
        if is_recovered:
            nama_alur_display = f"RECOVERY ({nama_alur_display})"

        log = LogScan(
            ID_Sarana=id_sarana, 
            Alur_Scan=nama_alur_display,
            Vendor=sarana.Vendor or "-",
            Waktu_Scan=datetime.datetime.now()
        )
        session.add(log)
        session.commit()

        background_tasks.add_task(send_to_google_sheets, id_sarana, new_status)
        msg = f"🎉 RECOVERY BERHASIL! Unit '{id_sarana}' ({sarana.Nama_Sarana}) berstatus {new_status}." if is_recovered else f"Scan Berhasil! Unit '{id_sarana}' ({sarana.Nama_Sarana}) masuk ke status {new_status}."
        return {"status": "success", "message": msg}
    except Exception as e:
        session.rollback()
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    finally:
        session.close()

@app.post("/api/finish-vendor-unloading")
def finish_vendor_unloading(background_tasks: BackgroundTasks):
    session = SessionLocal()
    try:
        in_transit_units = session.query(Sarana).filter_by(Status_Saat_Ini="IN_TRANSIT_OUT").all()
        if not in_transit_units:
            return {"status": "info", "message": "Tidak ada sarana yang sedang berada di dalam perjalanan Truk."}

        active_vendor_parts = {s.Nama_Sarana for s in session.query(Sarana).filter_by(Status_Saat_Ini="AT_VENDOR").all()}
        
        lost_units = []
        for unit in in_transit_units:
            if unit.Nama_Sarana in active_vendor_parts:
                unit.Status_Saat_Ini = "LOST_IN_TRANSIT"
                session.add(LogScan(
                    ID_Sarana=unit.ID_Sarana, 
                    Alur_Scan="⚠️ HILANG / SELISIH UNLOADING",
                    Vendor=unit.Vendor or "-",
                    Waktu_Scan=datetime.datetime.now()
                ))
                lost_units.append(unit.ID_Sarana)
                background_tasks.add_task(send_to_google_sheets, unit.ID_Sarana, "LOST_IN_TRANSIT")

        session.commit()
        
        if not lost_units:
            return {"status": "info", "message": "Unloading selesai! Semua muatan part yang dibongkar di Vendor telah diterima lengkap."}

        return {"status": "warning", "message": f"Unloading Selesai! Ditemukan {len(lost_units)} unit tertinggal/HILANG: {', '.join(lost_units)}"}
    except Exception as e:
        session.rollback()
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    finally:
        session.close()

@app.get("/api/dashboard")
def get_dashboard_data():
    session = SessionLocal()
    try:
        master_sarana = session.query(Sarana).all()
        grouped_data = {}
        for s in master_sarana:
            status_posisi = s.Status_Saat_Ini if s.Status_Saat_Ini else "AT_API"
            
            # ABAIKAN PART YANG SUDAH DIHAPUS (NG) DARI HITUNGAN STOK
            if status_posisi.startswith("DELETED"):
                continue
                
            nama_part = s.Nama_Sarana if s.Nama_Sarana else "Tanpa Nama Part"
            if nama_part not in grouped_data:
                grouped_data[nama_part] = {"Nama_Part": nama_part, "Total_QTY": 0, "PT_API": 0, "Truck": 0, "L2_Vendor": 0, "Lost": 0}
            
            grouped_data[nama_part]["Total_QTY"] += 1
            if status_posisi == "AT_API": grouped_data[nama_part]["PT_API"] += 1
            elif status_posisi in ["IN_TRANSIT_OUT", "IN_TRANSIT_IN"]: grouped_data[nama_part]["Truck"] += 1
            elif status_posisi == "AT_VENDOR": grouped_data[nama_part]["L2_Vendor"] += 1
            elif status_posisi == "LOST_IN_TRANSIT": grouped_data[nama_part]["Lost"] += 1
            
        return list(grouped_data.values())
    finally:
        session.close()

# Fungsi pembantu untuk update status delete ke Google Sheets
def send_delete_to_google_sheets(
    id_sarana: str,
    status_delete: str
):
    try:
        post_to_google_sheets({
            "action": "delete",
            "id_sarana": id_sarana,
            "status": status_delete
        })
    except Exception as error:
        print(f"[SYNC DELETE ERROR] {error}")

@app.post("/api/admin/sarana/delete-ng")
def delete_sarana_ng(
    background_tasks: BackgroundTasks,
    id_sarana: str = Form(...),
    alasan: str = Form(...)
):
    session = SessionLocal()
    try:
        id_clean = id_sarana.strip()
        alasan_clean = alasan.strip() or "Defect / NG"
        
        sarana = session.query(Sarana).filter_by(ID_Sarana=id_clean).first()
        if not sarana:
            return JSONResponse(status_code=404, content={"status": "error", "message": f"Part ID '{id_clean}' tidak ditemukan!"})
        
        status_lama = sarana.Status_Saat_Ini
        if status_lama.startswith("DELETED"):
            return JSONResponse(status_code=400, content={"status": "error", "message": f"Part ID '{id_clean}' sudah dihapus sebelumnya!"})

        # 1. Update Status Sarana di Master SQLite
        status_baru = f"DELETED_NG ({alasan_clean})"
        sarana.Status_Saat_Ini = status_baru
        
        # 2. Tambahkan Baris Audit Log di Menu Overview
        log = LogScan(
            ID_Sarana=id_clean,
            Alur_Scan=f"🗑️ DIHAPUS (NG) - {alasan_clean}",
            Vendor=sarana.Vendor or "-",
            Waktu_Scan=datetime.datetime.now()
        )
        session.add(log)
        session.commit()
        
        # 3. Kirim Perubahan ke Google Sheets di Background
        background_tasks.add_task(send_delete_to_google_sheets, id_clean, status_baru)
        
        return {
            "status": "success",
            "message": f"Part ID '{id_clean}' ({sarana.Nama_Sarana}) berhasil dihapus karena NG ({alasan_clean})!"
        }
    except Exception as e:
        session.rollback()
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    finally:
        session.close()        