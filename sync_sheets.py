"""Impor master dan antrean salinan perubahan ke Google Sheets."""
import datetime
import hashlib
import json
import os
import re
import uuid
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import text

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
    if LOCAL_TRIAL:
        return

    if not configured():
        raise RuntimeError(
            'Integrasi Sheets belum dikonfigurasi.'
        )

    payload = dict(payload)
    payload['schema'] = 'tracking_v3'
    payload['request_id'] = uuid.uuid4().hex

    session.add(SyncJob(
        state='pending',
        payload=json.dumps(
            payload,
            ensure_ascii=False,
        ),
    ))


def flush_one():
    if not configured():
        return {
            'status': 'not_configured',
            'message': 'Integrasi Sheets belum dikonfigurasi.',
        }

    with SessionLocal() as session:
        lock_writes(session)

        job = (
            session.query(SyncJob)
            .filter(SyncJob.state != 'done')
            .order_by(SyncJob.id)
            .first()
        )

        if not job:
            return {
                'status': 'idle',
                'message': 'Tidak ada antrean.',
            }

        if job.state != 'pending':
            return {
                'status': 'blocked',
                'job_id': job.id,
                'job_state': job.state,
                'message': job.error,
            }

        payload = json.loads(job.payload)

        if (
            payload.get('schema') != 'tracking_v3'
            or not payload.get('request_id')
        ):
            job.state = 'review'
            job.error = (
                'Antrean format lama. Periksa stok dan log '
                'di Sheets sebelum menyelesaikan atau mengirim ulang.'
            )

            session.commit()

            return {
                'status': 'review',
                'job_id': job.id,
                'message': job.error,
            }

        job.state = 'sending'
        job.sent_at = now()
        job_id = job.id

        session.commit()

    state = 'review'
    message = ''
    result = {}

    try:
        response = requests.post(
            APPS_SCRIPT_URL,
            json={
                **payload,
                '_sync_key': SYNC_API_KEY,
            },
            timeout=(5, 30),
            allow_redirects=True,
        )

        response.raise_for_status()

        try:
            result = response.json()
        except ValueError:
            raise ValueError(
                'Apps Script tidak mengembalikan JSON. '
                'Periksa URL /exec dan akses Web app.'
            ) from None

        if not isinstance(result, dict):
            raise ValueError(
                'Format respons Apps Script tidak valid.'
            )

        if result.get('status') != 'success':
            raise ValueError(
                safe_message(result.get('message'))
            )

        if (
            result.get('schema') != 'tracking_ack_v3'
            or result.get('request_id') != payload['request_id']
            or result.get('confirmed') is not True
        ):
            raise ValueError(
                'Respons sukses belum mengonfirmasi transaksi v3. '
                'Periksa deployment Apps Script.'
            )

        state = 'done'

    except requests.RequestException as error:
        message = (
            'Koneksi Sheets gagal atau timeout ('
            + type(error).__name__
            + '). Periksa master dan log sebelum mengulang.'
        )

    except ValueError as error:
        message = safe_message(error)

    except Exception:
        message = (
            'Pengiriman belum terkonfirmasi. '
            'Periksa master dan log Sheets.'
        )

    with SessionLocal() as session:
        lock_writes(session)

        job = session.get(SyncJob, job_id)

        if job and job.state == 'sending':
            job.state = state
            job.error = message

        session.commit()

    return {
        'status': (
            'success'
            if state == 'done'
            else 'review'
        ),
        'job_id': job_id,
        'job_state': state,
        'message': (
            message
            or result.get(
                'message',
                'Sheets terkonfirmasi.',
            )
        ),
    }

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


LOG_ID_KEYS = ('Log_ID', 'LogId', 'Log ID', 'log_id')


def _sheet_log_id(row):
    for key in LOG_ID_KEYS:
        if key in row:
            return row.get(key)
    return None


def _parse_log_id(value):
    if value is None or value == '':
        return None

    text_value = str(value).strip()

    if not re.fullmatch(r'\d+(?:\.0+)?', text_value):
        return None

    number = int(text_value.split('.', 1)[0])
    return number if number > 0 else None


def _assign_log_ids(rows, raw_ids):
    if raw_ids and all(value is not None for value in raw_ids):
        if len(set(raw_ids)) != len(raw_ids):
            raise ValueError(
                'Log_ID duplikat pada snapshot Sheets.'
            )

        for row, log_id in zip(rows, raw_ids):
            row['id'] = log_id

        return rows

    # Snapshot lama mungkin belum mengirim Log_ID. Nomor tetap
    # dimulai dari 1 agar cocok dengan urutan baris spreadsheet.
    for index, row in enumerate(rows, start=1):
        row['id'] = index

    return rows


def _restart_log_sequence(session):
    dialect = session.get_bind().dialect.name

    if dialect == 'postgresql':
        max_id = session.execute(text(
            'SELECT COALESCE(MAX(id), 1) FROM log_scan'
        )).scalar()
        has_rows = session.execute(text(
            'SELECT COUNT(*) FROM log_scan'
        )).scalar() > 0
        session.execute(
            text(
                "SELECT setval("
                "pg_get_serial_sequence('log_scan', 'id'), "
                ":value, :called)"
            ),
            {'value': max_id, 'called': has_rows},
        )
    elif dialect == 'sqlite':
        has_sequence = session.execute(text(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='sqlite_sequence'"
        )).scalar()

        if has_sequence:
            session.execute(text(
                "UPDATE sqlite_sequence "
                "SET seq = COALESCE((SELECT MAX(id) FROM log_scan), 0) "
                "WHERE name='log_scan'"
            ))


def import_data():
    if not configured():
        raise ValueError(
            'Integrasi Sheets belum dikonfigurasi.'
        )

    with SessionLocal() as session:
        lock_writes(session)

        if session.query(SyncJob).filter(
            SyncJob.state != 'done'
        ).first():
            raise ValueError(
                'Selesaikan/periksa antrean Sheets sebelum Sync.'
            )

        baseline = _sync_revision(session)
        session.rollback()

    response = requests.get(
        APPS_SCRIPT_URL,
        params={
            'action': 'snapshot',
            'sync_key': SYNC_API_KEY,
        },
        timeout=(5, 30),
        allow_redirects=True,
    )

    response.raise_for_status()
    data = response.json()

    if (
        not isinstance(data, dict)
        or data.get('status') != 'success'
        or data.get('schema') != 'tracking_snapshot_v3'
    ):
        detail = (
            data.get('message', 'Snapshot bukan v3.')
            if isinstance(data, dict)
            else 'Snapshot tidak valid.'
        )
        raise ValueError(safe_message(detail))

    master = data.get('master')
    logs = data.get('logs')

    if (
        not isinstance(master, list)
        or not isinstance(logs, list)
    ):
        raise ValueError(
            'Master/log bukan daftar.'
        )

    def date(value):
        if not value:
            return None

        try:
            value = datetime.datetime.fromisoformat(
                str(value)
            )

            if value.tzinfo:
                value = value.astimezone(
                    ZoneInfo('Asia/Jakarta')
                ).replace(tzinfo=None)

            if value > now() + datetime.timedelta(minutes=5):
                raise ValueError()

            return value

        except (ValueError, TypeError):
            raise ValueError(
                'Tanggal Sheets tidak valid.'
            ) from None

    parsed = []
    parsed_logs = []
    raw_log_ids = []
    seen = set()
    events = set()

    statuses = {
        'AT_API',
        'AT_VENDOR',
        'AT_CUSTOMER',
        'IN_TRANSIT_OUT',
        'IN_TRANSIT_IN',
        'LOST_IN_TRANSIT',
    }

    for row in master:
        if not isinstance(row, dict):
            raise ValueError(
                'Baris master tidak valid.'
            )

        id_sarana = str(
            row.get('ID_Sarana') or ''
        ).strip()

        name = str(
            row.get('Nama_Sarana') or ''
        ).strip()

        status = str(
            row.get('Status_Saat_Ini') or ''
        ).strip()

        partner = ' '.join(
            str(row.get('Vendor_Customer') or '')
            .upper().split()
        )

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
                'ID/nama kosong, tidak valid atau duplikat: '
                + id_sarana
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

        if (
            len(partner) > 100
            or (status == 'AT_API' and partner)
            or (
                status in statuses - {'AT_API'}
                and not partner
            )
        ):
            raise ValueError(
                'Status dan tujuan tidak sesuai: '
                + id_sarana
            )

        seen.add(id_sarana)

        parsed.append({
            'ID_Sarana': id_sarana,
            'Nama_Sarana': name,
            'No_Sarana': str(
                row.get('No_Sarana') or ''
            ),
            'Vendor': (
                partner
                if status != 'AT_CUSTOMER'
                else ''
            ),
            'Customer': (
                partner
                if status == 'AT_CUSTOMER'
                else ''
            ),
            'Status_Saat_Ini': status,
            'Last_Scan_At': date(
                row.get('Scan_Terakhir')
            ),
            'API_Received_At': None,
        })

    for row in logs:
        if not isinstance(row, dict):
            raise ValueError(
                'Baris log tidak valid.'
            )

        id_sarana = str(
            row.get('ID_Sarana') or ''
        )

        event = str(
            row.get('Alur_Scan') or ''
        )

        event_id = str(
            row.get('Event_ID') or ''
        )

        scan_time = date(
            row.get('Waktu_Scan')
        )

        if (
            not re.fullmatch(
                r'[A-Za-z0-9_.-]{1,100}',
                id_sarana,
            )
            or not event
            or not scan_time
            or not event_id
            or event_id in events
        ):
            raise ValueError(
                'ID, alur, waktu atau Event_ID '
                'log tidak valid/duplikat.'
            )

        events.add(event_id)
        raw_log_ids.append(_parse_log_id(_sheet_log_id(row)))

        parsed_logs.append({
            'ID_Sarana': id_sarana,
            'Alur_Scan': event,
            'Vendor': str(
                row.get('Vendor_Customer') or '-'
            ),
            'Customer': '',
            'Waktu_Scan': scan_time,
        })

    _assign_log_ids(parsed_logs, raw_log_ids)

    with SessionLocal() as session:
        lock_writes(session)

        if (
            session.query(SyncJob)
            .filter(SyncJob.state != 'done')
            .first()
            or _sync_revision(session) != baseline
        ):
            raise ValueError(
                'Data berubah ketika membaca Sheets. '
                'Hentikan scan sebentar dan ulangi.'
            )

        for row in parsed:
            if (
                row['Status_Saat_Ini'] == 'AT_CUSTOMER'
                and not session.get(
                    CustomerRecord,
                    row['Customer'],
                )
            ):
                raise ValueError(
                    'Tambahkan customer dahulu: '
                    + row['Customer']
                )

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

        session.flush()
        _restart_log_sequence(session)
        session.commit()

    return (
        f'Sync selesai: {len(parsed)} sarana '
        f'dan {len(parsed_logs)} log. '
        'Akun dan daftar mitra tetap tersimpan.'
    )