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
from database import (VendorRecord, DeletedVendor, CustomerRecord, LOCAL_TRIAL,)
from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool
from sqlalchemy import text, func, or_
from database import (BASE_DIR, IS_CLOUD, IS_POSTGRES, PRIMARY_ADMIN, SessionLocal,
                      Sarana, LogScan, User, SyncJob, LoginAttempt, AccountAudit, UnloadingGroup, init_db,
                      lock_writes, hash_password, verify_password,
                      encrypt_password_view, decrypt_password_view, now)
from sync_sheets import import_data, enqueue, flush_one, configured

secret = os.getenv('SESSION_SECRET', '')
if len(secret) < 32:
    if IS_CLOUD:
        raise RuntimeError('SESSION_SECRET minimal 32 karakter wajib diisi.')
    secret = secrets.token_urlsafe(48)
os.environ['SESSION_SECRET'] = secret

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
            if path in ['/api/scan','/api/finish-vendor-unloading','/api/finish-api-unloading','/api/finish-customer-unloading','/api/unloading/preview','/api/sheets/drain'] and user.role not in ['admin','member']:
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
    response.headers['Content-Security-Policy'] = (
        "default-src 'self' blob: data:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
        "https://cdn.tailwindcss.com https://unpkg.com "
        "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "media-src 'self' blob:; "
        "worker-src 'self' blob:; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'"
    )
    return response

# Terluar, agar request.session tersedia di middleware akses.
app.add_middleware(SessionMiddleware, secret_key=secret, max_age=8*3600,
                   same_site='strict', https_only=IS_CLOUD, session_cookie='ptapi_session')

@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return JSONResponse({'status':'error','message':str(exc.detail)}, status_code=exc.status_code)

@app.exception_handler(Exception)
async def server_error(request, exc):
    logging.getLogger('uvicorn.error').error(
        'unhandled error method=%s path=%s',
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse({'status':'error','message':'Permintaan gagal. Periksa log server tanpa membagikan kredensial.'}, status_code=500)

def require(condition, message, code=400):
    if not condition:
        raise HTTPException(code, message)

LOGIN_WINDOW = datetime.timedelta(minutes=15)
LOGIN_USER_LIMIT = 5
LOGIN_IP_LIMIT = 30
LOGIN_GLOBAL_LIMIT = 200

def _login_attempt_key(prefix, value):
    return prefix + hashlib.sha256(value.encode()).hexdigest()

def _login_attempt(session, key):
    attempt = session.get(LoginAttempt, key)
    if not attempt:
        attempt = LoginAttempt(
            key=key,
            count=0,
            window_start=now(),
        )
        session.add(attempt)
    elif (
        attempt.window_start is None
        or now() - attempt.window_start > LOGIN_WINDOW
    ):
        attempt.count = 0
        attempt.window_start = now()
    return attempt

def _login_allowed(attempt, limit):
    require(
        attempt.count < limit,
        'Terlalu banyak percobaan. Coba lagi setelah 15 menit.',
        429,
    )

def _cleanup_login_attempts(session):
    session.query(LoginAttempt).filter(
        LoginAttempt.window_start < now() - datetime.timedelta(days=1)
    ).delete(synchronize_session=False)

SCAN_LABELS = {
    '1': 'PT API ke Vendor',
    '2': 'Vendor Inbound',
    '3': 'Vendor Outbound',
    '4': 'PT API dari Vendor',
    '5': 'PT API ke Customer',
    '6': 'PT API dari Customer',
}


def allowed_scans(user):
    if user.role == 'admin':
        return list(SCAN_LABELS)

    if user.role != 'member':
        return []

    try:
        values = json.loads(user.scan_permissions or '[]')
    except (TypeError, ValueError):
        return []

    if not isinstance(values, list):
        return []

    return [
        key for key in SCAN_LABELS
        if key in values
    ]


def public_user(u):
    return {
        'username': u.username,
        'nama_lengkap': u.nama_lengkap,
        'npk': u.npk or '',
        'jabatan': u.jabatan or '',
        'role': u.role,
        'scan_permissions': allowed_scans(u),
    }


def require_scan_access(session, request, alur, vendor=None):
    user = session.get(
        User,
        request.session.get('user_id'),
    )

    require(
        user is not None,
        'Silakan login kembali.',
        401,
    )

    require(
        alur in allowed_scans(user),
        'Akun tidak memiliki izin scan bagian ini.',
        403,
    )

    # Tidak ada lagi pembatasan berdasarkan nama vendor.
    return user


def validate_access(
    session,
    role,
    raw_permissions,
    vendor_scope='',
):
    require(
        role in ('admin', 'member', 'guest'),
        'Role tidak valid.',
    )

    try:
        values = json.loads(raw_permissions)
    except (TypeError, ValueError):
        raise HTTPException(
            400,
            'Format izin scan tidak valid.',
        )

    require(
        isinstance(values, list)
        and all(
            isinstance(value, str)
            and value in SCAN_LABELS
            for value in values
        ),
        'Pilihan izin scan tidak valid.',
    )

    values = (
        sorted(set(values))
        if role == 'member'
        else []
    )

    if role == 'member':
        require(
            bool(values),
            'Pilih minimal satu izin scan.',
        )

        require(
            not (
                set(values) & {'2', '3'}
                and set(values) & {'1', '4', '5', '6'}
            ),
            'Pisahkan akun vendor dan akun PT API.',
        )

    return json.dumps(values), ''


def current_partner(unit):
    status = unit.Status_Saat_Ini or 'AT_API'

    if status == 'AT_API':
        return ''

    if status == 'AT_CUSTOMER':
        return unit.Customer or ''

    return unit.Customer or unit.Vendor or ''


def log_event(
    session,
    unit,
    event,
    actor,
    customer=None,
):
    partner = (
        current_partner(unit)
        if customer is None
        else customer
    )

    log = LogScan(
        ID_Sarana=unit.ID_Sarana,
        Vendor=partner or '-',
        Customer='',
        Alur_Scan=f'{event} | Oleh: {actor}',
        Waktu_Scan=now(),
    )

    session.add(log)
    return log


def record_status_change(
    session,
    unit,
    event,
    actor,
    customer=None,
    previous=None,
):
    if (
        customer is None
        and previous == 'AT_API'
        and (unit.Status_Saat_Ini or '').startswith('DELETED')
    ):
        customer = ''

    log = log_event(
        session,
        unit,
        event,
        actor,
        customer,
    )

    enqueue(session, {
        'action': (
            'delete'
            if (unit.Status_Saat_Ini or '').startswith('DELETED')
            else 'update_status'
        ),
        'previous_status': previous,
        'id_sarana': unit.ID_Sarana,
        'status': unit.Status_Saat_Ini,
        'partner': current_partner(unit),
        'last_scan_at': (
            unit.Last_Scan_At.strftime('%Y-%m-%d %H:%M:%S')
            if unit.Last_Scan_At
            else ''
        ),
        'log_partner': (
            ''
            if log.Vendor == '-'
            else log.Vendor
        ),
        'event': log.Alur_Scan,
        'waktu_scan': log.Waktu_Scan.strftime(
            '%Y-%m-%d %H:%M:%S'
        ),
    })


def unit_dict(unit):
    return {
        'ID_Sarana': unit.ID_Sarana,
        'Nama_Sarana': escaped(unit.Nama_Sarana),
        'No_Sarana': escaped(unit.No_Sarana),
        'Lokasi_Mitra': escaped(
            current_partner(unit) or '-'
        ),
        'Status_Saat_Ini': escaped(
            unit.Status_Saat_Ini
        ),
        'Last_Scan_At': (
            unit.Last_Scan_At.strftime('%Y-%m-%d %H:%M:%S')
            if unit.Last_Scan_At
            else ''
        ),
        'Days_Since_Scan': (
            max(0, (now() - unit.Last_Scan_At).days)
            if unit.Last_Scan_At
            else None
        ),
    }


def vendor_names(session):
    names = set(DEFAULT_VENDORS)

    names.update(
        name.strip().upper()
        for (name,) in session.query(VendorRecord.name)
        if name
    )

    names -= {
        name.strip().upper()
        for (name,) in session.query(DeletedVendor.name)
        if name
    }

    return sorted(names)

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

    client_ip = (
        request.client.host
        if request.client and request.client.host
        else 'unknown'
    )
    user_key = _login_attempt_key('user:', username.lower())
    ip_key = _login_attempt_key('ip:', client_ip)
    global_key = 'global-login'

    with SessionLocal() as session:
        lock_writes(session)
        _cleanup_login_attempts(session)

        user_attempt = _login_attempt(session, user_key)
        ip_attempt = _login_attempt(session, ip_key)
        global_attempt = _login_attempt(session, global_key)

        _login_allowed(user_attempt, LOGIN_USER_LIMIT)
        _login_allowed(ip_attempt, LOGIN_IP_LIMIT)
        _login_allowed(global_attempt, LOGIN_GLOBAL_LIMIT)

        user=session.query(User).filter_by(username=username).first()
        if not user or not verify_password(password,user.password):
            user_attempt.count += 1
            ip_attempt.count += 1
            global_attempt.count += 1
            session.commit()
            raise HTTPException(401,'Username atau password salah.')

        user_attempt.count=0
        session.commit()
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
        name.strip().upper()
        for (name,) in session.query(VendorRecord.name)
        if name
    )

    names -= {
        name.strip().upper()
        for (name,) in session.query(DeletedVendor.name)
        if name
    }

    return sorted(names)

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
        require(
            not session.query(Sarana).filter(
                Sarana.Vendor == name,
                Sarana.Status_Saat_Ini.in_([
                    'AT_VENDOR',
                    'IN_TRANSIT_OUT',
                    'IN_TRANSIT_IN',
                    'LOST_IN_TRANSIT',
                ]),
            ).first(),
            'Vendor masih dipakai oleh sarana aktif.',
            409,
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
def database(
    request: Request,
    background_tasks: BackgroundTasks,
    include_master: bool = True,
    start_date: str = None,
    end_date: str = None,
):
    require(
        not include_master or request.state.role == 'admin',
        'Master data khusus admin.',
        403,
    )

    with SessionLocal() as session:
        master = (
            [
                unit_dict(unit)
                for unit in session.query(Sarana).order_by(Sarana.ID_Sarana)
            ]
            if include_master
            else []
        )

        # Filter Query Log Scan Berdasarkan Rentang Tanggal
        query = session.query(LogScan)
        
        if start_date:
            s_date = datetime.datetime.strptime(start_date, '%Y-%m-%d')
            query = query.filter(LogScan.Waktu_Scan >= s_date)
            
        if end_date:
            e_date = datetime.datetime.strptime(end_date, '%Y-%m-%d') + datetime.timedelta(days=1)
            query = query.filter(LogScan.Waktu_Scan < e_date)

        logs = [
            {
                'id': log.id,
                'ID_Sarana': log.ID_Sarana,
                'Alur_Scan': escaped(log.Alur_Scan),
                'Vendor': escaped(log.Vendor or '-'),
                'Customer': escaped(log.Customer or ''),
                'Waktu_Scan': log.Waktu_Scan.strftime('%Y-%m-%d %H:%M:%S'),
            }
            for log in query.order_by(LogScan.id.desc()).limit(1000)
        ]

    return {
        'master': master,
        'logs': logs,
    }

@app.get('/api/dashboard')
def dashboard():
    grouped = {}

    with SessionLocal() as session:
        for unit in session.query(Sarana):
            status = unit.Status_Saat_Ini or 'AT_API'

            if status.startswith('DELETED'):
                continue

            name = unit.Nama_Sarana or 'Tanpa Nama Part'

            group = grouped.setdefault(
                name,
                dict(
                    Nama_Part=escaped(name),
                    Total_QTY=0,
                    PT_API=0,
                    Truck=0,
                    L2_Vendor=0,
                    Customer=0,
                    Lost=0,
                    Vendor_Detail={},
                    Customer_Detail={},
                ),
            )

            group['Total_QTY'] += 1

            column = {
                'AT_API': 'PT_API',
                'IN_TRANSIT_OUT': 'Truck',
                'IN_TRANSIT_IN': 'Truck',
                'AT_VENDOR': 'L2_Vendor',
                'AT_CUSTOMER': 'Customer',
                'LOST_IN_TRANSIT': 'Lost',
            }.get(status)

            if column:
                group[column] += 1

            detail = (
                'Vendor_Detail'
                if status == 'AT_VENDOR'
                else (
                    'Customer_Detail'
                    if status == 'AT_CUSTOMER'
                    else None
                )
            )

            if detail:
                partner = (
                    current_partner(unit)
                    or 'Belum tercatat'
                )

                group[detail][partner] = (
                    group[detail].get(partner, 0) + 1
                )

    for group in grouped.values():
        for key in (
            'Vendor_Detail',
            'Customer_Detail',
        ):
            group[key] = [
                {'nama': escaped(name), 'qty': qty}
                for name, qty in sorted(
                    group[key].items()
                )
            ]

    return list(grouped.values())

@app.get('/api/admin/users')
def users():
    with SessionLocal() as session:
        return {
            'users': [
                dict(
                    id=user.id,
                    username=user.username,
                    role=user.role,
                    nama_lengkap=escaped(user.nama_lengkap),
                    npk=escaped(user.npk),
                    jabatan=escaped(user.jabatan),
                    is_primary=user.username == PRIMARY_ADMIN,
                    scan_permissions=allowed_scans(user),
                    vendor_scope=user.vendor_scope or '',
                    password_view=decrypt_password_view(
                        user.password_view
                    ),
                )
                for user in session.query(User)
                .order_by(User.id)
            ]
        }

@app.post('/api/admin/users/create')
def create_user(
    request: Request,
    nama_lengkap: str = Form(...),
    username: str = Form(...),
    npk: str = Form(...),
    jabatan: str = Form(...),
    role: str = Form(...),
    password: str = Form(...),
    scan_permissions: str = Form('[]'),
    vendor_scope: str = Form(''),
):
    username = username.strip()

    require(
        bool(re.fullmatch(
            r'[A-Za-z0-9_.-]{3,50}',
            username,
        )),
        'Username tidak valid.',
    )

    require(
        8 <= len(password) <= 256,
        'Password minimal 8 dan maksimal 256 karakter.',
    )

    require(
        0 < len(nama_lengkap.strip()) <= 150
        and len(npk) <= 50
        and len(jabatan) <= 100,
        'Data pegawai tidak valid.',
    )

    with SessionLocal() as session:
        lock_writes(session)

        permissions, scope = validate_access(
            session,
            role,
            scan_permissions,
            vendor_scope,
        )

        require(
            not session.query(User)
            .filter_by(username=username)
            .first(),
            'Username sudah digunakan.',
            409,
        )

        session.add(User(
            username=username,
            password=hash_password(password),
            password_view=encrypt_password_view(password),
            role=role,
            nama_lengkap=nama_lengkap.strip(),
            npk=npk.strip(),
            jabatan=jabatan.strip(),
            scan_permissions=permissions,
            vendor_scope=scope,
        ))
        session.add(AccountAudit(
            actor=request.state.actor,
            target_username=username,
            event='CREATE_USER',
        ))

        session.commit()

    return {
        'status': 'success',
        'message': 'Akun berhasil dibuat.',
    }

@app.post('/api/admin/users/reset-password/{user_id}')
def reset_password(user_id:int, request:Request, password:str=Form(...),
                   confirm_password:str=Form(...)):
    require(8 <= len(password) <= 256, 'Password minimal 8 dan maksimal 256 karakter.')
    require(password == confirm_password, 'Konfirmasi password tidak sama.')
    with SessionLocal() as session:
        lock_writes(session)
        user = session.get(User, user_id)
        require(user is not None, 'Akun tidak ditemukan.', 404)
        require(not verify_password(password, user.password), 'Gunakan password baru yang berbeda.')
        user.password = hash_password(password)
        user.password_view = encrypt_password_view(password)
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
        session.add(AccountAudit(
            actor=request.state.actor,
            target_username=user.username,
            event='DELETE_USER',
        ))
        session.delete(user);session.commit()
    return {'status':'success','message':'Akun dihapus.'}

@app.post('/api/admin/sarana/add-batch')
def add_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    prefix_id: str = Form(...),
    nama_sarana: str = Form(...),
    qty: int = Form(...),
):
    prefix = prefix_id.strip().upper()
    name = nama_sarana.strip()

    require(
        bool(re.fullmatch(
            r'[A-Z0-9][A-Z0-9_-]{0,49}',
            prefix,
        )),
        'Prefix tidak valid.',
    )

    require(
        1 <= qty <= 100
        and 0 < len(name) <= 150,
        'Jumlah 1–100 dan nama wajib diisi.',
    )

    require(
        configured() or LOCAL_TRIAL,
        'Integrasi Sheets belum dikonfigurasi.',
        503,
    )

    items = []

    with SessionLocal() as session:
        lock_writes(session)

        maximum = 0

        for unit in session.query(Sarana):
            if unit.ID_Sarana.startswith(prefix + '-'):
                suffix = unit.ID_Sarana[len(prefix) + 1:]

                if suffix.isdigit():
                    maximum = max(
                        maximum,
                        int(suffix),
                    )

        for number in range(
            maximum + 1,
            maximum + qty + 1,
        ):
            unit = Sarana(
                ID_Sarana=f'{prefix}-{number:03d}',
                Nama_Sarana=name,
                No_Sarana=str(number),
                Vendor='',
                Customer='',
                Status_Saat_Ini='AT_API',
            )

            session.add(unit)

            log = log_event(
                session,
                unit,
                'TAMBAH STOK COMMON',
                request.state.actor,
            )

            items.append({
                'ID_Sarana': unit.ID_Sarana,
                'Nama_Sarana': name,
                'No_Sarana': str(number),
                'Vendor_Customer': '',
                'Status_Saat_Ini': 'AT_API',
                'Scan_Terakhir': '',
                'Alur_Scan': log.Alur_Scan,
                'Waktu_Scan': log.Waktu_Scan.strftime(
                    '%Y-%m-%d %H:%M:%S'
                ),
            })

        enqueue(session, {
            'action': 'add_batch',
            'items': items,
        })

        session.commit()

    return {
        'status': 'success',
        'message': f'{qty} sarana common ditambahkan.',
    }

@app.post('/api/scan')
def scan(
    request: Request,
    background_tasks: BackgroundTasks,
    id_sarana: str = Form(...),
    alur: str = Form(...),
    customer: str = Form(''),
    vendor: str = Form(''),
):
    targets = {
        '1': (
            'AT_API',
            'IN_TRANSIT_OUT',
            'PT API Out → In Truck',
        ),
        '2': (
            'IN_TRANSIT_OUT',
            'AT_VENDOR',
            'In Truck → Vendor In',
        ),
        '3': (
            'AT_VENDOR',
            'IN_TRANSIT_IN',
            'Vendor Out → In Truck',
        ),
        '4': (
            'IN_TRANSIT_IN',
            'AT_API',
            'In Truck → PT API In',
        ),
        '5': (
            'AT_API',
            'AT_CUSTOMER',
            'PT API Out → Customer',
        ),
        '6': (
            'AT_CUSTOMER',
            'AT_API',
            'Customer → PT API In',
        ),
    }

    require(alur in targets, 'Alur tidak valid.')

    require(
        configured() or LOCAL_TRIAL,
        'Integrasi Sheets belum dikonfigurasi.',
        503,
    )

    previous, target, label = targets[alur]

    with SessionLocal() as session:
        lock_writes(session)

        user = require_scan_access(
            session,
            request,
            alur,
        )

        unit = session.get(
            Sarana,
            id_sarana.strip(),
        )

        require(
            unit is not None,
            'ID tidak terdaftar.',
            404,
        )

        current = unit.Status_Saat_Ini or 'AT_API'

        recovery = (
            current == 'LOST_IN_TRANSIT'
            and user.role == 'admin'
            and (
                (
                    alur in ('2', '4')
                    and bool(unit.Vendor)
                )
                or (
                    alur == '6'
                    and bool(unit.Customer)
                )
            )
        )

        require(
            current == previous or recovery,
            f'Status saat ini {current}, scan ditolak.',
            409,
        )

        if recovery:
            label = 'RECOVERY (' + label + ')'

        partner = current_partner(unit)

        if alur == '1':
            vendor = ' '.join(
                vendor.strip().upper().split()
            )

            require(
                vendor in vendor_names(session),
                'Pilih vendor tujuan yang tersedia.',
            )

            unit.Vendor = vendor
            unit.Customer = ''
            partner = vendor

        elif alur == '5':
            customer = ' '.join(
                customer.strip().upper().split()
            )

            destination = session.get(
                CustomerRecord,
                customer,
            )

            require(
                destination is not None
                and destination.active == 1,
                'Pilih customer aktif sebelum scan.',
            )

            unit.Customer = customer
            unit.Vendor = ''
            partner = customer

        else:
            require(
                bool(partner),
                'Tujuan unit belum tercatat. Hubungi admin.',
                409,
            )

        unloading_kind = {
            '2': 'vendor',
            '4': 'api',
            '6': 'customer',
        }.get(alur)

        if unloading_kind:
            begin_unloading_group(
                session,
                unit,
                request.state.actor,
                unloading_kind,
            )

        unit.Status_Saat_Ini = target
        unit.Last_Scan_At = now()

        if target == 'AT_API':
            unit.Vendor = ''
            unit.Customer = ''

        record_status_change(
            session,
            unit,
            label + f' ({partner})',
            request.state.actor,
            partner,
            previous=current,
        )

        session.commit()

    return {
        'status': 'success',
        'message': f'Scan tersimpan: {target}.',
    }

@app.get('/api/admin/sarana/options')
def sarana_options():
    with SessionLocal() as session:
        rows = (
            session.query(
                Sarana.ID_Sarana,
                Sarana.Nama_Sarana,
                Sarana.Status_Saat_Ini,
            )
            .filter(or_(
                Sarana.Status_Saat_Ini.is_(None),
                ~Sarana.Status_Saat_Ini.startswith('DELETED'),
            ))
            .order_by(Sarana.ID_Sarana)
            .all()
        )
    return {
        'status': 'success',
        'units': [
            {
                'ID_Sarana': row.ID_Sarana,
                'Nama_Sarana': escaped(row.Nama_Sarana),
                'Status_Saat_Ini': escaped(row.Status_Saat_Ini),
            }
            for row in rows
        ],
    }

@app.post('/api/admin/sarana/delete-ng')
def delete_ng(request:Request,background_tasks:BackgroundTasks,id_sarana:str=Form(...),alasan:str=Form(...)):
    reason=alasan.strip();require(0<len(reason)<=300,'Alasan wajib diisi, maksimal 300 karakter.')
    require(
        configured() or LOCAL_TRIAL,
        'Integrasi Sheets belum dikonfigurasi.',
        503,
    )
    with SessionLocal() as session:
        lock_writes(session)
        unit=session.get(Sarana,id_sarana.strip());require(unit is not None,'ID tidak ditemukan.',404)
        require(not (unit.Status_Saat_Ini or '').startswith('DELETED'),'Unit sudah dihapus.',409)
        previous = unit.Status_Saat_Ini
        unit.Status_Saat_Ini = f'DELETED_NG ({reason})'
        record_status_change(
            session,
            unit,
            'DIHAPUS (NG) — ' + reason,
            request.state.actor,
            previous=previous,
        )
        session.commit()
    return {'status':'success','message':'Unit NG dihapus dari stok aktif dan dicatat di log.'}

UNLOADING_KINDS = {
    'vendor': {
        'flow': '2',
        'status': 'IN_TRANSIT_OUT',
        'partner_field': 'vendor',
        'name': 'Vendor',
        'event': 'HILANG / SELISIH UNLOADING VENDOR',
        'empty': 'Belum ada part yang mulai diterima pada sesi Anda. Stok truk tidak diubah.',
    },
    'api': {
        'flow': '4',
        'status': 'IN_TRANSIT_IN',
        'partner_field': 'vendor',
        'name': 'API',
        'event': 'HILANG / SELISIH UNLOADING API',
        'empty': 'Belum ada part yang mulai diterima di PT API pada sesi Anda. Stok truk tidak diubah.',
    },
    'customer': {
        'flow': '6',
        'status': 'AT_CUSTOMER',
        'partner_field': 'customer',
        'name': 'Customer',
        'event': 'HILANG / SELISIH UNLOADING CUSTOMER',
        'empty': 'Belum ada part yang mulai diterima dari customer pada sesi Anda. Stok truk tidak diubah.',
    },
}

def latest_log_ids(session, ids):
    if not ids:
        return {}
    return dict(session.query(LogScan.ID_Sarana, func.max(LogScan.id)).filter(
        LogScan.ID_Sarana.in_(ids)).group_by(LogScan.ID_Sarana).all())

def unloading_partner_value(unit, partner_field):
    return (
        unit.Customer
        if partner_field == 'customer'
        else unit.Vendor
    )

def begin_unloading_group(session, unit, actor, kind):
    config = UNLOADING_KINDS[kind]
    partner_field = config['partner_field']
    partner_value = unloading_partner_value(unit, partner_field)
    partner = partner_value or '-'
    part = unit.Nama_Sarana or ''
    groups = session.query(UnloadingGroup).filter_by(
        state='open',
        kind=kind,
        part=part,
        **{partner_field: partner},
    ).all()
    require(not any(g.actor != actor for g in groups),
            f"Unloading {config['name']} dan part ini sedang ditangani operator lain. Selesaikan sesi operator itu dahulu.", 409)
    if groups:
        group = groups[0]
        frozen = json.loads(group.snapshot)
        # Unit recovery boleh diterima, tetapi tidak mengaktifkan kandidat dari pengiriman baru.
        require(unit.ID_Sarana in frozen or unit.Status_Saat_Ini == 'LOST_IN_TRANSIT',
                f"Unit berasal dari pengiriman berikutnya. Selesaikan unloading {config['name']} part ini dahulu.", 409)
        return
    unit_partner_field = (
        'Customer'
        if partner_field == 'customer'
        else 'Vendor'
    )
    units = session.query(Sarana).filter_by(
        Nama_Sarana=unit.Nama_Sarana,
        Status_Saat_Ini=config['status'],
        **{unit_partner_field: partner_value},
    ).all()
    ids = [u.ID_Sarana for u in units]
    markers = latest_log_ids(session, ids)
    frozen = {id_:markers.get(id_,0) for id_ in ids}
    group_values = {'vendor': '', 'customer': ''}
    group_values[partner_field] = partner
    session.add(UnloadingGroup(
        kind=kind,
        actor=actor,
        part=part,
        snapshot=json.dumps(frozen),
        state='open',
        **group_values,
    ))

def unloading_preview(session, actor, kind):
    config = UNLOADING_KINDS[kind]
    partner_field = config['partner_field']
    groups = session.query(UnloadingGroup).filter_by(
        actor=actor, state='open', kind=kind).order_by(UnloadingGroup.id).all()
    result = []
    for group in groups:
        frozen = json.loads(group.snapshot)
        units = session.query(Sarana).filter(Sarana.ID_Sarana.in_(list(frozen))).all() if frozen else []
        latest = latest_log_ids(session, list(frozen))
        group_partner = getattr(group, partner_field) or '-'
        remaining = sorted(
            u.ID_Sarana for u in units
            if u.Status_Saat_Ini == config['status']
            and (u.Nama_Sarana or '') == group.part
            and (unloading_partner_value(u, partner_field) or '-') == group_partner
            and latest.get(u.ID_Sarana,0) == frozen[u.ID_Sarana]
        )
        result.append({
            'id': group.id,
            'kind': group.kind,
            'vendor': group.vendor or '',
            'customer': group.customer or '',
            'partner': group_partner,
            'part': group.part,
            'missing_ids': remaining,
        })
    serialized = json.dumps(result, sort_keys=True, separators=(',',':'))
    token = hmac.new(secret.encode(), serialized.encode(), hashlib.sha256).hexdigest()
    return groups, result, token

@app.get('/api/unloading/preview')
def preview_unloading(request:Request, kind:str='vendor'):
    require(kind in UNLOADING_KINDS, 'Jenis unloading tidak valid.')
    config = UNLOADING_KINDS[kind]
    with SessionLocal() as session:
        require_scan_access(session, request, config['flow'])
        _, details, token = unloading_preview(
            session,
            request.state.actor,
            kind,
        )
        for group in details:
            require_scan_access(
                session,
                request,
                config['flow'],
                group['partner'],
            )
    return {
        'kind': kind,
        'groups': details,
        'token': token,
        'missing_count': sum(len(g['missing_ids']) for g in details),
    }

def finish_unloading(request, kind, preview_token):
    config = UNLOADING_KINDS[kind]
    require(
        configured() or LOCAL_TRIAL,
        'Integrasi Sheets belum dikonfigurasi.',
        503,
    )
    with SessionLocal() as session:
        lock_writes(session)
        require_scan_access(session, request, config['flow'])

        groups, details, token = unloading_preview(
            session,
            request.state.actor,
            kind,
        )

        for group in groups:
            require_scan_access(
                session,
                request,
                config['flow'],
                getattr(group, config['partner_field']) or '',
            )
        require(hmac.compare_digest(preview_token,token), 'Data berubah sejak pratinjau. Buka Selesai Unloading lagi.',409)
        if not groups:
            return {'status':'info','message':config['empty']}
        missing = sorted({id_ for g in details for id_ in g['missing_ids']})
        for id_ in missing:
            unit = session.get(Sarana,id_)
            require(unit is not None, 'Unit tidak ditemukan. Muat ulang data dan periksa stok.', 409)
            unit.Status_Saat_Ini = 'LOST_IN_TRANSIT'
            record_status_change(
                session,
                unit,
                config['event'],
                request.state.actor,
                previous=config['status'],
            )
        for group in groups:
            group.state = 'closed'; group.closed_at = now()
        session.commit()
    return {'status':'warning' if missing else 'success', 'missing_ids':missing,
            'message':f"Unloading {config['name']} selesai. {len(missing)} unit ditandai hilang/selisih." if missing else f"Unloading {config['name']} selesai. Part yang mulai diterima sudah lengkap."}

@app.post('/api/finish-vendor-unloading')
def finish_vendor_unloading(request:Request, preview_token:str=Form(...)):
    return finish_unloading(request, 'vendor', preview_token)

@app.post('/api/finish-api-unloading')
def finish_api_unloading(request:Request, preview_token:str=Form(...)):
    return finish_unloading(request, 'api', preview_token)

@app.post('/api/finish-customer-unloading')
def finish_customer_unloading(request:Request, preview_token:str=Form(...)):
    return finish_unloading(request, 'customer', preview_token)

@app.post('/api/sheets/drain')
def drain_sheets():
    # Dipanggil terpisah dari request scan.
    # Mengembalikan hasil pengiriman antrean yang sebenarnya.
    return flush_one()

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
@app.get('/api/customers')
def list_customers():
    with SessionLocal() as session:
        return {
            'customers': [
                customer.name
                for customer in session.query(CustomerRecord)
                .filter_by(active=1)
                .order_by(CustomerRecord.name)
            ]
        }
@app.post('/api/admin/sarana/customers')
def add_customer(name: str = Form(...)):
    name = ' '.join(
        name.strip().upper().split()
    )
    require(
        bool(re.fullmatch(
            r'[A-Z0-9][A-Z0-9 .&()/_-]{0,99}',
            name,
        )),
        'Nama customer tidak valid. Maksimal 100 karakter.',
    )
    with SessionLocal() as session:
        lock_writes(session)

        customer = session.get(
            CustomerRecord,
            name,
        )
        require(
            not customer or customer.active != 1,
            'Customer sudah tersedia.',
            409,
        )
        if customer:
            customer.active = 1
        else:
            session.add(CustomerRecord(
                name=name,
                active=1,
            ))
        session.commit()
    return {
        'status': 'success',
        'message': 'Customer ditambahkan.',
    }
@app.post('/api/admin/sarana/customers/delete')
def remove_customer(name: str = Form(...)):
    name = ' '.join(
        name.strip().upper().split()
    )
    with SessionLocal() as session:
        lock_writes(session)
        customer = session.get(
            CustomerRecord,
            name,
        )
        require(
            customer is not None
            and customer.active == 1,
            'Customer tidak ditemukan.',
            404,
        )
        require(
            not session.query(Sarana).filter_by(
                Customer=name,
                Status_Saat_Ini='AT_CUSTOMER',
            ).first(),
            'Masih ada sarana tercatat pada customer ini.',
            409,
        )
        customer.active = 0
        session.commit()
    return {
        'status': 'success',
        'message': (
            'Customer dihapus dari pilihan. '
            'Riwayat tetap tersimpan.'
        ),
    }
@app.post('/api/admin/users/access/{user_id}')
def update_user_access(
    user_id: int,
    request: Request,
    scan_permissions: str = Form('[]'),
    vendor_scope: str = Form(''),
):
    with SessionLocal() as session:
        lock_writes(session)
        user = session.get(User, user_id)
        require(
            user is not None,
            'Akun tidak ditemukan.',
            404,
        )
        require(
            user.role == 'member',
            'Pengaturan ini khusus akun operator.',
        )
        require(
            not session.query(UnloadingGroup).filter_by(
                actor=user.username,
                state='open',
            ).first(),
            'Selesaikan sesi unloading akun ini '
            'sebelum mengganti izin.',
            409,
        )
        user.scan_permissions, user.vendor_scope = (
            validate_access(
                session,
                user.role,
                scan_permissions,
                vendor_scope,
            )
        )
        session.add(AccountAudit(
            actor=request.state.actor,
            target_username=user.username,
            event='UPDATE_SCAN_ACCESS',
        ))
        session.commit()
    return {
        'status': 'success',
        'message': (
            'Izin diperbarui. Pengguna perlu '
            'memuat ulang tampilan.'
        ),
    }
@app.get('/api/vendors')
def scan_vendor_list():
    with SessionLocal() as session:
        return {
            'vendors': vendor_names(session)
        }