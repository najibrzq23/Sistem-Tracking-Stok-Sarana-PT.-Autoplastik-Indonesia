import requests
from database import SessionLocal, Sarana

APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbwkYxvFb2L7HwlChAk_WpEAc6XqE5ueGM_h6_dp0pn0MjHiP6eaTGgwbskhm_uIcPwSXQ/exec"

def import_data():
    if "PASTE_URL" in APPS_SCRIPT_URL:
        print("[SYNC SKIPPED] URL Apps Script belum dipasang di sync_sheets.py.")
        return

    try:
        response = requests.get(APPS_SCRIPT_URL, timeout=15, allow_redirects=True)
        if response.status_code != 200:
            raise Exception(f"HTTP Status {response.status_code}")

        data = response.json()
        if isinstance(data, dict):
            message = str(
                data.get("message") or "Tidak ada detail error."
            )
            raise Exception(f"Apps Script: {message[:500]}")

        if not isinstance(data, list):
            raise Exception(
                "Respons Apps Script bukan daftar master. "
                "Periksa URL deployment /exec."
            )

        session = SessionLocal()
        try:
            existing_saranas = {s.ID_Sarana: s for s in session.query(Sarana).all()}
            
            for row in data:
                id_sarana = str(row.get("ID_Sarana", "")).strip()
                if not id_sarana:
                    continue

                nama_sarana = str(row.get("Nama_Sarana", ""))
                no_sarana = str(row.get("No_Sarana", ""))
                vendor = str(row.get("Vendor", "-")).strip() or "-"
                status = str(row.get("Status_Saat_Ini", "AT_API")).strip()

                if id_sarana in existing_saranas:
                    s = existing_saranas[id_sarana]
                    s.Nama_Sarana = nama_sarana
                    s.No_Sarana = no_sarana
                    s.Vendor = vendor
                    if status:
                        s.Status_Saat_Ini = status
                else:
                    new_s = Sarana(
                        ID_Sarana=id_sarana,
                        Nama_Sarana=nama_sarana,
                        No_Sarana=no_sarana,
                        Vendor=vendor,
                        Status_Saat_Ini=status if status else "AT_API"
                    )
                    session.add(new_s)

            session.commit()
            print("[SYNC SUCCESS] Data dari Google Sheets berhasil diperbarui di SQLite!")
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    except Exception as e:
        print(f"[SYNC ERROR] {e}")