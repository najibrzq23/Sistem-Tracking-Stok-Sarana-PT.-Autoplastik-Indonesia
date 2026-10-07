import datetime
import hashlib
import html
import json
import os
import re
import secrets
import time
import logging
import hmac
from database import VendorRecord, DeletedVendor
from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool
from sqlalchemy import text, func
from database import (BASE_DIR, IS_CLOUD, IS_POSTGRES, PRIMARY_ADMIN, SessionLocal,
                      Sarana, LogScan, User, SyncJob, LoginAttempt, AccountAudit, UnloadingGroup, init_db,
                      lock_writes, hash_password, verify_password, now)
from sync_sheets import import_data, enqueue, flush_one, configured

secret = os.getenv('SESSION_SECRET', '')
if len(secret) < 32:
    if IS_CLOUD:
        raise RuntimeError('SESSION_SECRET minimal 32 karakter wajib diisi.')
    secret = secrets.token_urlsafe(48)

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title='PT API Tracking Online', lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount('/static', StaticFiles(directory=str(BASE_DIR / 'static')), name='static')

def load_session_user(user_id):
    with SessionLocal() as session:
        return session.get(User, user_id) if user_id else None

@app.middleware('http')
async def access_control(request: Request, call_next):
    started = time.perf_counter()
    path = request.url.path
    if path.startswith('/api/'):
        if request.method not in ('GET','HEAD','OPTIONS') and request.headers.get('X-Requested-With') != 'XMLHttpRequest':
            return JSONResponse({'message':'Permintaan tidak valid.'}, status_code=403)
        if path not in ['/api/login','/api/health']:
            user = await run_in_threadpool(load_session_user, request.session.get('user_id'))
            if not user or request.session.get('credential_tag') != hashlib.sha256(user.password.encode()).hexdigest():
                request.session.clear()
                return JSONResponse({'message':'Silakan login kembali.'}, status_code=401)
            request.state.actor = user.username
            request.state.role = user.role
            request.state.user = public_user(user)
            admin_paths = ['/api/admin/users','/api/admin/sarana/','/api/sync']
            if any(path.startswith(p) for p in admin_paths) and user.role != 'admin':
                return JSONResponse({'message':'Akses khusus admin.'}, status_code=403)
            if path in ['/api/scan','/api/finish-vendor-unloading','/api/unloading/preview','/api/sheets/drain'] and user.role not in ['admin','member']:
                return JSONResponse({'message':'Akses khusus operator atau admin.'}, status_code=403)
    response = await call_next(request)
    if path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    duration_ms = (time.perf_counter() - started) * 1000
    response.headers['Server-Timing'] = f'app;dur={duration_ms:.1f}'
    if path.startswith('/api/'):
        logging.getLogger('uvicorn.error').info('api path=%s status=%s duration_ms=%.1f', path, response.status_code, duration_ms)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    return response

# Terluar, agar request.session tersedia di middleware akses.
app.add_middleware(SessionMiddleware, secret_key=secret, max_age=8*3600,
                   same_site='strict', https_only=IS_CLOUD, session_cookie='ptapi_session')

@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return JSONResponse({'status':'error','message':str(exc.detail)}, status_code=exc.status_code)

@app.exception_handler(Exception)
async def server_error(request, exc):
    return JSONResponse({'status':'error','message':'Permintaan gagal. Periksa log server tanpa membagikan kredensial.'}, status_code=500)

def require(condition, message, code=400):
    if not condition:
        raise HTTPException(code, message)

def public_user(u):
    return {'username':u.username,'nama_lengkap':u.nama_lengkap,'role':u.role}

def log_event(session, unit, event, actor):
    session.add(LogScan(ID_Sarana=unit.ID_Sarana, Vendor=unit.Vendor or '-',
                        Alur_Scan=f'{event} | Oleh: {actor}', Waktu_Scan=now()))

def escaped(value):
    return html.escape(str(value or ''), quote=True)

@app.get('/', response_class=HTMLResponse)
def root():
    return HTMLResponse((BASE_DIR/'index.html').read_text(encoding='utf-8'), headers={'Cache-Control':'no-store'})

@app.get('/api/health')
def health():
    with SessionLocal() as session:
        session.execute(text('SELECT 1'))
    return {'status':'ok','database':'postgresql' if IS_POSTGRES else 'sqlite-local'}

@app.post('/api/login')
def login(request:Request, username:str=Form(...), password:str=Form(...)):
    username=username.strip()
    require(len(username)<=100 and len(password)<=256, 'Username atau password tidak valid.')
    key=hashlib.sha256(username.lower().encode()).hexdigest()
    with SessionLocal() as session:
        lock_writes(session)
        attempt=session.get(LoginAttempt,key)
        if not attempt:
            attempt=LoginAttempt(key=key,count=0,window_start=now());session.add(attempt)
        if (now()-attempt.window_start).total_seconds()>900:
            attempt.count=0;attempt.window_start=now()
        require(attempt.count<5,'Terlalu banyak percobaan. Coba lagi setelah 15 menit.',429)
        user=session.query(User).filter_by(username=username).first()
        if not user or not verify_password(password,user.password):
            attempt.count+=1;session.commit()
            raise HTTPException(401,'Username atau password salah.')
        attempt.count=0;session.commit()
        request.session.clear();request.session['user_id']=user.id
        request.session['credential_tag']=hashlib.sha256(user.password.encode()).hexdigest()
        return {'status':'success','user':public_user(user)}

@app.get('/api/me')
def me(request:Request):
    return {'status':'success', 'user':request.state.user}

@app.post('/api/logout')
def logout(request:Request):
    request.session.clear()
    return {'status':'success'}

@app.get('/api/config')
def config():
    url=os.getenv('GOOGLE_SHEET_URL','').strip()
    return {'google_sheet_url':url if url.startswith('https://docs.google.com/spreadsheets/') else '', 'sheets_enabled':configured()}

DEFAULT_VENDORS = [
    "AUP",
    "ASN",
    "HASURA",
    "GBU",
    "MUI",
    "ABC",
    "WANMEI",
]
def vendor_names(session):
    names = set(DEFAULT_VENDORS)

    names.update(
        row[0].strip().upper()
        for row in session.query(VendorRecord.name).all()
        if row[0]
    )

    names.update(
        row[0].strip().upper()
        for row in session.query(Sarana.Vendor).distinct().all()
        if row[0] and row[0].strip() != "-"
    )

    deleted = {
        row[0].strip().upper()
        for row in session.query(DeletedVendor.name).all()
    }

    names -= deleted

    defaults = [
        name for name in DEFAULT_VENDORS
        if name in names
    ]

    extras = sorted(names - set(DEFAULT_VENDORS))

    return defaults + extras

@app.get("/api/admin/sarana/vendors")
def list_vendors():
    with SessionLocal() as session:
        return {"vendors": vendor_names(session)}

@app.post("/api/admin/sarana/vendors")
def create_vendor(name: str = Form(...)):
    name = " ".join(name.strip().upper().split())

    require(
        bool(re.fullmatch(r"[A-Z0-9][A-Z0-9 .&()/_-]{0,99}", name)),
        "Nama vendor tidak valid. Maksimal 100 karakter.",
    )

    with SessionLocal() as session:
        lock_writes(session)

        require(
            name not in vendor_names(session),
            "Vendor sudah tersedia.",
            409,
        )

        deleted = session.get(DeletedVendor, name)

        if deleted:
            session.delete(deleted)

        if not session.get(VendorRecord, name):
            session.add(VendorRecord(name=name))

        session.commit()

    return {
        "status": "success",
        "name": name,
        "message": "Vendor berhasil ditambahkan.",
    }
@app.post("/api/admin/sarana/vendors/delete")
def delete_vendor(name: str = Form(...)):
    name = " ".join(name.strip().upper().split())

    with SessionLocal() as session:
        lock_writes(session)

        require(
            name in vendor_names(session),
            "Vendor tidak ditemukan dalam daftar pilihan.",
            404,
        )

        if not session.get(DeletedVendor, name):
            session.add(DeletedVendor(name=name))

        session.commit()

    return {
        "status": "success",
        "message": (
            f"Vendor {name} dihapus dari pilihan. "
            "Stok dan log sebelumnya tetap tersimpan."
        ),
    }

@app.get('/api/admin/database')
def database(request:Request, background_tasks:BackgroundTasks, include_master:bool=True):
    with SessionLocal() as session:
        master=[{'ID_Sarana':s.ID_Sarana,'Nama_Sarana':escaped(s.Nama_Sarana),
                 'No_Sarana':escaped(s.No_Sarana),'Vendor':escaped(s.Vendor or '-'),
                 'Status_Saat_Ini':escaped(s.Status_Saat_Ini),'Tujuan_Akhir':''}
                for s in session.query(Sarana).order_by(Sarana.ID_Sarana)] if include_master else []
        logs=[{'id':l.id,'ID_Sarana':l.ID_Sarana,'Alur_Scan':escaped(l.Alur_Scan),
               'Vendor':escaped(l.Vendor or '-'),'Tujuan_Akhir':'',
               'Waktu_Scan':l.Waktu_Scan.strftime('%Y-%m-%d %H:%M:%S')}
              for l in session.query(LogScan).order_by(LogScan.id.desc()).limit(500)]
    return {'master':master,'logs':logs}

@app.get('/api/dashboard')
def dashboard():
    grouped={}
    with SessionLocal() as session:
        for s in session.query(Sarana):
            status=s.Status_Saat_Ini or 'AT_API'
            if status.startswith('DELETED'):continue
            name=s.Nama_Sarana or 'Tanpa Nama Part'
            g=grouped.setdefault(name,dict(Nama_Part=escaped(name),Total_QTY=0,PT_API=0,Truck=0,L2_Vendor=0,Lost=0))
            g['Total_QTY']+=1
            col={'AT_API':'PT_API','AT_VENDOR':'L2_Vendor','IN_TRANSIT_OUT':'Truck','IN_TRANSIT_IN':'Truck','LOST_IN_TRANSIT':'Lost'}.get(status)
            if col:g[col]+=1
    return list(grouped.values())

@app.get('/api/admin/users')
def users():
    with SessionLocal() as session:
        return {'users':[dict(id=u.id,username=u.username,role=u.role,nama_lengkap=escaped(u.nama_lengkap),
                              npk=escaped(u.npk),jabatan=escaped(u.jabatan),is_primary=u.username==PRIMARY_ADMIN)
                         for u in session.query(User).order_by(User.id)]}

@app.post('/api/admin/users/create')
def create_user(nama_lengkap:str=Form(...),username:str=Form(...),npk:str=Form(...),
                jabatan:str=Form(...),role:str=Form(...),password:str=Form(...)):
    username=username.strip()
    require(bool(re.fullmatch(r'[A-Za-z0-9_.-]{3,50}',username)),'Username harus 3–50 karakter huruf/angka/titik/underscore/tanda minus.')
    require(role in ['admin','member','guest'],'Role tidak valid.')
    require(12<=len(password)<=256,'Password minimal 12 dan maksimal 256 karakter.')
    require(0<len(nama_lengkap.strip())<=150 and len(npk)<=50 and len(jabatan)<=100,'Data pegawai tidak valid.')
    with SessionLocal() as session:
        lock_writes(session)
        require(not session.query(User).filter_by(username=username).first(),'Username sudah digunakan.',409)
        session.add(User(username=username,password=hash_password(password),role=role,
                         nama_lengkap=nama_lengkap.strip(),npk=npk.strip(),jabatan=jabatan.strip()))
        session.commit()
    return {'status':'success','message':'Akun berhasil dibuat.'}

@app.post('/api/admin/users/reset-password/{user_id}')
def reset_password(user_id:int, request:Request, password:str=Form(...),
                   confirm_password:str=Form(...)):
    require(12 <= len(password) <= 256, 'Password minimal 12 dan maksimal 256 karakter.')
    require(password == confirm_password, 'Konfirmasi password tidak sama.')
    with SessionLocal() as session:
        lock_writes(session)
        user = session.get(User, user_id)
        require(user is not None, 'Akun tidak ditemukan.', 404)
        require(not verify_password(password, user.password), 'Gunakan password baru yang berbeda.')
        user.password = hash_password(password)
        key = hashlib.sha256(user.username.lower().encode()).hexdigest()
        attempt = session.get(LoginAttempt, key)
        if attempt:
            session.delete(attempt)
        session.add(AccountAudit(actor=request.state.actor,
                                 target_username=user.username, event='RESET_PASSWORD'))
        own_account = user.id == request.session['user_id']
        session.commit()
    if own_account:
        request.session.clear()
    return {'status':'success', 'logout':own_account,
            'message':'Password berhasil direset. Akun harus login menggunakan password baru.'}

@app.delete('/api/admin/users/delete/{user_id}')
def delete_user(user_id:int,request:Request):
    with SessionLocal() as session:
        lock_writes(session)
        user=session.get(User,user_id)
        require(user is not None,'Akun tidak ditemukan.',404)
        require(user.username!=PRIMARY_ADMIN and user.id!=request.session['user_id'],'Akun utama atau akun yang sedang dipakai tidak dapat dihapus.')
        if user.role=='admin':require(session.query(User).filter_by(role='admin').count()>1,'Admin terakhir tidak boleh dihapus.')
        session.delete(user);session.commit()
    return {'status':'success','message':'Akun dihapus.'}

@app.post('/api/admin/sarana/add-batch')
def add_batch(request:Request,background_tasks:BackgroundTasks,prefix_id:str=Form(...),
              nama_sarana:str=Form(...),qty:int=Form(...),vendor:str=Form(...)):
    prefix=prefix_id.strip().upper();name=nama_sarana.strip();vendor=vendor.strip().upper()
    require(bool(re.fullmatch(r'[A-Z0-9][A-Z0-9_-]{0,49}',prefix)),'Prefix tidak valid.')
    require(1<=qty<=100 and 0<len(name)<=150 and 0<len(vendor)<=100,'Jumlah 1–100, nama dan vendor wajib diisi.')
    items=[]
    with SessionLocal() as session:
        lock_writes(session)
        maximum=0
        for s in session.query(Sarana):
            if s.ID_Sarana.startswith(prefix+'-'):
                suffix=s.ID_Sarana[len(prefix)+1:]
                if suffix.isdigit():maximum=max(maximum,int(suffix))
        for n in range(maximum+1,maximum+qty+1):
            item=dict(ID_Sarana=f'{prefix}-{n:03d}',Nama_Sarana=name,No_Sarana=str(n),Vendor=vendor,Status_Saat_Ini='AT_API')
            unit=Sarana(**item);session.add(unit);items.append(item)
            log_event(session,unit,'TAMBAH STOK — Unit baru masuk PT API',request.state.actor)
        enqueue(session,{'action':'add_batch','items':items})
        session.commit()
    return {'status':'success','message':f'{qty} unit tersimpan di database online. Salinan Sheets mengikuti status sinkronisasi.'}

@app.post('/api/scan')
def scan(request:Request,background_tasks:BackgroundTasks,id_sarana:str=Form(...),alur:str=Form(...)):
    targets={'1':('AT_API','IN_TRANSIT_OUT','PT API Out → In Truck'),
             '2':('IN_TRANSIT_OUT','AT_VENDOR','In Truck → Vendor In'),
             '3':('AT_VENDOR','IN_TRANSIT_IN','Vendor Out → In Truck'),
             '4':('IN_TRANSIT_IN','AT_API','In Truck → PT API In')}
    require(alur in targets,'Alur tidak valid.')
    previous,target,label=targets[alur]
    with SessionLocal() as session:
        lock_writes(session)
        unit=session.get(Sarana,id_sarana.strip());require(unit is not None,'ID tidak terdaftar.',404)
        current=unit.Status_Saat_Ini or 'AT_API'
        require(not current.startswith('DELETED'),'Unit sudah dihapus karena NG.',409)
        require(current==previous or current=='LOST_IN_TRANSIT',f'Scan ditolak: status saat ini {current}.',409)
        if current=='LOST_IN_TRANSIT':label='RECOVERY ('+label+')'
        if alur == '2':
            begin_unloading_group(session, unit, request.state.actor)
        unit.Status_Saat_Ini=target;log_event(session,unit,label,request.state.actor)
        enqueue(session,{'action':'update_status','id_sarana':unit.ID_Sarana,'status':target})
        session.commit()
    return {'status':'success','message':f'Scan tersimpan: {target}.'}

@app.post('/api/admin/sarana/delete-ng')
def delete_ng(request:Request,background_tasks:BackgroundTasks,id_sarana:str=Form(...),alasan:str=Form(...)):
    reason=alasan.strip();require(0<len(reason)<=300,'Alasan wajib diisi, maksimal 300 karakter.')
    with SessionLocal() as session:
        lock_writes(session)
        unit=session.get(Sarana,id_sarana.strip());require(unit is not None,'ID tidak ditemukan.',404)
        require(not (unit.Status_Saat_Ini or '').startswith('DELETED'),'Unit sudah dihapus.',409)
        unit.Status_Saat_Ini=f'DELETED_NG ({reason})'
        log_event(session,unit,'DIHAPUS (NG) — '+reason,request.state.actor)
        enqueue(session,{'action':'delete','id_sarana':unit.ID_Sarana,'status':unit.Status_Saat_Ini})
        session.commit()
    return {'status':'success','message':'Unit NG dihapus dari stok aktif dan dicatat di log.'}

def latest_log_ids(session, ids):
    if not ids:
        return {}
    return dict(session.query(LogScan.ID_Sarana, func.max(LogScan.id)).filter(
        LogScan.ID_Sarana.in_(ids)).group_by(LogScan.ID_Sarana).all())

def begin_unloading_group(session, unit, actor):
    vendor, part = unit.Vendor or '-', unit.Nama_Sarana or ''
    groups = session.query(UnloadingGroup).filter_by(state='open', vendor=vendor, part=part).all()
    require(not any(g.actor != actor for g in groups),
            'Unloading vendor dan part ini sedang ditangani operator lain. Selesaikan sesi operator itu dahulu.', 409)
    if groups:
        group = groups[0]
        frozen = json.loads(group.snapshot)
        # Unit recovery boleh diterima, tetapi tidak mengaktifkan kandidat dari pengiriman baru.
        require(unit.ID_Sarana in frozen or unit.Status_Saat_Ini == 'LOST_IN_TRANSIT',
                'Unit berasal dari pengiriman berikutnya. Selesaikan unloading part ini dahulu.', 409)
        return
    units = session.query(Sarana).filter_by(Vendor=unit.Vendor, Nama_Sarana=unit.Nama_Sarana,
                                           Status_Saat_Ini='IN_TRANSIT_OUT').all()
    ids = [u.ID_Sarana for u in units]
    markers = latest_log_ids(session, ids)
    frozen = {id_:markers.get(id_,0) for id_ in ids}
    session.add(UnloadingGroup(actor=actor, vendor=vendor, part=part,
                               snapshot=json.dumps(frozen), state='open'))

def unloading_preview(session, actor):
    groups = session.query(UnloadingGroup).filter_by(actor=actor,state='open').order_by(UnloadingGroup.id).all()
    result = []
    for group in groups:
        frozen = json.loads(group.snapshot)
        units = session.query(Sarana).filter(Sarana.ID_Sarana.in_(list(frozen))).all() if frozen else []
        latest = latest_log_ids(session, list(frozen))
        remaining = sorted(u.ID_Sarana for u in units
                           if u.Status_Saat_Ini == 'IN_TRANSIT_OUT'
                           and (u.Vendor or '-') == group.vendor and (u.Nama_Sarana or '') == group.part
                           and latest.get(u.ID_Sarana,0) == frozen[u.ID_Sarana])
        result.append({'id':group.id, 'vendor':group.vendor, 'part':group.part, 'missing_ids':remaining})
    serialized = json.dumps(result, sort_keys=True, separators=(',',':'))
    token = hmac.new(secret.encode(), serialized.encode(), hashlib.sha256).hexdigest()
    return groups, result, token

@app.get('/api/unloading/preview')
def preview_unloading(request:Request):
    with SessionLocal() as session:
        _, groups, token = unloading_preview(session, request.state.actor)
    return {'groups':groups, 'token':token, 'missing_count':sum(len(g['missing_ids']) for g in groups)}

@app.post('/api/finish-vendor-unloading')
def finish_unloading(request:Request, preview_token:str=Form(...)):
    with SessionLocal() as session:
        lock_writes(session)
        groups, details, token = unloading_preview(session, request.state.actor)
        require(hmac.compare_digest(preview_token,token), 'Data berubah sejak pratinjau. Buka Selesai Unloading lagi.',409)
        if not groups:
            return {'status':'info','message':'Belum ada part yang mulai diterima pada sesi Anda. Stok truk tidak diubah.'}
        missing = sorted({id_ for g in details for id_ in g['missing_ids']})
        for id_ in missing:
            unit = session.get(Sarana,id_)
            unit.Status_Saat_Ini = 'LOST_IN_TRANSIT'
            log_event(session,unit,'HILANG / SELISIH UNLOADING',request.state.actor)
            enqueue(session,{'action':'update_status','id_sarana':id_,'status':'LOST_IN_TRANSIT'})
        for group in groups:
            group.state = 'closed'; group.closed_at = now()
        session.commit()
    return {'status':'warning' if missing else 'success', 'missing_ids':missing,
            'message':f'Unloading selesai. {len(missing)} unit ditandai hilang/selisih.' if missing else 'Unloading selesai. Part yang mulai diterima sudah lengkap.'}

@app.post('/api/sheets/drain')
def drain_sheets():
    # Dipanggil terpisah dari request scan. Antrean tetap persisten di database.
    flush_one()
    return {'status':'success'}

@app.post('/api/sync')
def sync():
    try:
        message=import_data()
    except Exception as error:
        if isinstance(error,ValueError):raise HTTPException(502,str(error))
        raise HTTPException(502,'Tidak bisa mengambil master Sheets. Periksa URL /exec, key, dan deployment Apps Script.')
    return {'status':'success','message':message}

@app.get('/api/sync/status')
def sync_status():
    with SessionLocal() as session:
        pending=session.query(SyncJob).filter(SyncJob.state!='done').count()
        first=session.query(SyncJob).filter(SyncJob.state!='done').order_by(SyncJob.id).first()
        return {'configured':configured(),'pending':pending,'first_id':first.id if first else None,
                'state':first.state if first else 'done',
                'message':first.error if first and first.error else '',
                'payload':json.loads(first.payload) if first else None,
                'reviewable':bool(first and (first.state=='review' or (first.state=='sending' and first.sent_at and (now()-first.sent_at).total_seconds()>600)))}

@app.post('/api/sync/resolve')
def resolve_sync(request:Request, background_tasks:BackgroundTasks, job_id:int=Form(...), decision:str=Form(...)):
    require(decision in ['verified','retry'],'Keputusan tidak valid.')
    with SessionLocal() as session:
        lock_writes(session)
        job=session.query(SyncJob).filter(SyncJob.state!='done').order_by(SyncJob.id).first()
        require(job is not None and job.id==job_id,'Antrean telah berubah. Muat ulang dahulu.',409)
        require(job.state=='review' or (job.state=='sending' and job.sent_at and (now()-job.sent_at).total_seconds()>600),
                'Pengiriman masih berjalan. Tunggu sekurangnya 10 menit sebelum pemeriksaan manual.',409)
        job.state='done' if decision=='verified' else 'pending'
        job.error='Ditinjau oleh '+request.state.actor+'; keputusan: '+decision
        session.commit()
    return {'status':'success','message':'Keputusan dicatat. Antrean berikutnya dapat diproses.'}
