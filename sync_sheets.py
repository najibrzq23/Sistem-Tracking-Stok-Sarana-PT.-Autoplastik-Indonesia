"""Impor master dan antrean salinan perubahan ke Google Sheets."""

import json
import os
import re

import requests

from database import (
    SessionLocal,
    Sarana,
    SyncJob,
    lock_writes,
    now,
)


APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL", "").strip()
SYNC_API_KEY = os.getenv("SYNC_API_KEY", "").strip()


def configured():
    return bool(APPS_SCRIPT_URL and SYNC_API_KEY)


def safe_message(value):
    """Hilangkan key dari pesan sebelum ditampilkan."""
    message = str(value or "Tidak ada detail error.")

    if SYNC_API_KEY:
        message = message.replace(
            SYNC_API_KEY,
            "[disembunyikan]",
        )

    return message[:500]


def enqueue(session, payload):
    """Simpan antrean dalam transaksi stok yang sama."""
    if configured():
        session.add(
            SyncJob(
                payload=json.dumps(
                    payload,
                    ensure_ascii=False,
                )
            )
        )


def flush_one():
    """Kirim satu antrean, sesuai urutan."""
    if not configured():
        return

    with SessionLocal() as session:
        lock_writes(session)

        job = (
            session.query(SyncJob)
            .filter(SyncJob.state != "done")
            .order_by(SyncJob.id)
            .first()
        )

        # Pengiriman tidak pasti harus ditinjau dahulu.
        if not job or job.state != "pending":
            session.rollback()
            return

        job.state = "sending"
        job.sent_at = now()

        payload = json.loads(job.payload)
        job_id = job.id

        session.commit()

    try:
        payload["_sync_key"] = SYNC_API_KEY

        response = requests.post(
            APPS_SCRIPT_URL,
            json=payload,
            timeout=(5, 20),
            allow_redirects=True,
        )

        response.raise_for_status()
        result = response.json()

        if (
            not isinstance(result, dict)
            or result.get("status") != "success"
        ):
            raise ValueError(
                "Apps Script belum mengonfirmasi pengiriman."
            )

        state = "done"
        message = ""

    except Exception:
        state = "review"
        message = (
            "Pengiriman belum terkonfirmasi. "
            "Periksa DATABASE dan LOG_SCAN sebelum "
            "mengirim ulang atau melanjutkan antrean."
        )

    with SessionLocal() as session:
        lock_writes(session)

        job = session.get(SyncJob, job_id)

        if job and job.state == "sending":
            job.state = state
            job.error = message

        session.commit()


def import_data():
    """Baca master Sheets dan impor ke database aplikasi."""
    if not configured():
        raise ValueError(
            "Isi APPS_SCRIPT_URL dan SYNC_API_KEY "
            "pada Environment Variables Vercel."
        )

    if len(SYNC_API_KEY) < 32:
        raise ValueError(
            "SYNC_API_KEY di Vercel minimal 32 karakter "
            "dan harus sama dengan Script Properties."
        )

    try:
        response = requests.get(
            APPS_SCRIPT_URL,
            params={"sync_key": SYNC_API_KEY},
            timeout=(5, 20),
            allow_redirects=True,
        )

        response.raise_for_status()

    except requests.RequestException:
        raise ValueError(
            "Tidak bisa mengakses Apps Script. "
            "Periksa URL /exec dan akses Web app."
        ) from None

    try:
        rows = response.json()

    except ValueError:
        raise ValueError(
            "Apps Script tidak membalas JSON. "
            "Periksa URL /exec, akses Web app, "
            "dan versi deployment."
        ) from None

    # Tampilkan pesan asli jika Apps Script membalas error.
    if isinstance(rows, dict):
        message = safe_message(rows.get("message"))

        raise ValueError(
            f"Apps Script: {message}"
        )

    if not isinstance(rows, list):
        raise ValueError(
            "Respons Apps Script bukan daftar master. "
            "Periksa URL deployment /exec."
        )

    parsed = []
    ids = set()

    valid_statuses = {
        "AT_API",
        "AT_VENDOR",
        "IN_TRANSIT_OUT",
        "IN_TRANSIT_IN",
        "LOST_IN_TRANSIT",
    }

    # Validasi seluruh data sebelum mengubah database.
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(
                "Format baris spreadsheet tidak valid."
            )

        id_sarana = str(
            row.get("ID_Sarana") or ""
        ).strip()

        if not id_sarana:
            continue

        if not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}",
            id_sarana,
        ):
            raise ValueError(
                "ID harus berupa huruf, angka, titik, "
                "underscore, atau tanda minus; "
                "maksimal 100 karakter."
            )

        if id_sarana in ids:
            raise ValueError(
                "Spreadsheet memiliki ID duplikat: "
                f"{id_sarana}. Perbaiki sebelum impor."
            )

        ids.add(id_sarana)

        name = str(
            row.get("Nama_Sarana") or ""
        ).strip()

        status = str(
            row.get("Status_Saat_Ini") or "AT_API"
        ).strip()

        if not name:
            raise ValueError(
                f"Nama sarana kosong pada ID {id_sarana}."
            )

        if (
            status not in valid_statuses
            and not status.startswith("DELETED_NG")
        ):
            raise ValueError(
                f"Status tidak valid pada ID {id_sarana}."
            )

        parsed.append(
            {
                "ID_Sarana": id_sarana,
                "Nama_Sarana": name,
                "No_Sarana": str(
                    row.get("No_Sarana") or ""
                ),
                "Vendor": str(
                    row.get("Vendor") or "-"
                ),
                "Status_Saat_Ini": status,
            }
        )

    added = 0
    updated = 0

    with SessionLocal() as session:
        lock_writes(session)

        existing = {
            unit.ID_Sarana: unit
            for unit in session.query(Sarana).all()
        }

        for row in parsed:
            unit = existing.get(row["ID_Sarana"])

            if unit:
                unit.Nama_Sarana = row["Nama_Sarana"]
                unit.No_Sarana = row["No_Sarana"]
                unit.Vendor = row["Vendor"]

                # Pertahankan status operasional/NG
                # yang sudah ada di database online.
                updated += 1

            else:
                session.add(Sarana(**row))
                added += 1

        session.commit()

    return (
        f"Impor selesai: {added} unit baru, "
        f"{updated} metadata diperbarui. "
        "Status unit lama dipertahankan."
    )