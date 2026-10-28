#!/usr/bin/env python3
"""Sejoli CRM & Voucher — Sistem CRM dan Voucher khusus Sejoli.

Alamat baru: https://sejoli.grok.me
Data Sejoli terpisah total dari Sarae.
QR pendaftaran lama diarahkan (redirect) ke alamat baru /daftar.

Fitur:
- CRM Member Sejoli (Nama, NIM, HP, Email, Status, Poin, Catatan)
- Manajemen Voucher (buat, assign, redeem)
- Pendaftaran publik via QR /daftar
- Dashboard statistik
- Export CSV
- Sistem Donasi dengan kode unik dan aturan target

Jalankan: python3 jalankan.py
Akses admin: http://localhost:8770
Pendaftaran: http://localhost:8770/daftar
"""

from __future__ import annotations

import hashlib
import secrets
import csv
import io
import json
import os
import sqlite3
from http.cookies import SimpleCookie
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"
DB_PATH = DATA / "sejolicrm.db"

DATA.mkdir(exist_ok=True)
WEB.mkdir(exist_ok=True)

HOST = "0.0.0.0"
PORT = int(os.environ.get("SEJOLI_PORT", "8770"))
BASE_URL = os.environ.get("SEJOLI_BASE_URL", "https://sejoli.grok.me")

ADMIN_USER = os.environ.get("SEJOLI_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("SEJOLI_ADMIN_PASS", "sejoli123")
ADMIN_PASS_HASH = hashlib.sha256(ADMIN_PASS.encode()).hexdigest()

SESSIONS = {}

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nama TEXT NOT NULL,
            nim TEXT UNIQUE NOT NULL,
            email TEXT,
            hp TEXT,
            status TEXT DEFAULT 'aktif',
            poin INTEGER DEFAULT 0,
            notes TEXT,
            registered_at TEXT,
            source TEXT DEFAULT 'sejoli'
        );

        CREATE TABLE IF NOT EXISTS vouchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            nama TEXT NOT NULL,
            tipe TEXT DEFAULT 'diskon_nominal',
            nilai REAL DEFAULT 0,
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            valid_from TEXT,
            valid_until TEXT,
            active INTEGER DEFAULT 1,
            keterangan TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_id INTEGER,
            member_id INTEGER,
            redeemed_at TEXT,
            nilai_terpakai REAL,
            keterangan TEXT,
            FOREIGN KEY(voucher_id) REFERENCES vouchers(id),
            FOREIGN KEY(member_id) REFERENCES members(id)
        );

        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER,
            jenis TEXT,
            deskripsi TEXT,
            created_at TEXT,
            FOREIGN KEY(member_id) REFERENCES members(id)
        );

        CREATE TABLE IF NOT EXISTS donations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kode TEXT UNIQUE NOT NULL,
            donor_nama TEXT NOT NULL,
            donor_wa TEXT NOT NULL,
            porsi INTEGER DEFAULT 1,
            nilai_per_porsi INTEGER DEFAULT 20001,
            total INTEGER,
            tipe TEXT DEFAULT 'umum',
            target_jurusan TEXT,
            target_nim TEXT,
            target_nama TEXT,
            pesan TEXT,
            used_count INTEGER DEFAULT 0,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS donation_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            donation_id INTEGER,
            member_nim TEXT,
            member_nama TEXT,
            used_at TEXT,
            FOREIGN KEY(donation_id) REFERENCES donations(id)
        );
        """)
        count = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
        if count == 0:
            samples = [
                ("Budi Santoso", "13522001", "budi@itb.ac.id", "081234567890", "aktif", 50, "Member aktif Sejoli", now_iso()),
                ("Siti Aminah", "13522002", "siti@itb.ac.id", "081298765432", "aktif", 80, "Sering redeem voucher", now_iso()),
                ("Andi Wijaya", "13522003", "andi@itb.ac.id", "081112223334", "aktif", 20, "", now_iso()),
            ]
            for s in samples:
                conn.execute(
                    """INSERT INTO members (nama, nim, email, hp, status, poin, notes, registered_at, source)
                       VALUES (?,?,?,?,?,?,?,?,'sejoli')""",
                    s
                )
            conn.execute(
                """INSERT INTO vouchers (code, nama, tipe, nilai, max_uses, valid_from, valid_until, active, keterangan, created_at)
                   VALUES (?,?,?,?,?,?,?,1,?,?)""",
                ("SEJOLI10", "Diskon 10rb Sejoli", "diskon_nominal", 10000, 100, today(), 
                 (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"), "Voucher welcome", now_iso())
            )
            conn.commit()
        print(f"Database initialized at {DB_PATH}")

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def get_session_user(self):
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        if "sejoli_session" in cookie:
            token = cookie["sejoli_session"].value
            return SESSIONS.get(token)
        return None

    def require_admin(self):
        user = self.get_session_user()
        if user != ADMIN_USER:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"Login diperlukan"}')
            return False
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/daftar" or path == "/daftar/" or path == "/login" or path == "/login/":
            if path.startswith("/login"):
                self.path = "/login.html"
            else:
                self.path = "/daftar.html"
            return super().do_GET()

        if path == "/gab" or path == "/gab/":
            self.send_response(301)
            self.send_header("Location", "/daftar")
            self.end_headers()
            return

        if path.startswith("/api/"):
            if path in ("/api/register", "/api/login", "/api/qr-info"):
                self.handle_api_get(path, parsed.query)
                return
            if not self.require_admin():
                return
            self.handle_api_get(path, parsed.query)
            return

        if not self.get_session_user():
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return

        if path == "/" or path == "/admin":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        if parsed.path in ("/api/register", "/api/login", "/api/donations/check", "/api/donations"):
            self.handle_api_post(parsed.path, data)
            return

        if not self.require_admin():
            return
        self.handle_api_post(parsed.path, data)

    def handle_api_get(self, path, query):
        qs = parse_qs(query)
        if path == "/api/members":
            self.api_members_list(qs)
        elif path == "/api/vouchers":
            self.api_vouchers_list()
        elif path == "/api/donations":
            self.api_donations_list()
        elif path == "/api/stats":
            self.api_stats()
        elif path == "/api/export/members":
            self.api_export_members()
        elif path == "/api/qr-info":
            self.api_qr_info()
        else:
            self.send_error(404)

    def handle_api_post(self, path, data):
        if path == "/api/members":
            self.api_members_save(data)
        elif path == "/api/members/adjust-poin":
            self.api_adjust_poin(data)
        elif path == "/api/vouchers":
            self.api_vouchers_save(data)
        elif path == "/api/redeem":
            self.api_redeem(data)
        elif path == "/api/register":
            self.api_register(data)
        elif path == "/api/login":
            self.api_login(data)
        elif path == "/api/logout":
            self.api_logout()
        elif path == "/api/donations":
            self.api_donation_create(data)
        elif path == "/api/donations/check":
            self.api_donation_check(data)
        elif path == "/api/donations/use":
            self.api_donation_use(data)
        elif path == "/api/activity":
            self.api_activity_add(data)
        else:
            self.send_error(404)

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def api_members_list(self, qs):
        search = qs.get("q", [""])[0]
        with get_db() as conn:
            if search:
                rows = conn.execute(
                    """SELECT * FROM members WHERE nama LIKE ? OR nim LIKE ? OR hp LIKE ? OR email LIKE ?
                       ORDER BY registered_at DESC""",
                    (f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%")
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM members ORDER BY registered_at DESC").fetchall()
            self.send_json([dict(r) for r in rows])

    def api_members_save(self, data):
        with get_db() as conn:
            if data.get("id"):
                conn.execute(
                    """UPDATE members SET nama=?, nim=?, email=?, hp=?, status=?, notes=? WHERE id=?""",
                    (data["nama"], data["nim"], data.get("email"), data.get("hp"),
                     data.get("status", "aktif"), data.get("notes"), data["id"])
                )
            else:
                try:
                    conn.execute(
                        """INSERT INTO members (nama, nim, email, hp, status, poin, notes, registered_at, source)
                           VALUES (?,?,?,?,?,?,?,?, 'sejoli')""",
                        (data["nama"], data["nim"], data.get("email"), data.get("hp"),
                         data.get("status", "aktif"), data.get("poin", 0), data.get("notes"), now_iso())
                    )
                except sqlite3.IntegrityError:
                    self.send_json({"ok": False, "error": "NIM sudah terdaftar"}, 400)
                    return
            conn.commit()
        self.send_json({"ok": True})

    def api_adjust_poin(self, data):
        with get_db() as conn:
            conn.execute("UPDATE members SET poin = ? WHERE id = ?", (data["poin"], data["id"]))
            conn.execute(
                "INSERT INTO activities (member_id, jenis, deskripsi, created_at) VALUES (?,?,?,?)",
                (data["id"], "adjust", f"Poin diubah menjadi {data['poin']}", now_iso())
            )
            conn.commit()
        self.send_json({"ok": True})

    def api_vouchers_list(self):
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM vouchers ORDER BY created_at DESC").fetchall()
            self.send_json([dict(r) for r in rows])

    def api_vouchers_save(self, data):
        with get_db() as conn:
            if data.get("id"):
                conn.execute(
                    """UPDATE vouchers SET code=?, nama=?, tipe=?, nilai=?, max_uses=?, valid_from=?, valid_until=?, active=?, keterangan=? WHERE id=?""",
                    (data["code"], data["nama"], data.get("tipe", "diskon_nominal"), data.get("nilai", 0),
                     data.get("max_uses", 1), data.get("valid_from"), data.get("valid_until"),
                     1 if data.get("active", True) else 0, data.get("keterangan"), data["id"])
                )
            else:
                conn.execute(
                    """INSERT INTO vouchers (code, nama, tipe, nilai, max_uses, valid_from, valid_until, active, keterangan, created_at)
                       VALUES (?,?,?,?,?,?,?,1,?,?)""",
                    (data["code"], data["nama"], data.get("tipe", "diskon_nominal"), data.get("nilai", 0),
                     data.get("max_uses", 1), data.get("valid_from"), data.get("valid_until"),
                     data.get("keterangan"), now_iso())
                )
            conn.commit()
        self.send_json({"ok": True})

    def api_register(self, data):
        nama = (data.get("nama") or "").strip()
        nim = (data.get("nim") or "").strip()
        if not nama or not nim:
            self.send_json({"ok": False, "error": "Nama dan NIM wajib"}, 400)
            return
        with get_db() as conn:
            try:
                conn.execute(
                    """INSERT INTO members (nama, nim, email, hp, status, poin, notes, registered_at, source)
                       VALUES (?,?,?,?, 'aktif', 10, 'Pendaftaran via QR/form', ?, 'sejoli')""",
                    (nama, nim, data.get("email"), data.get("hp"), now_iso())
                )
                mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO activities (member_id, jenis, deskripsi, created_at) VALUES (?,?,?,?)",
                    (mid, "register", "Pendaftaran baru via form/QR", now_iso())
                )
                conn.commit()
            except sqlite3.IntegrityError:
                self.send_json({"ok": False, "error": "NIM sudah terdaftar"}, 400)
                return
        self.send_json({"ok": True, "message": "Pendaftaran berhasil! Selamat datang di Sejoli."})

    def api_login(self, data):
        user = data.get("username", "")
        pwd = data.get("password", "")
        if user == ADMIN_USER and hashlib.sha256(pwd.encode()).hexdigest() == ADMIN_PASS_HASH:
            token = secrets.token_hex(16)
            SESSIONS[token] = user
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", f"sejoli_session={token}; Path=/; HttpOnly")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        self.send_json({"ok": False, "error": "Login gagal"}, 401)

    def api_logout(self):
        cookie_header = self.headers.get("Cookie", "")
        if cookie_header:
            cookie = SimpleCookie()
            cookie.load(cookie_header)
            if "sejoli_session" in cookie:
                token = cookie["sejoli_session"].value
                SESSIONS.pop(token, None)
        self.send_json({"ok": True})

    def api_activity_add(self, data):
        with get_db() as conn:
            conn.execute(
                "INSERT INTO activities (member_id, jenis, deskripsi, created_at) VALUES (?,?,?,?)",
                (data["member_id"], "note", data.get("deskripsi", ""), now_iso())
            )
            conn.commit()
        self.send_json({"ok": True})

    def api_stats(self):
        with get_db() as conn:
            total_members = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
            aktif = conn.execute("SELECT COUNT(*) FROM members WHERE status='aktif'").fetchone()[0]
            total_vouchers = conn.execute("SELECT COUNT(*) FROM vouchers WHERE active=1").fetchone()[0]
            total_redeem = conn.execute("SELECT COUNT(*) FROM redemptions").fetchone()[0]
            self.send_json({
                "total_members": total_members,
                "aktif_members": aktif,
                "active_vouchers": total_vouchers,
                "total_redemptions": total_redeem,
                "base_url": BASE_URL,
                "register_url": f"{BASE_URL}/daftar"
            })

    def api_export_members(self):
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM members ORDER BY registered_at DESC").fetchall()
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["id", "nama", "nim", "email", "hp", "status", "poin", "notes", "registered_at"])
            for r in rows:
                writer.writerow([r["id"], r["nama"], r["nim"], r["email"] or "", r["hp"] or "",
                                 r["status"], r["poin"], r["notes"] or "", r["registered_at"]])
            body = output.getvalue().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=members_sejoli.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def api_qr_info(self):
        self.send_json({
            "old_qr_note": "QR pendaftaran lama tetap dipakai, tetapi harus diarahkan (redirect) ke alamat baru.",
            "new_register_url": f"{BASE_URL}/daftar",
            "recommendation": "Atur redirect 301 dari URL lama ke https://sejoli.grok.me/daftar",
            "qr_content": f"{BASE_URL}/daftar"
        })

    def make_kode(self, nama: str) -> str:
        initials = "".join([w[0] for w in nama.upper().split() if w])[:3] or "DON"
        with get_db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM donations WHERE kode LIKE ?", (f"{initials}%",)).fetchone()[0]
        return f"{initials}{n+1}"

    def api_donations_list(self):
        with get_db() as conn:
            rows = conn.execute("SELECT * FROM donations ORDER BY created_at DESC").fetchall()
            result = []
            for r in rows:
                d = dict(r)
                uses = conn.execute(
                    "SELECT member_nim, member_nama, used_at FROM donation_uses WHERE donation_id=? ORDER BY used_at DESC",
                    (r["id"],)
                ).fetchall()
                d["uses"] = [dict(u) for u in uses]
                result.append(d)
            self.send_json(result)

    def api_donation_check(self, data):
        nama = (data.get("nama") or "").strip()
        wa = (data.get("wa") or "").strip()
        if not nama or not wa:
            self.send_json({"ok": False, "error": "Nama dan WA wajib"}, 400)
            return
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM donations WHERE donor_nama=? AND donor_wa=? ORDER BY created_at DESC",
                (nama, wa)
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                uses = conn.execute(
                    "SELECT member_nim, member_nama, used_at FROM donation_uses WHERE donation_id=? ORDER BY used_at DESC",
                    (r["id"],)
                ).fetchall()
                d["uses"] = [dict(u) for u in uses]
                result.append(d)
            self.send_json({"ok": True, "donations": result})

    def api_donation_create(self, data):
        nama = (data.get("donor_nama") or "").strip()
        wa = (data.get("donor_wa") or "").strip()
        porsi = int(data.get("porsi") or 1)
        tipe = data.get("tipe") or "umum"
        if not nama or not wa or porsi < 1:
            self.send_json({"ok": False, "error": "Data tidak lengkap"}, 400)
            return
        kode = self.make_kode(nama)
        total = porsi * 20001
        with get_db() as conn:
            conn.execute(
                """INSERT INTO donations (kode, donor_nama, donor_wa, porsi, nilai_per_porsi, total, tipe,
                   target_jurusan, target_nim, target_nama, pesan, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (kode, nama, wa, porsi, 20001, total, tipe,
                 data.get("target_jurusan"), data.get("target_nim"), data.get("target_nama"),
                 data.get("pesan"), now_iso())
            )
            conn.commit()
        self.send_json({"ok": True, "kode": kode, "total": total})

    def api_donation_use(self, data):
        kode = (data.get("kode") or "").strip().upper()
        nim = (data.get("nim") or "").strip()
        nama_mhs = (data.get("nama") or "").strip()
        if not kode or not nim:
            self.send_json({"ok": False, "error": "Kode dan NIM wajib"}, 400)
            return
        with get_db() as conn:
            don = conn.execute("SELECT * FROM donations WHERE kode=?", (kode,)).fetchone()
            if not don:
                self.send_json({"ok": False, "error": "Kode donasi tidak ditemukan"}, 404)
                return
            if don["used_count"] >= don["porsi"]:
                self.send_json({"ok": False, "error": "Kuota donasi sudah habis"}, 400)
                return
            if don["tipe"] == "spesifik" and don["target_nim"] and don["target_nim"] != nim:
                self.send_json({"ok": False, "error": "Donasi ini khusus untuk NIM tertentu"}, 400)
                return
            if don["tipe"] == "jurusan" and don["target_jurusan"]:
                if don["target_jurusan"].lower() not in nim.lower():
                    self.send_json({"ok": False, "error": "Donasi ini khusus untuk jurusan tertentu"}, 400)
                    return
            conn.execute(
                "INSERT INTO donation_uses (donation_id, member_nim, member_nama, used_at) VALUES (?,?,?,?)",
                (don["id"], nim, nama_mhs, now_iso())
            )
            conn.execute("UPDATE donations SET used_count = used_count + 1 WHERE id=?", (don["id"],))
            conn.commit()
        self.send_json({"ok": True, "message": f"Voucher {kode} berhasil dipakai oleh {nim}"})

def main():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Sejoli CRM & Voucher berjalan di http://{HOST}:{PORT}")
    print(f"Admin: http://localhost:{PORT}")
    print(f"Pendaftaran (QR): http://localhost:{PORT}/daftar")
    print(f"Base URL produksi: {BASE_URL}")
    print("Data Sejoli terpisah dari Sarae. Database: sejolicrm.db")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer dihentikan.")
        server.server_close()

if __name__ == "__main__":
    main()
