"""Impor master dan antrean salinan ke Sheets; PostgreSQL menyimpan transaksi utama."""
import json
import os
import re
import requests
from database import SessionLocal, Sarana, SyncJob, lock_writes, now

APPS_SCRIPT_URL = os.getenv('APPS_SCRIPT_URL', '').strip()
SYNC_API_KEY = os.getenv('SYNC_API_KEY', '').strip()

def configured():
    return bool(APPS_SCRIPT_URL and SYNC_API_KEY)

def enqueue(session, payload):
    if configured():
        session.add(SyncJob(payload=json.dumps(payload, ensure_ascii=False)))

def flush_one():
    if not configured():
        return
    with SessionLocal() as session:
        lock_writes(session)
        job = session.query(SyncJob).filter(SyncJob.state != 'done').order_by(SyncJob.id).first()
        # Error harus ditinjau: jangan mengulang request berpotensi sudah tersimpan.
        if not job or job.state != 'pending':
            session.rollback()
            return
        # Simpan 'sending' SEBELUM jaringan; jika function berhenti, UI menunjukkan
        # status tidak pasti dan operator wajib meninjau, bukan mengirim ulang buta.
        job.state = 'sending'
        job.sent_at = now()
        payload = json.loads(job.payload)
        job_id = job.id
        session.commit()
    try:
        payload['_sync_key'] = SYNC_API_KEY
        response = requests.post(APPS_SCRIPT_URL, json=payload, timeout=(5, 20), allow_redirects=True)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get('status') != 'success':
            raise ValueError('Apps Script menolak data. Periksa deployment, key, ID duplikat, dan LOG_SCAN.')
        state, message = 'done', ''
    except Exception:
        # Jangan menyimpan URL atau rahasia dari exception jaringan.
        state, message = 'review', 'Pengiriman belum terkonfirmasi. Periksa DATABASE dan LOG_SCAN sebelum tindakan lanjutan.'
    with SessionLocal() as session:
        lock_writes(session)
        job = session.get(SyncJob, job_id)
        if job and job.state == 'sending':
            job.state, job.error = state, message
        session.commit()

def import_data():
    if not configured():
        raise ValueError('Isi APPS_SCRIPT_URL dan SYNC_API_KEY pada Environment Variables.')
    response = requests.get(APPS_SCRIPT_URL, params={'sync_key': SYNC_API_KEY}, timeout=(5, 20), allow_redirects=True)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError('Apps Script harus membalas daftar master. Periksa key, deployment, dan tab DATABASE.')
    parsed, ids = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Format baris spreadsheet tidak valid.')
        id_ = str(row.get('ID_Sarana', '')).strip()
        if not id_:
            continue
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', id_):
            raise ValueError('ID harus berupa huruf/angka, titik, underscore, atau tanda minus; maksimal 100 karakter.')
        if id_ in ids:
            raise ValueError('Spreadsheet memiliki ID duplikat. Perbaiki master sebelum impor.')
        ids.add(id_)
        name = str(row.get('Nama_Sarana') or '').strip()
        status = str(row.get('Status_Saat_Ini') or 'AT_API').strip()
        if not name or (status not in ['AT_API','AT_VENDOR','IN_TRANSIT_OUT','IN_TRANSIT_IN','LOST_IN_TRANSIT'] and not status.startswith('DELETED_NG')):
            raise ValueError('Nama atau status pada spreadsheet tidak valid.')
        parsed.append(dict(ID_Sarana=id_, Nama_Sarana=name, No_Sarana=str(row.get('No_Sarana') or ''),
                           Vendor=str(row.get('Vendor') or '-'), Status_Saat_Ini=status))
    added, updated = 0, 0
    with SessionLocal() as session:
        lock_writes(session)
        existing = {s.ID_Sarana:s for s in session.query(Sarana).all()}
        for row in parsed:
            unit = existing.get(row['ID_Sarana'])
            if unit:
                # Tidak menimpa posisi/NG yang sudah tercatat di database online.
                unit.Nama_Sarana, unit.No_Sarana, unit.Vendor = row['Nama_Sarana'], row['No_Sarana'], row['Vendor']
                updated += 1
            else:
                session.add(Sarana(**row)); added += 1
        session.commit()
    return f'Impor selesai: {added} unit baru, {updated} metadata diperbarui. Status unit lama dipertahankan.'
