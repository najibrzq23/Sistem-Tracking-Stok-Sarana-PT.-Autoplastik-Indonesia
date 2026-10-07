"""Impor master dan antrean salinan perubahan ke Google Sheets."""
import datetime
import hashlib
import json
import os
import re
from zoneinfo import ZoneInfo

import requests

from database import (
    SessionLocal,
    Sarana,
    LogScan,
    SyncJob,
    UnloadingGroup,
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


def _sync_revision(session):
    master = [
        (
            u.ID_Sarana,
            u.Nama_Sarana,
            u.No_Sarana,
            u.Vendor,
            u.Status_Saat_Ini,
        )
        for u in session.query(Sarana).order_by(Sarana.ID_Sarana)
    ]

    logs = [
        (
            l.id,
            l.ID_Sarana,
            l.Alur_Scan,
            l.Vendor,
            str(l.Waktu_Scan),
        )
        for l in session.query(LogScan).order_by(LogScan.id)
    ]

    jobs = [
        (j.id, j.state)
        for j in session.query(SyncJob).order_by(SyncJob.id)
    ]

    groups = [
        (g.id, g.state, g.snapshot)
        for g in session.query(UnloadingGroup)
        .order_by(UnloadingGroup.id)
    ]

    content = json.dumps(
        [master, logs, jobs, groups],
        ensure_ascii=False,
    )

    return hashlib.sha256(content.encode()).hexdigest()


def import_data():
    if not configured():
        raise ValueError("Isi APPS_SCRIPT_URL dan SYNC_API_KEY.")

    # Jangan menimpa transaksi yang belum terkirim ke Sheets.
    with SessionLocal() as session:
        lock_writes(session)

        pending = session.query(SyncJob).filter(
            SyncJob.state != "done"
        ).first()

        if pending:
            raise ValueError(
                "Masih ada antrean Sheets. Selesaikan/periksa "
                "antrean sebelum menyamakan data."
            )

        baseline = _sync_revision(session)
        session.rollback()

    try:
        response = requests.get(
            APPS_SCRIPT_URL,
            params={
                "sync_key": SYNC_API_KEY,
                "action": "snapshot",
            },
            timeout=(5, 30),
            allow_redirects=True,
        )

        response.raise_for_status()
        data = response.json()

    except (requests.RequestException, ValueError):
        raise ValueError(
            "Tidak dapat membaca snapshot Sheets. "
            "Data web tidak diubah."
        ) from None

    if (
        not isinstance(data, dict)
        or data.get("status") != "success"
        or data.get("schema") != "tracking_snapshot_v1"
    ):
        raise ValueError(
            "Snapshot tidak valid. Periksa versi deployment "
            "Apps Script. Data web tidak diubah."
        )

    master = data.get("master")
    logs = data.get("logs")

    if not isinstance(master, list) or not isinstance(logs, list):
        raise ValueError(
            "Snapshot master/log tidak valid. "
            "Data web tidak diubah."
        )

    parsed = []
    seen = set()

    valid_statuses = {
        "AT_API",
        "AT_VENDOR",
        "IN_TRANSIT_OUT",
        "IN_TRANSIT_IN",
        "LOST_IN_TRANSIT",
    }

    for row in master:
        if not isinstance(row, dict):
            raise ValueError("Baris master tidak valid.")

        id_sarana = str(row.get("ID_Sarana") or "").strip()
        name = str(row.get("Nama_Sarana") or "").strip()
        status = str(row.get("Status_Saat_Ini") or "").strip()

        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", id_sarana)
            or id_sarana in seen
            or not name
        ):
            raise ValueError(
                "Master memiliki ID/nama kosong, "
                "ID tidak valid, atau ID duplikat."
            )

        if (
            status not in valid_statuses
            and not re.fullmatch(r"DELETED_NG \(.+\)", status)
        ):
            raise ValueError("Status master tidak valid.")

        seen.add(id_sarana)

        parsed.append({
            "ID_Sarana": id_sarana,
            "Nama_Sarana": name,
            "No_Sarana": str(row.get("No_Sarana") or ""),
            "Vendor": str(row.get("Vendor") or "-"),
            "Status_Saat_Ini": status,
        })

    parsed_logs = []

    for row in logs:
        if not isinstance(row, dict):
            raise ValueError("Baris log tidak valid.")

        id_sarana = str(row.get("ID_Sarana") or "").strip()
        event = str(row.get("Alur_Scan") or "").strip()

        try:
            timestamp = datetime.datetime.fromisoformat(
                str(row.get("Waktu_Scan") or "").strip()
            )

            if timestamp.tzinfo:
                timestamp = timestamp.astimezone(
                    ZoneInfo("Asia/Jakarta")
                ).replace(tzinfo=None)

        except ValueError:
            raise ValueError(
                "Waktu_Scan harus berupa tanggal valid, "
                "contoh 2026-10-07 08:00:00."
            ) from None

        if not id_sarana or not event:
            raise ValueError("ID atau Alur_Scan log kosong.")

        parsed_logs.append({
            "ID_Sarana": id_sarana,
            "Alur_Scan": event,
            "Vendor": str(row.get("Vendor") or "-"),
            "Waktu_Scan": timestamp,
        })

    # Semua validasi selesai sebelum mengubah database.
    with SessionLocal() as session:
        lock_writes(session)

        pending = session.query(SyncJob).filter(
            SyncJob.state != "done"
        ).first()

        if pending or _sync_revision(session) != baseline:
            raise ValueError(
                "Data web berubah ketika membaca Sheets. "
                "Hentikan scan sebentar lalu ulangi Sync."
            )

        old_master = session.query(Sarana).count()
        old_logs = session.query(LogScan).count()

        session.query(UnloadingGroup).delete(
            synchronize_session=False
        )
        session.query(LogScan).delete(
            synchronize_session=False
        )
        session.query(Sarana).delete(
            synchronize_session=False
        )

        session.add_all([
            Sarana(**row) for row in parsed
        ])
        session.add_all([
            LogScan(**row) for row in parsed_logs
        ])

        session.commit()

    return (
        f"Sheets diterapkan: master {old_master} → {len(parsed)}, "
        f"log {old_logs} → {len(parsed_logs)}. "
        "Sesi unloading direset; akun dan vendor tetap tersimpan."
    )