from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import datetime

DATABASE_URL = "sqlite:///./tracking_stok.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Sarana(Base):
    __tablename__ = "master_sarana"
    ID_Sarana = Column(String, primary_key=True, index=True)
    Nama_Sarana = Column(String)
    No_Sarana = Column(String)
    Vendor = Column(String, default="-")
    Status_Saat_Ini = Column(String, default="AT_API")

class LogScan(Base):
    __tablename__ = "log_scan"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ID_Sarana = Column(String)
    Alur_Scan = Column(String)
    Vendor = Column(String, default="-")
    Waktu_Scan = Column(DateTime, default=datetime.datetime.now)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, index=True)
    password = Column(String)
    role = Column(String)
    nama_lengkap = Column(String)
    npk = Column(String)
    jabatan = Column(String)

def init_db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    if not session.query(User).filter_by(username="najib_rzq").first():
        session.add(User(
            username="najib_rzq", 
            password="123",
            role="admin", 
            nama_lengkap="Najib Razzaq", 
            npk="P344", 
            jabatan="MagangHub"
        ))
        session.commit()
    session.close()