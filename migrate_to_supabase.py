"""Impor CSV backup PostgreSQL/Neon ke Supabase.

Script ini sengaja tidak menyimpan URL/password. Isi koneksi lewat
environment `SUPABASE_DATABASE_URL` atau file `.env.migration`.

Contoh:
    py -3.12 migrate_to_supabase.py --source-dir .\\backup_csv --dry-run
    py -3.12 migrate_to_supabase.py --source-dir .\\backup_csv --replace --force --yes
"""
from __future__ import annotations

import argparse
import ast
import csv
import datetime
import io
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
WIB = ZoneInfo("Asia/Jakarta")

STATUSES = {
    "AT_API",
    "AT_VENDOR",
    "AT_CUSTOMER",
    "IN_TRANSIT_OUT",
    "IN_TRANSIT_IN",
    "LOST_IN_TRANSIT",
}
ROLES = {"admin", "member", "guest"}

IMPORT_ORDER = [
    "vendors",
    "customers",
    "users",
    "master_sarana",
    "log_scan",
    "account_audit",
    "unloading_groups",
    "sync_jobs",
    "deleted_vendors",
    "login_attempts",
]

PRIMARY_KEYS = {
    "vendors": "name",
    "customers": "name",
    "users": "username",
    "master_sarana": "ID_Sarana",
    "log_scan": "id",
    "account_audit": "id",
    "unloading_groups": "id",
    "sync_jobs": "id",
    "deleted_vendors": "name",
    "login_attempts": "key",
}

SEQUENCE_TABLES = {
    "users",
    "log_scan",
    "account_audit",
    "unloading_groups",
    "sync_jobs",
}

TABLE_HINTS = {
    "vendors": ("vendors", "vendor"),
    "customers": ("customers", "customer"),
    "users": ("users", "user"),
    "master_sarana": ("master_sarana", "sarana", "database"),
    "log_scan": ("log_scan", "logscan", "log"),
    "account_audit": ("account_audit", "audit"),
    "unloading_groups": ("unloading_groups", "unloading"),
    "sync_jobs": ("sync_jobs", "syncjob"),
    "deleted_vendors": ("deleted_vendors", "deletedvendor"),
    "login_attempts": ("login_attempts", "loginattempt"),
}


class MigrationError(RuntimeError):
    pass


def norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_int(value: Any, default: int | None = None) -> int | None:
    text = clean(value)
    if not text:
        return default
    try:
        return int(float(text))
    except (TypeError, ValueError):
        raise ValueError(f"angka tidak valid: {text!r}") from None


def parse_id(value: Any) -> int | None:
    number = parse_int(value)
    if number is not None and number <= 0:
        raise ValueError("ID harus lebih besar dari 0")
    return number


def parse_datetime(value: Any) -> datetime.datetime | None:
    text = clean(value)
    if not text:
        return None

    text = text.replace("Z", "+00:00")
    parsed = None

    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y",
        ):
            try:
                parsed = datetime.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        raise ValueError(f"tanggal tidak valid: {text!r}")

    if parsed.tzinfo:
        parsed = parsed.astimezone(WIB).replace(tzinfo=None)

    return parsed


def require_text(
    row: dict[str, Any],
    key: str,
    label: str,
    max_length: int | None = None,
) -> str:
    value = clean(row.get(key))
    if not value:
        raise ValueError(f"{label} wajib diisi")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{label} maksimal {max_length} karakter")
    return value


def parse_scan_permissions(value: Any) -> str:
    text = clean(value)
    if not text:
        return "[]"

    try:
        parsed = json.loads(text)
    except ValueError:
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            raise ValueError("scan_permissions bukan JSON/list yang valid") from None

    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise ValueError("scan_permissions harus berupa daftar string")

    return json.dumps(parsed, ensure_ascii=False)


def normalize_master(row: dict[str, Any]) -> dict[str, Any]:
    id_sarana = require_text(row, "id_sarana", "ID_Sarana", 100)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", id_sarana):
        raise ValueError(f"ID_Sarana tidak valid: {id_sarana!r}")

    name = require_text(row, "nama_sarana", "Nama_Sarana", 150)
    status = require_text(row, "status_saat_ini", "Status_Saat_Ini")

    if status not in STATUSES and not re.fullmatch(r"DELETED_NG \(.+\)", status):
        raise ValueError(f"status tidak valid: {status!r}")

    vendor = clean(row.get("vendor"))
    customer = clean(row.get("customer"))
    partner = clean(row.get("vendor_customer"))

    if partner:
        if status == "AT_CUSTOMER":
            customer = customer or partner
            vendor = ""
        elif status == "AT_API":
            vendor = ""
            customer = ""
        elif status == "LOST_IN_TRANSIT":
            # Jangan menimpa sisi customer yang sudah ada. Vendor_Customer
            # hanya dipakai bila kedua kolom sisi belum terisi.
            if not vendor and not customer:
                vendor = partner
        elif status.startswith("DELETED"):
            # Unit NG bisa berada di sisi vendor atau customer; pertahankan
            # sisi yang sudah ada dan pakai partner hanya sebagai fallback.
            if not vendor and not customer:
                vendor = partner
        else:
            vendor = vendor or partner
            customer = ""

    if status == "AT_API":
        if vendor or customer:
            raise ValueError("status AT_API tidak boleh memiliki vendor/customer")
    elif status == "AT_CUSTOMER":
        vendor = ""
        if not customer:
            raise ValueError("status AT_CUSTOMER wajib memiliki Customer")
    elif status == "LOST_IN_TRANSIT":
        # Unit hilang bisa di sisi vendor (Vendor terisi) atau customer
        # (Customer terisi). Jangan hapus sisi customer.
        if not vendor and not customer:
            if partner:
                vendor = partner
            else:
                raise ValueError(
                    "status LOST_IN_TRANSIT wajib memiliki Vendor atau Customer"
                )
    elif not status.startswith("DELETED"):
        customer = ""
        if not vendor:
            raise ValueError("status selain AT_API/AT_CUSTOMER wajib memiliki Vendor")

    if len(vendor) > 100 or len(customer) > 100:
        raise ValueError("nama vendor/customer maksimal 100 karakter")

    return {
        "ID_Sarana": id_sarana,
        "Nama_Sarana": name,
        "No_Sarana": clean(row.get("no_sarana")),
        "Vendor": vendor,
        "Customer": customer,
        "Status_Saat_Ini": status,
        "Last_Scan_At": parse_datetime(
            row.get("last_scan_at") or row.get("scan_terakhir")
        ),
        "API_Received_At": parse_datetime(row.get("api_received_at")),
    }


def normalize_log(row: dict[str, Any]) -> dict[str, Any]:
    id_sarana = require_text(row, "id_sarana", "ID_Sarana", 100)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", id_sarana):
        raise ValueError(f"ID_Sarana tidak valid: {id_sarana!r}")

    event = require_text(row, "alur_scan", "Alur_Scan")
    scan_time = parse_datetime(row.get("waktu_scan"))
    if scan_time is None:
        raise ValueError("Waktu_Scan wajib diisi")

    return {
        "id": parse_id(row.get("id")),
        "ID_Sarana": id_sarana,
        "Alur_Scan": event,
        "Vendor": clean(row.get("vendor") or row.get("vendor_customer")) or "-",
        "Waktu_Scan": scan_time,
        "Customer": clean(row.get("customer")),
    }


def normalize_user(row: dict[str, Any]) -> dict[str, Any]:
    username = require_text(row, "username", "username", 50)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,50}", username):
        raise ValueError(f"username tidak valid: {username!r}")

    password = require_text(row, "password", "password", 512)
    role = require_text(row, "role", "role").lower()
    if role not in ROLES:
        raise ValueError(f"role tidak valid: {role!r}")

    return {
        "id": parse_id(row.get("id")),
        "username": username,
        "password": password,
        "password_view": clean(row.get("password_view")),
        "role": role,
        "nama_lengkap": clean(row.get("nama_lengkap")) or username,
        "npk": clean(row.get("npk")),
        "jabatan": clean(row.get("jabatan")),
        "scan_permissions": parse_scan_permissions(row.get("scan_permissions")),
        "vendor_scope": clean(row.get("vendor_scope")),
    }


def normalize_customer(row: dict[str, Any]) -> dict[str, Any]:
    name = require_text(row, "name", "name", 100)
    active = parse_int(row.get("active"), 1)
    if active not in (0, 1):
        raise ValueError("active harus 0 atau 1")
    return {
        "name": name,
        "active": active,
        "created_at": parse_datetime(row.get("created_at")) or datetime.datetime.now(WIB).replace(tzinfo=None),
    }


def normalize_vendor(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": require_text(row, "name", "name", 100),
        "created_at": parse_datetime(row.get("created_at")) or datetime.datetime.now(WIB).replace(tzinfo=None),
    }


def normalize_deleted_vendor(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": require_text(row, "name", "name", 100),
        "deleted_at": parse_datetime(row.get("deleted_at")) or datetime.datetime.now(WIB).replace(tzinfo=None),
    }


def normalize_audit(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": parse_id(row.get("id")),
        "actor": require_text(row, "actor", "actor"),
        "target_username": require_text(row, "target_username", "target_username"),
        "event": require_text(row, "event", "event"),
        "created_at": parse_datetime(row.get("created_at")) or datetime.datetime.now(WIB).replace(tzinfo=None),
    }


def normalize_unloading(row: dict[str, Any]) -> dict[str, Any]:
    kind = clean(row.get("kind")).lower() or "vendor"
    if kind not in {"vendor", "api", "customer"}:
        raise ValueError(f"kind unloading tidak valid: {kind!r}")

    vendor = clean(row.get("vendor"))
    customer = clean(row.get("customer"))
    if kind == "customer":
        if not customer:
            raise ValueError("unloading customer wajib memiliki customer")
    elif not vendor:
        raise ValueError("unloading vendor/api wajib memiliki vendor")

    return {
        "id": parse_id(row.get("id")),
        "kind": kind,
        "actor": require_text(row, "actor", "actor"),
        "vendor": vendor,
        "customer": customer,
        "part": require_text(row, "part", "part"),
        "snapshot": require_text(row, "snapshot", "snapshot"),
        "state": clean(row.get("state")) or "open",
        "created_at": parse_datetime(row.get("created_at")) or datetime.datetime.now(WIB).replace(tzinfo=None),
        "closed_at": parse_datetime(row.get("closed_at")),
    }


def normalize_sync_job(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": parse_id(row.get("id")),
        "payload": require_text(row, "payload", "payload"),
        "state": clean(row.get("state")) or "pending",
        "error": clean(row.get("error")),
        "created_at": parse_datetime(row.get("created_at")) or datetime.datetime.now(WIB).replace(tzinfo=None),
        "sent_at": parse_datetime(row.get("sent_at")),
    }


def normalize_login_attempt(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": require_text(row, "key", "key"),
        "count": parse_int(row.get("count"), 0),
        "window_start": parse_datetime(row.get("window_start")) or datetime.datetime.now(WIB).replace(tzinfo=None),
    }


NORMALIZERS = {
    "master_sarana": normalize_master,
    "log_scan": normalize_log,
    "users": normalize_user,
    "customers": normalize_customer,
    "vendors": normalize_vendor,
    "account_audit": normalize_audit,
    "unloading_groups": normalize_unloading,
    "sync_jobs": normalize_sync_job,
    "deleted_vendors": normalize_deleted_vendor,
    "login_attempts": normalize_login_attempt,
}


def detect_table(path: Path, headers: set[str]) -> str | None:
    # Header yang kuat lebih dipercaya daripada nama file.
    if {"id_sarana", "nama_sarana", "status_saat_ini"} <= headers:
        return "master_sarana"
    if {"alur_scan", "waktu_scan"} <= headers:
        return "log_scan"
    if {"username", "password", "role"} <= headers:
        return "users"
    if {"target_username", "event", "actor"} <= headers:
        return "account_audit"
    if {"payload", "state"} <= headers:
        return "sync_jobs"
    if {"actor", "vendor", "part", "snapshot"} <= headers:
        return "unloading_groups"
    if {"key", "count", "window_start"} <= headers:
        return "login_attempts"
    if {"name", "active"} <= headers:
        return "customers"
    if {"name", "deleted_at"} <= headers:
        return "deleted_vendors"
    if {"name", "created_at"} <= headers:
        return "vendors"

    stem = norm_key(path.stem)
    for table, hints in TABLE_HINTS.items():
        if any(hint in stem for hint in hints):
            return table
    return None


def read_csv(path: Path) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise MigrationError(f"Encoding tidak dikenali: {path}")

    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    raw_headers = reader.fieldnames or []
    headers = [norm_key(header) for header in raw_headers]
    if not headers or any(not header for header in headers):
        raise MigrationError(f"Header CSV tidak valid: {path}")
    if len(set(headers)) != len(headers):
        raise MigrationError(f"Header CSV duplikat: {path}")

    rows: list[tuple[int, dict[str, str]]] = []
    for line_number, raw_row in enumerate(reader, start=2):
        row = {
            norm_key(key): value
            for key, value in raw_row.items()
            if key is not None
        }
        if any(clean(value) for value in row.values()):
            rows.append((line_number, row))

    return headers, rows


def parse_files(paths: list[Path], ignore_unknown: bool) -> dict[str, list[dict[str, Any]]]:
    parsed: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, Path] = {}

    for path in paths:
        headers, rows = read_csv(path)
        table = detect_table(path, set(headers))
        if table is None:
            if ignore_unknown:
                print(f"LEWATI  {path.name}: jenis tabel tidak dikenali")
                continue
            raise MigrationError(
                f"Tidak bisa mengenali tabel untuk {path.name}. "
                "Gunakan nama file seperti users.csv, master_sarana.csv, "
                "log_scan.csv, customers.csv, vendors.csv, account_audit.csv, "
                "atau tambahkan --ignore-unknown."
            )
        if table in parsed:
            raise MigrationError(
                f"Tabel {table} muncul lebih dari sekali: "
                f"{sources[table].name} dan {path.name}"
            )

        normalizer = NORMALIZERS[table]
        normalized_rows: list[dict[str, Any]] = []
        seen: set[Any] = set()

        for line_number, row in rows:
            try:
                normalized = normalizer(row)
            except Exception as error:
                raise MigrationError(
                    f"{path.name}:{line_number}: {error}"
                ) from None

            primary_key = PRIMARY_KEYS[table]
            key_value = normalized.get(primary_key)
            if key_value is not None:
                if key_value in seen:
                    raise MigrationError(
                        f"{path.name}:{line_number}: "
                        f"{primary_key} duplikat: {key_value!r}"
                    )
                seen.add(key_value)

            normalized_rows.append(normalized)

        parsed[table] = normalized_rows
        sources[table] = path
        print(f"BACA     {table:18} {len(normalized_rows):>8} baris  ({path.name})")

    return parsed


def load_database(url: str):
    if not url:
        raise MigrationError(
            "URL Supabase kosong. Isi SUPABASE_DATABASE_URL "
            "atau gunakan --database-url."
        )

    os.environ["DATABASE_URL"] = url
    os.environ.pop("LOCAL_TRIAL", None)

    import database  # noqa: PLC0415

    if database.LOCAL_TRIAL:
        raise MigrationError("LOCAL_TRIAL aktif; migrasi dibatalkan.")
    return database


def create_schema(database) -> None:
    with database.engine.begin() as connection:
        if database.IS_POSTGRES:
            connection.execute(database.text("SELECT pg_advisory_xact_lock(530050)"))
        database.Base.metadata.create_all(connection)
        database.migrate_tracking_v2(connection)


def model_map(database) -> dict[str, Any]:
    return {
        "vendors": database.VendorRecord,
        "customers": database.CustomerRecord,
        "users": database.User,
        "master_sarana": database.Sarana,
        "log_scan": database.LogScan,
        "account_audit": database.AccountAudit,
        "unloading_groups": database.UnloadingGroup,
        "sync_jobs": database.SyncJob,
        "deleted_vendors": database.DeletedVendor,
        "login_attempts": database.LoginAttempt,
    }


def derive_partners(session, database, parsed: dict[str, list[dict[str, Any]]]) -> None:
    vendor_names: set[str] = set()
    customer_names: set[str] = set()

    for row in parsed.get("master_sarana", []):
        if row["Vendor"]:
            vendor_names.add(row["Vendor"])
        if row["Customer"]:
            customer_names.add(row["Customer"])

    for row in parsed.get("log_scan", []):
        # Log alur customer juga mengisi kolom Vendor dengan nama customer.
        # Jangan sampai customer ikut dibuat sebagai vendor.
        event = row["Alur_Scan"].lower()
        if (
            row["Vendor"]
            and row["Vendor"] != "-"
            and "customer" not in event
        ):
            vendor_names.add(row["Vendor"])

    # Kalau master tidak ikut diimpor, tetap lengkapi dari data yang sudah ada.
    if "master_sarana" not in parsed:
        for unit in session.query(database.Sarana).all():
            if unit.Vendor:
                vendor_names.add(unit.Vendor)
            if unit.Customer:
                customer_names.add(unit.Customer)

    for name in sorted(vendor_names):
        if not session.get(database.VendorRecord, name):
            session.add(database.VendorRecord(name=name))

    for name in sorted(customer_names):
        if not session.get(database.CustomerRecord, name):
            session.add(database.CustomerRecord(name=name, active=1))

    session.flush()


def validate_relations(session, database, parsed, allow_orphan_logs: bool) -> None:
    if "master_sarana" in parsed:
        customer_names = {
            customer.name
            for customer in session.query(database.CustomerRecord).all()
        }
        for row in parsed["master_sarana"]:
            if (
                row["Status_Saat_Ini"] == "AT_CUSTOMER"
                and row["Customer"] not in customer_names
            ):
                raise MigrationError(
                    "Customer belum terdaftar untuk unit "
                    f"{row['ID_Sarana']}: {row['Customer']}"
                )

    if "master_sarana" in parsed and "log_scan" in parsed:
        master_ids = {
            row["ID_Sarana"]
            for row in parsed["master_sarana"]
        }
        orphans = [
            row["ID_Sarana"]
            for row in parsed["log_scan"]
            if row["ID_Sarana"] not in master_ids
        ]
        if orphans and not allow_orphan_logs:
            sample = ", ".join(sorted(set(orphans))[:5])
            raise MigrationError(
                f"{len(orphans)} log menunjuk ID yang tidak ada di master: {sample}. "
                "Gunakan --allow-orphan-logs bila ini memang disengaja."
            )


def restart_sequence(session, table: str, column: str = "id") -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        max_id = session.execute(
            database_text(f'SELECT COALESCE(MAX("{column}"), 1) FROM "{table}"')
        ).scalar()
        has_rows = session.execute(
            database_text(f'SELECT COUNT(*) FROM "{table}"')
        ).scalar() > 0
        session.execute(
            database_text(
                "SELECT setval("
                "pg_get_serial_sequence(:table, :column), "
                ":value, :called)"
            ),
            {"table": table, "column": column, "value": max_id, "called": has_rows},
        )
    elif dialect == "sqlite":
        has_sequence = session.execute(
            database_text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='sqlite_sequence'"
            )
        ).scalar()
        if has_sequence:
            session.execute(
                database_text(
                    "UPDATE sqlite_sequence "
                    "SET seq = COALESCE((SELECT MAX(id) FROM \"" + table + "\"), 0) "
                    "WHERE name=:table"
                ),
                {"table": table},
            )


def database_text(statement: str):
    from sqlalchemy import text  # noqa: PLC0415

    return text(statement)


def insert_rows(session, model, rows: list[dict[str, Any]]) -> None:
    """Insert per batch; omit id=None so PostgreSQL/SQLite can autoincrement."""
    for start in range(0, len(rows), 500):
        chunk = []
        for row in rows[start:start + 500]:
            item = dict(row)
            if item.get("id") is None:
                item.pop("id", None)
            chunk.append(item)
        session.bulk_insert_mappings(model, chunk)
        session.flush()


def import_rows(
    database,
    parsed,
    replace: bool,
    force: bool,
    derive: bool,
    allow_orphan_logs: bool,
) -> None:
    models = model_map(database)
    tables = [table for table in IMPORT_ORDER if table in parsed]

    with database.SessionLocal() as session:
        database.lock_writes(session)

        if replace:
            for table in reversed(tables):
                count = session.query(models[table]).count()
                if count and not force:
                    raise MigrationError(
                        f"Tabel {table} sudah berisi {count} baris. "
                        "Gunakan --force hanya jika yakin data lama boleh diganti."
                    )

            for table in reversed(tables):
                session.query(models[table]).delete(synchronize_session=False)
            session.flush()

        # Vendor/customer harus masuk lebih dulu agar master AT_CUSTOMER
        # punya referensi yang valid.
        for table in ("vendors", "customers"):
            if table not in parsed:
                continue
            rows = parsed[table]
            insert_rows(session, models[table], rows)
            print(f"IMPOR    {table:18} {len(rows):>8} baris")

        if derive:
            derive_partners(session, database, parsed)
        validate_relations(
            session,
            database,
            parsed,
            allow_orphan_logs=allow_orphan_logs,
        )

        for table in tables:
            if table in ("vendors", "customers"):
                continue
            rows = parsed[table]
            insert_rows(session, models[table], rows)
            print(f"IMPOR    {table:18} {len(rows):>8} baris")

        session.commit()

    with database.SessionLocal() as session:
        for table in SEQUENCE_TABLES:
            if table in parsed:
                restart_sequence(session, table)
        session.commit()


def verify(database, parsed, replace: bool) -> None:
    models = model_map(database)
    # Vendor/customer boleh bertambah karena script melengkapi pasangan
    # yang ditemukan di master/log.
    allow_extra = {"vendors", "customers"}
    with database.SessionLocal() as session:
        print("\nVERIFIKASI")
        for table in IMPORT_ORDER:
            if table not in parsed:
                continue
            count = session.query(models[table]).count()
            expected = len(parsed[table])
            valid = count == expected or (
                table in allow_extra and count >= expected
            )
            marker = "OK" if valid else "PERIKSA"
            print(f"  {table:18} database={count:>8} csv={expected:>8}  {marker}")
            if replace and not valid:
                raise MigrationError(
                    f"Jumlah {table} tidak cocok: database {count}, CSV {expected}"
                )

        if "master_sarana" in parsed:
            statuses = Counter(
                row["Status_Saat_Ini"]
                for row in parsed["master_sarana"]
            )
            print("  status master:")
            for status, count in sorted(statuses.items()):
                print(f"    {status:24} {count:>8}")

        if "log_scan" in parsed:
            max_id = session.execute(
                database_text('SELECT COALESCE(MAX(id), 0) FROM log_scan')
            ).scalar()
            print(f"  max log_scan.id: {max_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Impor CSV backup PostgreSQL/Neon ke Supabase."
    )
    parser.add_argument(
        "csv",
        nargs="*",
        type=Path,
        help="File CSV tertentu; kosongkan untuk memakai --source-dir.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=ROOT / "backup_csv",
        help="Folder berisi CSV backup (default: ./backup_csv).",
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="URL Supabase; utamakan env SUPABASE_DATABASE_URL.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / ".env.migration",
        help="File env lokal untuk SUPABASE_DATABASE_URL (default: .env.migration).",
    )
    parser.add_argument("--replace", action="store_true", help="Kosongkan tabel target sebelum impor.")
    parser.add_argument("--force", action="store_true", help="Izinkan --replace pada tabel yang sudah berisi.")
    parser.add_argument("--dry-run", action="store_true", help="Validasi CSV tanpa menulis database.")
    parser.add_argument("--ignore-unknown", action="store_true", help="Lewati CSV yang jenis tabelnya tidak dikenali.")
    parser.add_argument("--allow-orphan-logs", action="store_true", help="Izinkan log tanpa master.")
    parser.add_argument("--no-derive-partners", action="store_true", help="Jangan buat vendor/customer otomatis dari master/log.")
    parser.add_argument("--allow-sqlite", action="store_true", help="Hanya untuk uji lokal; jangan dipakai ke produksi.")
    parser.add_argument("--yes", action="store_true", help="Konfirmasi tulis ke database.")
    args = parser.parse_args(argv)

    if args.env_file.exists():
        load_dotenv(args.env_file, override=False)

    database_url = (
        args.database_url
        or os.getenv("SUPABASE_DATABASE_URL", "").strip()
        or os.getenv("MIGRATION_DATABASE_URL", "").strip()
    )

    paths = list(args.csv)
    if not paths:
        if not args.source_dir.exists():
            raise MigrationError(f"Folder CSV tidak ditemukan: {args.source_dir}")
        paths = sorted(args.source_dir.glob("*.csv"))
    if not paths:
        raise MigrationError("Tidak ada file CSV untuk diimpor.")

    print("SUMBER CSV")
    for path in paths:
        print(f"  {path}")

    parsed = parse_files(paths, args.ignore_unknown)
    if not parsed:
        raise MigrationError("Tidak ada CSV yang dikenali.")

    if args.dry_run:
        print("\nDRY RUN: CSV valid; database tidak diubah.")
        return 0

    if not database_url:
        raise MigrationError(
            "URL Supabase belum diisi. Buat .env.migration berisi "
            "SUPABASE_DATABASE_URL=... atau gunakan --database-url."
        )

    if not args.yes:
        raise MigrationError(
            "Tambahkan --yes setelah memastikan target Supabase benar. "
            "Gunakan --dry-run terlebih dahulu."
        )

    database = load_database(database_url)
    if not database.IS_POSTGRES and not args.allow_sqlite:
        raise MigrationError(
            "Target migrasi harus PostgreSQL/Supabase, bukan SQLite. "
            "Gunakan --allow-sqlite hanya untuk uji lokal."
        )

    print("\nTARGET DATABASE")
    print(f"  {database.engine.url.render_as_string(hide_password=True)}")

    create_schema(database)
    import_rows(
        database,
        parsed,
        replace=args.replace,
        force=args.force,
        derive=not args.no_derive_partners,
        allow_orphan_logs=args.allow_orphan_logs,
    )
    verify(database, parsed, replace=args.replace)
    print("\nSELESAI. Jalankan aplikasi dan cek /api/health.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as error:
        print(f"\nGAGAL: {error}", file=sys.stderr)
        raise SystemExit(1)

