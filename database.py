import datetime
import os
import re
import hashlib
import hmac
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.pool import NullPool, QueuePool

BASE_DIR = Path(__file__).resolve().parent
LOCAL_TRIAL = os.getenv('LOCAL_TRIAL') == '1'
if LOCAL_TRIAL:
    if os.getenv('VERCEL'):
        raise RuntimeError('Mode trial lokal tidak boleh dijalankan di Vercel.')
    load_dotenv(BASE_DIR / '.env.local', override=True)
    os.environ['DATABASE_URL'] = f"sqlite:///{(BASE_DIR / 'trial_local.db').as_posix()}"
    # Trial tidak boleh membaca/menulis Sheets produksi.
    for key in ('APPS_SCRIPT_URL', 'SYNC_API_KEY', 'GOOGLE_SHEET_URL'):
        os.environ[key] = ''
else:
    load_dotenv(BASE_DIR / '.env')
IS_CLOUD = bool(os.getenv('VERCEL'))
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
if not DATABASE_URL:
    if IS_CLOUD:
        raise RuntimeError('DATABASE_URL PostgreSQL wajib diisi pada Vercel.')
    DATABASE_URL = f"sqlite:///{(BASE_DIR / 'tracking_stok.db').as_posix()}"
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql+psycopg://', 1)
elif DATABASE_URL.startswith('postgresql://'):
    DATABASE_URL = DATABASE_URL.replace('postgresql://', 'postgresql+psycopg://', 1)
IS_POSTGRES = DATABASE_URL.startswith('postgresql')
if IS_CLOUD and not IS_POSTGRES:
    raise RuntimeError('Vercel memerlukan PostgreSQL persisten, bukan SQLite lokal.')
pool_options = dict(poolclass=QueuePool, pool_size=2, max_overflow=3,
                    pool_timeout=15, pool_recycle=300, pool_use_lifo=True) if IS_POSTGRES else dict(poolclass=NullPool)
engine = create_engine(DATABASE_URL, pool_pre_ping=True, **pool_options,
                       connect_args={'connect_timeout':15, 'prepare_threshold':None} if IS_POSTGRES else {'check_same_thread':False, 'timeout':30})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()
PRIMARY_ADMIN = os.getenv('INITIAL_ADMIN_USERNAME', '').strip()

def now():
    return datetime.datetime.now(ZoneInfo('Asia/Jakarta')).replace(tzinfo=None)

def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600000).hex()
    return f'pbkdf2_sha256$600000${salt}${digest}'

def verify_password(password, stored):
    try:
        algorithm, iterations, salt, expected = stored.split('$')
        if algorithm != 'pbkdf2_sha256' or int(iterations) != 600000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (AttributeError, ValueError):
        return False

def lock_writes(session):
    if IS_POSTGRES:
        session.execute(text('SELECT pg_advisory_xact_lock(530051)'))
    else:
        session.execute(text('BEGIN IMMEDIATE'))

class Sarana(Base):
    __tablename__ = 'master_sarana'
    ID_Sarana = Column(String, primary_key=True)
    Nama_Sarana = Column(String)
    No_Sarana = Column(String)
    Vendor = Column(String, default='-')
    Status_Saat_Ini = Column(String, default='AT_API')

class LogScan(Base):
    __tablename__ = 'log_scan'
    id = Column(Integer, primary_key=True, autoincrement=True)
    ID_Sarana = Column(String)
    Alur_Scan = Column(String)
    Vendor = Column(String, default='-')
    Waktu_Scan = Column(DateTime, default=now)

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, index=True)
    password = Column(String)
    role = Column(String)
    nama_lengkap = Column(String)
    npk = Column(String)
    jabatan = Column(String)

class UnloadingGroup(Base):
    __tablename__ = 'unloading_groups'
    id = Column(Integer, primary_key=True, autoincrement=True)
    actor = Column(String, nullable=False, index=True)
    vendor = Column(String, nullable=False)
    part = Column(String, nullable=False)
    snapshot = Column(Text, nullable=False)
    state = Column(String, default='open', nullable=False, index=True)
    created_at = Column(DateTime, default=now)
    closed_at = Column(DateTime, nullable=True)

class AccountAudit(Base):
    __tablename__ = 'account_audit'
    id = Column(Integer, primary_key=True, autoincrement=True)
    actor = Column(String, nullable=False)
    target_username = Column(String, nullable=False)
    event = Column(String, nullable=False)
    created_at = Column(DateTime, default=now)

class VendorRecord(Base):
    __tablename__ = "vendors"

    name = Column(String(100), primary_key=True)
    created_at = Column(DateTime, default=now)

class SyncJob(Base):
    __tablename__ = 'sync_jobs'
    id = Column(Integer, primary_key=True, autoincrement=True)
    payload = Column(Text, nullable=False)
    state = Column(String, default='pending', nullable=False)
    error = Column(String, default='')
    created_at = Column(DateTime, default=now)
    sent_at = Column(DateTime, nullable=True)

class LoginAttempt(Base):
    __tablename__ = 'login_attempts'
    key = Column(String, primary_key=True)
    count = Column(Integer, default=0)
    window_start = Column(DateTime, default=now)

def init_db():
    # Lock PostgreSQL juga melindungi inisialisasi beberapa cold start sekaligus.
    with engine.begin() as connection:
        if IS_POSTGRES:
            connection.execute(text('SELECT pg_advisory_xact_lock(530050)'))
        Base.metadata.create_all(connection)
        with Session(bind=connection) as session:
            # Database awal harus baru; akun SQLite lama tidak diimpor diam-diam.
            if not session.query(User).first():
                password = os.getenv('INITIAL_ADMIN_PASSWORD', '')
                if not re.fullmatch(r'[A-Za-z0-9_.-]{3,50}', PRIMARY_ADMIN) or not 12 <= len(password) <= 256:
                    raise RuntimeError('Isi INITIAL_ADMIN_USERNAME dan INITIAL_ADMIN_PASSWORD minimal 12 karakter.')
                session.add(User(username=PRIMARY_ADMIN, password=hash_password(password), role='admin',
                                 nama_lengkap=os.getenv('INITIAL_ADMIN_NAME', 'Administrator'),
                                 npk='', jabatan='Administrator'))
                session.flush()
