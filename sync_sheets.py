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
    now, CustomerRecord,
    LOCAL_TRIAL,)


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
    if LOCAL_TRIAL:
        return
    if not configured():
        raise RuntimeError(
            "Integrasi Sheets belum dikonfigurasi. "
            "Periksa APPS_SCRIPT_URL dan SYNC_API_KEY."
        )

    session.add(
        SyncJob(
            state="pending",
            payload=json.dumps(
                payload,
                ensure_ascii=False,
            ),
        )
    )


def flush_one():
    """Kirim satu antrean, sesuai urutan."""
    if not configured():
        return {
            "status": "not_configured",
            "message": "Integrasi Sheets belum dikonfigurasi."}

    with SessionLocal() as session:
        lock_writes(session)

        job = (
            session.query(SyncJob)
            .filter(SyncJob.state != "done")
            .order_by(SyncJob.id)
            .first()
        )

        # Pengiriman tidak pasti harus ditinjau dahulu.
        if not job:
            session.rollback()
            return {
                "status": "idle",
                "message": "Tidak ada antrean yang belum selesai."
            }
        if job.state != "pending":
            result = {
                "status": "blocked",
                "job_id": job.id,
                "job_state": job.state,
                "message": job.error or (
                    "Antrean paling awal perlu diperiksa sebelum dilanjutkan."
                )
            }
            session.rollback()
            return result
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
    output = {
        "status": "success" if state == "done" else "review",
        "job_id": job_id,
        "job_state": state,
        "message": message
    }
    if state == "done":
        output["sheets_result"] = {
            "message": result.get("message"),
            "id_sarana": result.get("id_sarana"),
            "status_sesudah": result.get("status_sesudah"),
            "changed": result.get("changed"),
        }

    return output

def _sync_revision(session):
    master = [
        (
            u.ID_Sarana,
            u.Nama_Sarana,
            u.No_Sarana,
            u.Vendor,
            u.Status_Saat_Ini,
            u.Customer,
            str(u.Last_Scan_At),
            str(u.API_Received_At),
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
            l.Customer,
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
        raise ValueError(
            'Isi APPS_SCRIPT_URL dan SYNC_API_KEY.'
        )

    with SessionLocal() as session:
        lock_writes(session)

        pending = session.query(SyncJob).filter(
            SyncJob.state != 'done'
        ).first()

        if pending:
            raise ValueError(
                'Masih ada antrean Sheets. '
                'Selesaikan/periksa antrean dahulu.'
            )

        baseline = _sync_revision(session)
        session.rollback()

    try:
        response = requests.get(
            APPS_SCRIPT_URL,
            params={
                'sync_key': SYNC_API_KEY,
                'action': 'snapshot',
            },
            timeout=(5, 30),
            allow_redirects=True,
        )

        response.raise_for_status()
        data = response.json()

    except (requests.RequestException, ValueError):
        raise ValueError(
            'Snapshot Sheets tidak terbaca. '
            'Data web tidak diubah.'
        ) from None

    if (
        not isinstance(data, dict)
        or data.get('status') != 'success'
        or data.get('schema') != 'tracking_snapshot_v2'
    ):
        raise ValueError(
            'Snapshot belum versi v2. '
            'Periksa deployment Apps Script.'
        )

    master = data.get('master')
    logs = data.get('logs')

    if not isinstance(master, list) or not isinstance(logs, list):
        raise ValueError(
            'Master/log snapshot tidak valid.'
        )

    def timestamp(value):
        if value is None or str(value).strip() == '':
            return None

        try:
            result = datetime.datetime.fromisoformat(
                str(value).strip()
            )

            if result.tzinfo:
                result = result.astimezone(
                    ZoneInfo('Asia/Jakarta')
                ).replace(tzinfo=None)

            if result > now() + datetime.timedelta(minutes=5):
                raise ValueError()

            return result

        except (ValueError, TypeError):
            raise ValueError(
                'Tanggal tidak valid atau berada '
                'di masa depan: ' + str(value)
            ) from None

    parsed = []
    parsed_logs = []
    seen = set()

    fields = (
        'ID_Sarana',
        'Nama_Sarana',
        'No_Sarana',
        'Vendor',
        'Status_Saat_Ini',
        'Customer',
        'Last_Scan_At',
        'API_Received_At',
    )

    statuses = {
        'AT_API',
        'AT_VENDOR',
        'AT_CUSTOMER',
        'IN_TRANSIT_OUT',
        'IN_TRANSIT_IN',
        'LOST_IN_TRANSIT',
    }

    for row in master:
        if (
            not isinstance(row, dict)
            or not set(fields).issubset(row)
        ):
            raise ValueError(
                'Kolom master snapshot belum lengkap.'
            )

        if all(
            not str(row.get(field) or '').strip()
            for field in fields
        ):
            continue

        id_sarana = str(
            row.get('ID_Sarana') or ''
        ).strip()

        name = str(
            row.get('Nama_Sarana') or ''
        ).strip()

        status = str(
            row.get('Status_Saat_Ini') or ''
        ).strip()

        if (
            not re.fullmatch(
                r'[A-Za-z0-9_.-]{1,100}',
                id_sarana,
            )
            or not name
            or len(name) > 150
            or id_sarana in seen
        ):
            raise ValueError(
                'Master memiliki ID/nama tidak valid '
                'atau ID duplikat: ' + id_sarana
            )

        if (
            status not in statuses
            and not re.fullmatch(
                r'DELETED_NG \(.+\)',
                status,
            )
        ):
            raise ValueError(
                'Status tidak valid: ' + id_sarana
            )

        customer = ' '.join(
            str(row.get('Customer') or '')
            .strip().upper().split()
        )

        if (
            len(customer) > 100
            or (
                status == 'AT_CUSTOMER'
                and not customer
            )
        ):
            raise ValueError(
                'Customer tujuan tidak valid: '
                + id_sarana
            )

        if (
            customer
            and status != 'AT_CUSTOMER'
            and not status.startswith('DELETED_NG')
        ):
            raise ValueError(
                'Status dan customer tidak konsisten: '
                + id_sarana
            )

        seen.add(id_sarana)

        parsed.append({
            'ID_Sarana': id_sarana,
            'Nama_Sarana': name,
            'No_Sarana': str(row.get('No_Sarana') or ''),
            'Vendor': str(row.get('Vendor') or '-'),
            'Status_Saat_Ini': status,
            'Customer': customer,
            'Last_Scan_At': timestamp(
                row.get('Last_Scan_At')
            ),
            'API_Received_At': timestamp(
                row.get('API_Received_At')
            ),
        })

    for row in logs:
        if not isinstance(row, dict):
            raise ValueError(
                'Baris log tidak valid.'
            )

        id_sarana = str(
            row.get('ID_Sarana') or ''
        ).strip()

        event = str(
            row.get('Alur_Scan') or ''
        ).strip()

        scan_time = timestamp(
            row.get('Waktu_Scan')
        )

        if (
            not re.fullmatch(
                r'[A-Za-z0-9_.-]{1,100}',
                id_sarana,
            )
            or not event
            or not scan_time
        ):
            raise ValueError(
                'ID, alur atau waktu log tidak valid.'
            )

        parsed_logs.append({
            'ID_Sarana': id_sarana,
            'Alur_Scan': event,
            'Vendor': str(row.get('Vendor') or '-'),
            'Customer': str(row.get('Customer') or ''),
            'Waktu_Scan': scan_time,
        })

    with SessionLocal() as session:
        lock_writes(session)

        pending = session.query(SyncJob).filter(
            SyncJob.state != 'done'
        ).first()

        if pending or _sync_revision(session) != baseline:
            raise ValueError(
                'Data web berubah saat membaca Sheets. '
                'Hentikan scan sebentar dan ulangi Sync.'
            )

        for row in parsed:
            if (
                row['Customer']
                and not session.get(
                    CustomerRecord,
                    row['Customer'],
                )
            ):
                raise ValueError(
                    'Daftarkan customer di List Customer dahulu: '
                    + row['Customer']
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
            Sarana(**row)
            for row in parsed
        ])

        session.add_all([
            LogScan(**row)
            for row in parsed_logs
        ])

        session.commit()

    return (
        f'Sheets diterapkan: master {old_master} → {len(parsed)}, '
        f'log {old_logs} → {len(parsed_logs)}. '
        'Sesi unloading direset; akun, vendor '
        'dan daftar customer tetap tersimpan.'
    )