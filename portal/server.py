#!/usr/bin/env python3
"""CHUNIL Smart Mail Portal — 팀 공용 통합 포털 서버 (단일 파일 · 표준 라이브러리).

팀원은 이 서버가 떠 있는 PC 의 주소(예: http://192.168.0.12:8080) 하나만 열고
**자기 회사 웹메일 아이디/비밀번호**를 넣으면, 본인 이름·직급·부서·사내 주소록·메일 분석
대시보드를 바로 쓴다. GitHub 가입·Secrets·코드 복붙이 필요 없다.

구성
  · HTTP       : http.server.ThreadingHTTPServer (표준 라이브러리) — 도커/DB 없음
  · 메일 수집  : scripts/sync_mail.py 를 라이브러리로 그대로 재사용 (같은 파서·같은 분류)
  · 인증       : mail.chunil.co.kr 실제 로그인으로 검증 → HttpOnly 세션 쿠키
  · 저장       : data/users/<아이디>/board.json (서버 안에만. 브라우저에는 본인 세션으로만)
                 data/users/<아이디>/cred.enc (웹메일 비밀번호 — 서버 마스터키로 암호화)
                 data/users/<아이디>/personal_contacts.json (그 사람의 개인주소록만)
                 data/shared_org_contacts.json (전사 공용 조직도 392명 — 개인주소록은 공유하지 않음)
  · 자동 갱신  : 백그라운드 워커가 10분마다 등록된 모든 사용자의 새 메일을 수집·분석

실행
  python server.py                 # 기본 0.0.0.0:8080
  python server.py --port 9000
  python server.py --no-worker     # 자동 갱신 없이 (점검용)

환경변수(.env 파일도 읽는다)
  PORT, HOST, DATA_DIR, GEMINI_API_KEY, GEMINI_MODEL,
  SYNC_INTERVAL(초, 기본 600), SEED_DAYS(기본 14), SEED_MAX(기본 30),
  NMAIL_IP_CHECK(기본 1), PORTAL_IMAGES(기본 1), BOARD_CONFIG,
  PORTAL_UPDATE_URL(GitHub raw portal 폴더), PORTAL_AUTO_UPDATE(0=끔),
  PORTAL_UPDATE_INTERVAL(초, 기본 300)

자동 업데이트
  서버가 5분마다 `mail-board-template` 저장소의 `portal/manifest.json` 을 확인해, 바뀐 파일만
  받아 스스로 갱신한다. 화면(index.html)만 바뀌면 접속자의 F5 만으로, 코드(server.py·
  scripts/*.py)가 바뀌면 문법 검사를 통과한 뒤 새 프로세스로 갈아타며 재시작한다.
  `data/`·`.env` 는 절대 건드리지 않고, 네트워크 장애는 로그만 남기고 서버는 계속 돈다.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import hashlib
import hmac
import json
import mimetypes
import os
import py_compile
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import sync_mail as sm  # noqa: E402  (scripts/sync_mail.py — 메일 파서·분류·Gemini)
import backfill_archive as bfa  # noqa: E402  (scripts/backfill_archive.py — 과거 메일 점진 백필)

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

VERSION = "1.2.22"
APP_NAME = "CHUNIL Smart Mail Portal"
KST = dt.timezone(dt.timedelta(hours=9))
COOKIE_NAME = "chunil_portal"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
STATE_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
MAX_STATE_BYTES = 1_000_000
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "SAMEORIGIN",
}

# ────────────────────────────────────────────────────────────── 자동 업데이트 상수
#  서버 PC 한 대만 고치면 팀 전원이 따라오게 하려는 장치.
#    · 5분마다 portal/manifest.json(작은 JSON)만 받아 파일별 sha256 비교 → 바뀐 파일만 다운로드
#    · index.html : 임시 파일로 받아 os.replace → 재시작 없이 접속자 F5 만으로 새 화면
#    · *.py       : py_compile 문법 검사 통과한 것만 교체 → 새 프로세스로 갈아타며 재시작
#    · 장애(네트워크·GitHub)는 경고 로그만 남기고 서버는 절대 멈추지 않는다
UPDATE_URL_DEFAULT = ("https://raw.githubusercontent.com/seongkuy0427-blip/"
                      "mail-board-template/main/portal")
UPDATE_INTERVAL_SEC = 300              # 확인 주기(초)
UPDATE_FILES = ("index.html", "server.py", "scripts/sync_mail.py", "scripts/backfill_archive.py")
UPDATE_MAX_BYTES = 8 * 1024 * 1024     # 파일 하나 상한
UPDATE_TIMEOUT = 20                    # 요청 타임아웃(초)


# ────────────────────────────────────────────────────────────── 설정
def load_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if k:
                    out[k] = v
    except OSError:
        pass
    return out


def _int(v: Any, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def build_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """환경변수 → 실행 설정. (우선순위: 실제 환경변수 > .env > 기본값)"""
    e = {**load_env_file(os.path.join(HERE, ".env")), **(env if env is not None else os.environ)}
    return {
        "host": (e.get("HOST") or "0.0.0.0").strip(),
        "port": _int(e.get("PORT"), 8080),
        "data_dir": os.path.abspath(os.path.expanduser(e.get("DATA_DIR") or os.path.join(HERE, "data"))),
        "gemini_key": (e.get("GEMINI_API_KEY") or "").strip(),
        "gemini_model": (e.get("GEMINI_MODEL") or sm.DEFAULT_MODEL).strip(),
        "sync_interval": max(60, _int(e.get("SYNC_INTERVAL"), 600)),
        "seed_days": max(1, _int(e.get("SEED_DAYS"), 14)),
        "seed_max": max(3, _int(e.get("SEED_MAX"), 30)),
        "max_new": max(5, _int(e.get("MAX_NEW"), 40)),
        "ip_check": (e.get("NMAIL_IP_CHECK") or "1").strip(),
        "images": (e.get("PORTAL_IMAGES") or "1").strip() not in ("0", "false", "False"),
        "board_config": (e.get("BOARD_CONFIG") or os.path.join(HERE, "board_config.json")).strip(),
        "shared_contacts_days": max(1, _int(e.get("SHARED_CONTACTS_DAYS"), 1)),
        "session_hours": max(1, _int(e.get("SESSION_HOURS"), 12)),
        "remember_days": max(1, _int(e.get("REMEMBER_DAYS"), 30)),
        "max_login_fails": max(3, _int(e.get("MAX_LOGIN_FAILS"), 10)),
        "login_fail_window": max(60, _int(e.get("LOGIN_FAIL_WINDOW"), 600)),
        "max_parallel_syncs": max(1, _int(e.get("MAX_PARALLEL_SYNCS"), 1)),
        # 과거 메일 백필 — 0 이면 끈다. 워커 1회당 사용자별 BACKFILL_LIMIT 건씩 채운다
        "backfill_limit": max(0, _int(e.get("BACKFILL_LIMIT"), 100)),
        "backfill_pages": max(1, _int(e.get("BACKFILL_PAGES"), 30)),
        "backfill_s_date": max(0, _int(e.get("BACKFILL_S_DATE"), 0)),   # 0 = 전체 기간
        "backfill_skip_items": max(1, _int(e.get("BACKFILL_SKIP_ITEMS"), 500)),
        # 자동 업데이트 — PORTAL_AUTO_UPDATE=0 이면 끈다(수동 교체만)
        "auto_update": (e.get("PORTAL_AUTO_UPDATE") or "1").strip().lower()
                       not in ("0", "false", "off", "no", "n"),
        "update_url": (e.get("PORTAL_UPDATE_URL") or UPDATE_URL_DEFAULT).strip().rstrip("/"),
        "update_interval": max(60, _int(e.get("PORTAL_UPDATE_INTERVAL"), UPDATE_INTERVAL_SEC)),
    }


CFG: dict[str, Any] = build_config()

# 테스트·교체 지점 (테스트는 이 두 이름을 가짜 클래스로 바꾼다)
NMAIL_CLASS = sm.Nmail
GEMINI_CLASS = sm.Gemini

# ────────────────────────────────────────────────────────────── 런타임 상태
SESSIONS: dict[str, dict] = {}
SESS_LOCK = threading.Lock()
STATUS: dict[str, dict] = {}
STATUS_LOCK = threading.Lock()
RUNNING: set[str] = set()
RUN_LOCK = threading.Lock()
NEXT_DUE: dict[str, float] = {}
SYNC_LOCK = threading.Lock()          # sync_mail 모듈의 전역 분류 상태를 한 번에 한 사용자만
KEY_LOCK = threading.Lock()
LOGIN_FAILS: dict[str, list[float]] = {}
LOGIN_LOCK = threading.Lock()
INDEX_CACHE: dict[str, Any] = {"mtime": 0.0, "bytes": None}

# 전송 최적화 — /api/data(보드 JSON)는 사용자당 11MB · gzip 1.2MB 라 매 요청 압축은 낭비다.
#  (path → [mtime_ns, size, gzip bytes, etag]) 로 압축본을 재사용하고, 내용이 그대로면
#  If-None-Match 에 304(본문 0바이트)로 답해 5분 자동 새로고침이 거의 빈 패킷으로 끝난다.
GZ_CACHE: dict[str, list[Any]] = {}
GZ_LOCK = threading.Lock()
GZ_CACHE_MAX = 24                 # 사용자 수 × 여유 — 넘치면 오래된 것부터 버린다
SESSION_HANDOFF: dict[str, tuple[Any, float]] = {}   # 동기화가 연 웹메일 세션을 백필이 이어 쓴다
STOP = threading.Event()
HTTPD: Any = None                       # serve_forever 중인 서버 — 자동 재시작 때 내린다
UPDATE_ROOT = HERE                      # 업데이트 대상 폴더(테스트는 임시 폴더로 바꿔 끼운다)
UPDATE_LOCK = threading.Lock()
UPDATE_STATE: dict[str, Any] = {        # 자동 업데이트 상태 — /api/health · 배너 · 로그
    "checks": 0, "lastCheckAt": "", "lastCheckOk": None, "lastError": "",
    "lastChanged": [], "lastApplied": [], "remoteVersion": "",
    "restartRequired": False, "nextCheckAt": "",
}
_RESTARTING = False


def kst_now() -> dt.datetime:
    return dt.datetime.now(KST)


def stamp(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return kst_now().strftime(fmt)


def data_dir() -> str:
    return CFG["data_dir"]


def ensure_data_dirs() -> None:
    os.makedirs(os.path.join(data_dir(), "users"), exist_ok=True)


def user_dir(uid: str) -> str:
    return os.path.join(data_dir(), "users", uid)


def user_path(uid: str, name: str) -> str:
    return os.path.join(user_dir(uid), name)


def read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def atomic_write_json(path: str, obj: Any, mode: int | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    if mode is not None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


# ────────────────────────────────────────────────────────────── 저장된 본문 사진 정리
#  2026-11-24 사용자 지적: GitHub data.enc.json 은 정리됐는데, PC 의 data/users/*/board.json 은
#  예전 수집분이라 `Hi CHUNIL, High green` 배너와 흐릿한 엑셀 X 아이콘이 그대로 보였다.
#  새 수집과 같은 기준(12KB 미만 · 배너/아이콘 비율 · 같은 사진 중복)으로 이미 저장된 사진도 거른다.
def _data_uri_bytes(uri: str) -> bytes | None:
    s = str(uri or "")
    if not s.startswith("data:"):
        return None
    head, _, payload = s.partition(",")
    if ";base64" not in head or not payload:
        return None
    try:
        return base64.b64decode(payload)
    except (ValueError, TypeError):
        return None


def sanitize_board_images(board: dict[str, Any]) -> int:
    """이미 저장된 `it["images"]` 에서 슬로건 배너·소형 아이콘·중복을 걷어낸다 → 지운 장수."""
    removed = 0
    seen: set[str] = set()
    items = board.get("items") if isinstance(board, dict) else None
    for it in items or []:
        if not isinstance(it, dict):
            continue
        imgs = it.get("images")
        if not isinstance(imgs, list) or not imgs:
            continue
        keep = []
        for im in imgs:
            if not isinstance(im, dict):
                removed += 1
                continue
            blob = _data_uri_bytes(im.get("dataUrl") or im.get("dataURI") or "")
            if blob is None:
                keep.append(im)          # data URI 가 아니면 건드리지 않는다
                continue
            if len(blob) < sm.IMG_MIN_BYTES or sm.is_banner_or_icon(sm.image_size(blob)):
                removed += 1
                continue
            digest = hashlib.sha256(blob).hexdigest()
            if digest in seen:
                removed += 1
                continue
            seen.add(digest)
            keep.append(im)
        if len(keep) != len(imgs):
            if keep:
                it["images"] = keep
                it["imageCount"] = len(keep)
            else:
                it.pop("images", None)
                it.pop("imageCount", None)
    return removed


def sanitize_user_board_images(uid: str) -> int:
    """사용자 board.json 한 개를 읽어 정리하고, 바뀌었으면 0600 으로 다시 쓴다."""
    path = user_path(uid, "board.json")
    board = read_json(path, None)
    if not isinstance(board, dict):
        return 0
    removed = sanitize_board_images(board)
    if not removed:
        return 0
    try:
        atomic_write_json(path, board, mode=0o600)
    except OSError as e:
        print(f"[images] {uid} board.json 정리 저장 실패: {e}", file=sys.stderr)
        return 0
    print(f"[images] {uid} 저장된 본문 사진 {removed}장 정리(슬로건 배너·아이콘·중복)", flush=True)
    return removed


def sanitize_all_boards() -> int:
    """data/users/*/board.json 전체를 한 번 정리한다(서버 기동 때 — 업데이트 재시작만으로 즉시 반영)."""
    root = os.path.join(data_dir(), "users")
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return 0
    total = 0
    for uid in names:
        if not os.path.isfile(os.path.join(root, uid, "board.json")):
            continue
        try:
            total += sanitize_user_board_images(uid)
        except Exception as e:  # noqa: BLE001 - 정리가 실패해도 서버는 뜬다
            print(f"[images] {uid} 정리 실패: {type(e).__name__}: {e}", file=sys.stderr)
    if total:
        print(f"[images] 저장된 본문 사진 모두 {total}장 정리했습니다", flush=True)
    return total


# ────────────────────────────────────────────────────────────── 로그 파일
#  pythonw.exe(창 없는 실행)에서는 콘솔이 없어 print 가 사라진다. 그 때도 남도록
#  stdout/stderr 를 data/server.log 로 함께 보낸다(5MB 넘으면 .1/.2 로 넘김).
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 2
_LOG_LOCK = threading.Lock()


class _RotatingLog:
    """간단한 로그 파일 — 크기 제한을 넘으면 server.log.1 → .2 로 밀어낸다."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, path: str, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS):
        self.path = os.path.abspath(path)
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.backups = max(1, int(backups))
        self._fh = None
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._open()

    def _open(self) -> None:
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)

    def _rotate(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass
        for i in range(self.backups - 1, 0, -1):
            src, dst = f"{self.path}.{i}", f"{self.path}.{i + 1}"
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass
        self._open()

    def write(self, text: str) -> int:
        if not text:
            return 0
        with _LOG_LOCK:
            try:
                if self._fh is not None and self._fh.tell() >= self.max_bytes:
                    self._rotate()
                self._fh.write(text)
            except Exception:  # noqa: BLE001 - 로그 실패가 서버를 멈추지 않게
                pass
        return len(text)

    def flush(self) -> None:
        with _LOG_LOCK:
            try:
                self._fh.flush()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        with _LOG_LOCK:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._fh.fileno()


class _Tee:
    """콘솔(있으면) + 로그 파일에 동시에 쓴다."""

    def __init__(self, *streams: Any):
        self.streams = [s for s in streams if s is not None]
        self.encoding = getattr(self.streams[0], "encoding", "utf-8") if self.streams else "utf-8"

    def write(self, text: str) -> int:
        for s in self.streams:
            try:
                s.write(text)
            except Exception:  # noqa: BLE001
                pass
        return len(text)

    def flush(self) -> None:
        for s in self.streams:
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        for s in self.streams:
            try:
                return s.fileno()
            except Exception:  # noqa: BLE001
                continue
        raise OSError("no fileno")


LOG_STREAM: _RotatingLog | None = None


def enable_file_logging(path: str) -> str:
    """이후의 stdout/stderr 를 path 파일에도 남긴다(pythonw 무창 실행 대비)."""
    global LOG_STREAM
    if LOG_STREAM is not None:
        return LOG_STREAM.path
    LOG_STREAM = _RotatingLog(path)
    sys.stdout = _Tee(sys.stdout, LOG_STREAM)    # type: ignore[assignment]
    sys.stderr = _Tee(sys.stderr, LOG_STREAM)    # type: ignore[assignment]
    return LOG_STREAM.path


def reset_runtime(data_dir_path: str | None = None) -> None:
    """테스트용 — 세션·상태·캐시를 비운다."""
    global _SESSION_DIR
    if data_dir_path:
        CFG["data_dir"] = os.path.abspath(data_dir_path)
    with SESS_LOCK:
        SESSIONS.clear()
        try:
            os.remove(sessions_path())      # 빈 상태에서 시작 — 지난 파일의 세션이 되살아나지 않게
        except OSError:
            pass
    _SESSION_DIR = None          # 지운 파일을 다음 요청이 다시 읽어 빈 상태에서 시작
    with STATUS_LOCK:
        STATUS.clear()
    with RUN_LOCK:
        RUNNING.clear()
    with LOGIN_LOCK:
        LOGIN_FAILS.clear()
    NEXT_DUE.clear()
    INDEX_CACHE.update({"mtime": 0.0, "bytes": None})
    ensure_data_dirs()


# ────────────────────────────────────────────────────────────── 암호(자격증명)
def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def master_key() -> bytes:
    """서버 마스터키 — data/.server_key. 없으면 만들어 0600 으로 둔다."""
    path = os.path.join(data_dir(), ".server_key")
    with KEY_LOCK:
        if os.path.exists(path):
            try:
                raw = bytes.fromhex(open(path, encoding="utf-8").read().strip())
                if len(raw) == 32:
                    return raw
            except (OSError, ValueError):
                pass
        key = secrets.token_bytes(32)
        os.makedirs(data_dir(), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(key.hex())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key


def save_cred(uid: str, password: str) -> None:
    """웹메일 비밀번호를 서버 마스터키로 암호화해 보관한다(백그라운드 갱신용)."""
    iv = secrets.token_bytes(12)
    ct = AESGCM(master_key()).encrypt(iv, password.encode("utf-8"), uid.encode("utf-8"))
    rec = {"v": 1, "iv": _b64e(iv), "ct": _b64e(ct), "savedAt": stamp(), "user": uid}
    atomic_write_json(user_path(uid, "cred.enc"), rec, mode=0o600)


def load_cred(uid: str) -> str | None:
    rec = read_json(user_path(uid, "cred.enc"), None)
    if not isinstance(rec, dict) or not rec.get("iv") or not rec.get("ct"):
        return None
    try:
        plain = AESGCM(master_key()).decrypt(_b64d(rec["iv"]), _b64d(rec["ct"]), uid.encode("utf-8"))
        return plain.decode("utf-8")
    except Exception:  # noqa: BLE001 - 복호화 실패는 재로그인으로 해결
        return None


def clear_cred(uid: str) -> None:
    try:
        os.remove(user_path(uid, "cred.enc"))
    except OSError:
        pass


# ────────────────────────────────────────────────────────────── 사용자
def normalize_id(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if "@" in s:
        local, _, domain = s.partition("@")
        if domain not in ("chunil.co.kr", "mail.chunil.co.kr"):
            return ""
        s = local
    return s if ID_RE.match(s) else ""


def list_users() -> list[str]:
    root = os.path.join(data_dir(), "users")
    try:
        names = os.listdir(root)
    except OSError:
        return []
    out = []
    for n in sorted(names):
        if ID_RE.match(n) and os.path.isdir(os.path.join(root, n)):
            out.append(n)
    return out


def user_meta(uid: str) -> dict:
    rec = read_json(user_path(uid, "user.json"), {}) or {}
    if not isinstance(rec, dict):
        rec = {}
    rec.setdefault("id", uid)
    return rec


def update_user_meta(uid: str, **kw: Any) -> dict:
    rec = user_meta(uid)
    rec.update(kw)
    rec["id"] = uid
    atomic_write_json(user_path(uid, "user.json"), rec, mode=0o600)
    return rec


def needs_relogin(uid: str) -> bool:
    return bool(user_meta(uid).get("needsRelogin"))


# ────────────────────────────────────────────────────────────── 세션
#  로그인 세션은 RAM(SESSIONS)에만 두지 않고 data/sessions.json(0600)에도 남긴다.
#  자동 업데이트 재시작이나 PC 재부팅으로 프로세스가 새로 떠도, 브라우저 쿠키가 살아 있으면
#  같은 로그인이 그대로 이어진다(만료된 세션은 되살리지 않고 파일에서도 지운다).
SESSION_FILE = "sessions.json"
SESSION_MAX = 400                     # 파일이 끝없이 자라지 않게 — 오래 쓴 것부터 버린다
_SESSION_DIR: str | None = None       # 어느 data 폴더의 세션 파일을 이미 읽었는지


def sessions_path() -> str:
    return os.path.join(data_dir(), SESSION_FILE)


def _persist_sessions_locked() -> None:
    """SESS_LOCK 을 잡은 채로 호출한다 — 지금 세션을 파일에 원자적으로 쓴다."""
    now = time.time()
    rows = {sid: rec for sid, rec in SESSIONS.items()
            if isinstance(rec, dict) and isinstance(rec.get("expires"), (int, float))
            and rec["expires"] > now}
    if len(rows) > SESSION_MAX:
        keep = sorted(rows.items(), key=lambda kv: float(kv[1].get("last") or 0),
                      reverse=True)[:SESSION_MAX]
        rows = dict(keep)
    try:
        atomic_write_json(sessions_path(), {"saved": stamp(), "sessions": rows}, mode=0o600)
    except OSError as e:              # 디스크 문제로 로그인이 막히지는 않게
        print(f"경고: 세션 파일을 쓰지 못했습니다({sessions_path()}): {e}", file=sys.stderr)


def restore_sessions() -> int:
    """data/sessions.json 에서 로그인 세션을 되살린다 — 만료·깨진 줄은 버리고 파일도 정리한다."""
    global _SESSION_DIR
    d = os.path.abspath(data_dir())
    if _SESSION_DIR == d:
        return 0
    _SESSION_DIR = d
    raw = read_json(sessions_path(), {}) or {}
    rows = raw.get("sessions") if isinstance(raw, dict) else None
    if not isinstance(rows, dict) or not rows:
        return 0
    now = time.time()
    n = 0
    with SESS_LOCK:
        for sid, rec in rows.items():
            if not isinstance(rec, dict) or sid in SESSIONS:
                continue
            uid, exp = rec.get("user"), rec.get("expires")
            if not isinstance(uid, str) or not uid:
                continue
            if not isinstance(exp, (int, float)) or exp <= now:
                continue
            SESSIONS[sid] = {"user": uid,
                             "created": float(rec.get("created") or now),
                             "expires": float(exp),
                             "remember": bool(rec.get("remember")),
                             "last": float(rec.get("last") or now)}
            n += 1
        if n != len(rows):
            _persist_sessions_locked()      # 만료분이 있었다 — 파일도 깨끗하게
    if n:
        print(f"로그인 세션 {n}건을 되살렸습니다 ({sessions_path()})")
    return n


def create_session(uid: str, remember: bool) -> str:
    sid = secrets.token_urlsafe(32)
    ttl = (CFG["remember_days"] if remember else 0) * 86400 or CFG["session_hours"] * 3600
    now = time.time()
    with SESS_LOCK:
        SESSIONS[sid] = {"user": uid, "created": now, "expires": now + ttl,
                         "remember": bool(remember), "last": now}
        _persist_sessions_locked()
    return sid


def get_session(sid: str | None) -> dict | None:
    if not sid:
        return None
    restore_sessions()               # 재시작 뒤 첫 요청이면 파일에서 되살린다
    with SESS_LOCK:
        s = SESSIONS.get(sid)
        if not s:
            return None
        if s["expires"] < time.time():
            SESSIONS.pop(sid, None)
            _persist_sessions_locked()
            return None
        s["last"] = time.time()
        return dict(s)


def drop_session(sid: str | None) -> None:
    if not sid:
        return
    with SESS_LOCK:
        SESSIONS.pop(sid, None)
        _persist_sessions_locked()


# ────────────────────────────────────────────────────────────── 진행 상태
def set_status(uid: str, _persist: bool = False, **kw: Any) -> dict:
    with STATUS_LOCK:
        st = STATUS.setdefault(uid, {})
        st.update(kw)
        st["at"] = stamp()
        snap = dict(st)
    if _persist or kw.get("state") in ("ready", "error"):
        atomic_write_json(user_path(uid, "status.json"), snap, mode=0o600)
    return snap


def get_status(uid: str) -> dict:
    with STATUS_LOCK:
        st = STATUS.get(uid)
        if st is None:
            st = read_json(user_path(uid, "status.json"), {}) or {}
            if not isinstance(st, dict):
                st = {}
            STATUS[uid] = st
        return dict(st)


def public_status(uid: str) -> dict:
    st = get_status(uid)
    meta = user_meta(uid)
    items_count = int(meta.get("itemCount") or 0)
    if not items_count and os.path.exists(user_path(uid, "board.json")):
        board = read_json(user_path(uid, "board.json"), None)   # 예전 보드만 한 번 읽어 센다
        items = board.get("items") if isinstance(board, dict) else None
        items_count = len(items) if isinstance(items, list) else 0
    try:
        contact_count = merged_contacts(uid)["counts"]["total"]
    except Exception:  # noqa: BLE001 - 상태 조회가 주소록 오류로 막히지 않게
        contact_count = 0
    out = {
        "ok": True,
        "id": uid,
        "state": st.get("state") or ("ready" if items_count else "idle"),
        "message": st.get("message") or ("준비 완료" if items_count else "아직 수집 전"),
        "progress": int(st.get("progress") or (100 if items_count else 0)),
        "phase": st.get("phase") or "",
        "lastSync": st.get("lastSync") or meta.get("lastSync"),
        "itemCount": items_count,
        "contactCount": contact_count,
        "backfill": meta.get("backfill") if isinstance(meta.get("backfill"), dict) else {},
        "running": uid in RUNNING,
        "updatedAt": st.get("at") or "",
    }
    if st.get("state") == "error":
        out["code"] = st.get("code") or "error"
        out["message"] = st.get("message") or "메일 수집 중 오류가 발생했습니다."
    return out


# ────────────────────────────────────────────────────────────── 로그인 시도 제한
def login_blocked(key: str) -> bool:
    now = time.time()
    window = CFG["login_fail_window"]
    with LOGIN_LOCK:
        arr = [t for t in LOGIN_FAILS.get(key, []) if now - t < window]
        LOGIN_FAILS[key] = arr
        return len(arr) >= CFG["max_login_fails"]


def record_login_fail(key: str) -> None:
    with LOGIN_LOCK:
        LOGIN_FAILS.setdefault(key, []).append(time.time())


def clear_login_fails(key: str) -> None:
    with LOGIN_LOCK:
        LOGIN_FAILS.pop(key, None)


# ────────────────────────────────────────────────────────────── 주소록
#   조직도(org, 392명)   : data/shared_org_contacts.json — 서버가 하루 1회 수집해 전원이 공유
#   개인주소록(personal) : data/users/<아이디>/personal_contacts.json — 각자 자기 세션으로만 수집
#   화면에는 /api/contacts 가 둘을 합쳐서(같은 메일은 조직도 우선) 내려준다.
SHARED_ORG_NAME = "shared_org_contacts.json"
LEGACY_SHARED_NAME = "shared_contacts.json"
CONTACTS_CACHE: dict[str, tuple[tuple, dict]] = {}
CONTACTS_CACHE_LOCK = threading.Lock()


def shared_org_path() -> str:
    return os.path.join(data_dir(), SHARED_ORG_NAME)


def load_shared_org() -> dict:
    path = shared_org_path()
    if not os.path.exists(path):
        path = os.path.join(data_dir(), LEGACY_SHARED_NAME)   # 초기 버전 파일명 호환
    rec = read_json(path, {}) or {}
    contacts = rec.get("contacts") if isinstance(rec, dict) else None
    if not isinstance(contacts, list):
        contacts = []
    return {"fetchedAt": (rec.get("fetchedAt") if isinstance(rec, dict) else "") or "",
            "groups": (rec.get("groups") if isinstance(rec, dict) else 0) or 0,
            "contacts": contacts}


def save_shared_org(org: list[dict], groups: int = 0) -> None:
    """조직도만 공유 파일에 쓴다 — 개인주소록은 절대 여기에 넣지 않는다."""
    rec = {"fetchedAt": stamp(), "count": len(org), "groups": groups, "contacts": org,
           "note": "사내 조직도(전사 공용). 개인주소록은 사용자별 personal_contacts.json 에만 저장됩니다."}
    atomic_write_json(shared_org_path(), rec, mode=0o600)


def shared_org_stale() -> bool:
    rec = load_shared_org()
    if not rec["contacts"] or not rec["fetchedAt"]:
        return True
    try:
        when = dt.datetime.strptime(rec["fetchedAt"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except ValueError:
        return True
    return (kst_now() - when).days >= CFG["shared_contacts_days"]


def load_personal(uid: str) -> dict:
    rec = read_json(user_path(uid, "personal_contacts.json"), None)
    if isinstance(rec, list):                       # 예전 형식(배열) 호환
        return {"fetchedAt": "", "contacts": rec}
    if not isinstance(rec, dict):
        return {"fetchedAt": "", "contacts": []}
    contacts = rec.get("contacts") if isinstance(rec.get("contacts"), list) else []
    return {"fetchedAt": rec.get("fetchedAt") or "", "contacts": contacts}


def save_personal(uid: str, personal: list[dict]) -> None:
    atomic_write_json(user_path(uid, "personal_contacts.json"),
                      {"fetchedAt": stamp(), "count": len(personal), "contacts": personal}, mode=0o600)


def fetch_org_contacts(nm: Any, *, pause: float = 0.2, max_groups: int = 400) -> tuple[list[dict], int]:
    """조직도(member_list.php)만 읽는다 — 개인주소록은 건드리지 않는다.

    공용 파일을 만드는 사람의 개인 거래처가 공용 저장소로 새지 않게 하려는 의도다.
    """
    groups = nm.contact_group_tree()
    org_groups = [g for g in groups
                  if g.get("kind") == "org" and str(g.get("no")) not in ("", "0")]
    out: list[dict] = []
    for g in org_groups[:max_groups]:
        try:
            rows = nm.contact_rows(sm.MEMBER_LIST_PATH, "s_mg_no", g["no"], kind="org", pause=pause)
        except sm.NmailError:
            raise
        except Exception as e:  # noqa: BLE001 - 한 그룹 실패가 전체를 멈추지 않게
            print(f"[contacts] 조직도 그룹 '{g.get('name')}' 실패: {type(e).__name__}", file=sys.stderr)
            rows = []
        out.extend({**row, "dept": sm.contact_dept(str(g.get("name") or ""))} for row in rows)
        time.sleep(pause)
    return out, len(org_groups)


def fetch_personal_contacts(nm: Any, *, pause: float = 0.2, max_groups: int = 50) -> list[dict]:
    """개인주소록(addr_list.php)만 읽는다 — 이 계정의 거래처·협력사 목록."""
    groups = nm.contact_group_tree()
    per_groups = [g for g in groups
                  if g.get("kind") == "personal" and str(g.get("no")) not in ("", "0")]
    out: list[dict] = []
    for g in per_groups[:max_groups]:
        try:
            rows = nm.contact_rows(sm.ADDR_LIST_PATH, "s_adg_no", g["no"], kind="personal", pause=pause)
        except sm.NmailError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[contacts] 개인주소록 '{g.get('name')}' 실패: {type(e).__name__}", file=sys.stderr)
            rows = []
        out.extend({**row, "dept": sm.contact_dept(str(g.get("name") or ""))} for row in rows)
        time.sleep(pause)
    return out


def merged_contacts(uid: str) -> dict:
    """조직도 + 그 사용자의 개인주소록 (같은 메일은 조직도 우선). 화면에는 이 목록을 준다."""
    shared = load_shared_org()
    personal_box = load_personal(uid)
    try:
        sig = (os.path.getmtime(shared_org_path()), os.path.getmtime(user_path(uid, "personal_contacts.json")))
    except OSError:
        sig = (0.0, 0.0)
    with CONTACTS_CACHE_LOCK:
        hit = CONTACTS_CACHE.get(uid)
        if hit and hit[0] == sig:
            return hit[1]
    merged = sm.merge_contacts(list(shared["contacts"]), list(personal_box["contacts"]))
    out = {"ok": True,
           "contacts": merged,
           "counts": {"org": sum(1 for c in merged if c.get("kind") == "org"),
                      "personal": sum(1 for c in merged if c.get("kind") != "org"),
                      "total": len(merged)},
           "orgFetchedAt": shared["fetchedAt"],
           "personalFetchedAt": personal_box["fetchedAt"]}
    with CONTACTS_CACHE_LOCK:
        CONTACTS_CACHE[uid] = (sig, out)
    return out


# ────────────────────────────────────────────────────────────── 동기화
def _friendly_error(e: BaseException) -> tuple[str, str]:
    """(코드, 사용자에게 보여줄 한 줄). 내부 진단 문구는 콘솔에만 남긴다."""
    msg = str(e)
    if isinstance(e, sm.NmailError):
        if "로그인" in msg or "비밀번호" in msg:
            return "auth", "웹메일 로그인에 실패했습니다. 로그아웃 후 다시 로그인해 주세요."
        return "nmail", "웹메일 화면을 읽지 못했습니다. 잠시 후 자동으로 다시 시도합니다."
    if "연결" in msg or "timed out" in msg.lower() or "Connection" in msg:
        return "network", "웹메일 서버에 연결하지 못했습니다. 네트워크를 확인해 주세요."
    return "error", "메일 수집 중 오류가 발생했습니다. 잠시 후 자동으로 다시 시도합니다."


def start_sync(uid: str, reason: str = "manual", force_contacts: bool = False,
               force: bool = False) -> bool:
    """사용자 1명 동기화를 백그라운드로 시작한다.

    force=True(로그인·수동 새로고침)면 다른 사용자가 동기화 중이어도 대기열에 넣는다
    — 로그인 직후의 수집이 조용히 빠지지 않게. 주기 워커는 force=False로 부른다.
    """
    with RUN_LOCK:
        if uid in RUNNING:
            return False
        if not force and len(RUNNING) >= CFG["max_parallel_syncs"]:
            return False
        RUNNING.add(uid)
    t = threading.Thread(target=_sync_main, args=(uid, reason, force_contacts),
                         name=f"sync-{uid}", daemon=True)
    t.start()
    return True


def _sync_main(uid: str, reason: str, force_contacts: bool) -> None:
    try:
        run_sync(uid, reason=reason, force_contacts=force_contacts)
    except Exception as e:  # noqa: BLE001 - 어떤 실패도 서버를 멈추지 않는다
        code, msg = _friendly_error(e)
        print(f"[sync] {uid} 실패({code}): {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        print(traceback.format_exc(limit=4), file=sys.stderr)
        set_status(uid, state="error", code=code, message=msg, progress=0, _persist=True)
        if code == "auth":
            update_user_meta(uid, needsRelogin=True)
    finally:
        # ── 새 메일 동기화가 끝난 뒤에야 과거 메일 1배치 — 신규 10분 동기화가 밀리지 않는다
        try:
            if CFG["backfill_limit"] > 0 and not needs_relogin(uid):
                run_backfill(uid)
        except Exception as e:  # noqa: BLE001 - 백필 실패는 경고만 남기고 동기화 결과를 건드리지 않는다
            print(f"[backfill] {uid} 경고: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        SESSION_HANDOFF.pop(uid, None)
        with RUN_LOCK:
            RUNNING.discard(uid)
        NEXT_DUE[uid] = time.time() + CFG["sync_interval"]


def _backfill_meta(board: dict) -> dict:
    return bfa.progress(board)


def run_backfill(uid: str, *, limit: int | None = None, nm: Any | None = None) -> dict:
    """과거 메일을 한 배치(기본 100건) board.json 에 병합한다.

    · 새 메일 동기화가 끝난 뒤에만 불린다(신규 메일이 밀리지 않게).
    · 이미 과거 기록이 충분한 보드(kimpro 처럼 2년치가 들어 있는 경우)는 should_skip 이 걸러
      로그인조차 하지 않는다.
    · 실패해도 새 메일 동기화 결과·상태는 그대로 두고 경고만 남긴다.
    """
    if CFG["backfill_limit"] <= 0:
        return {"ok": True, "skipped": "disabled"}
    board = read_json(user_path(uid, "board.json"), None)
    if not isinstance(board, dict) or not board.get("items"):
        return {"ok": True, "skipped": "no-board"}
    skip, why = bfa.should_skip(board, seed_days=CFG["seed_days"],
                                min_existing=CFG["backfill_skip_items"])
    if skip:
        if why == "existing":
            bfa.mark_done(board, reason="기존 과거 기록 보유")
            atomic_write_json(user_path(uid, "board.json"), board, mode=0o600)
            update_user_meta(uid, backfill=_backfill_meta(board))
            print(f"[backfill] {uid} 건너뜀 — 이미 과거 기록 {len(board.get('items') or [])}건 보유", flush=True)
        return {"ok": True, "skipped": why}

    password = load_cred(uid)
    if not password:
        return {"ok": True, "skipped": "no-cred"}

    if nm is None:
        cached, at = SESSION_HANDOFF.pop(uid, (None, 0.0))
        nm = cached if (cached is not None and time.time() - at < 300) else None
    opened = False
    if nm is None:
        nm = NMAIL_CLASS(uid, password, ip_check=str(CFG["ip_check"]))
        nm.login()
        opened = True

    contacts_list = merged_contacts(uid)["contacts"]
    contacts = sm.load_contacts({"contacts": contacts_list})
    try:
        with SYNC_LOCK:
            # sync_mail 의 분류 규칙은 전역 — 다른 사용자가 마지막으로 설정했을 수 있어 다시 맞춘다
            owner = board.get("owner") if isinstance(board.get("owner"), dict) else {}
            sm.configure(dict(owner), classify_cfg(uid))
            report = bfa.run_batch(board, nm, limit=limit or CFG["backfill_limit"],
                                   max_pages=CFG["backfill_pages"], s_date=CFG["backfill_s_date"],
                                   contacts=contacts)
            if report.get("added") or report.get("pages"):
                # 배치 결과뿐 아니라 진행 위치(cursor)도 남긴다 — 다음 주기가 같은 페이지에서 맴돌지 않게
                atomic_write_json(user_path(uid, "board.json"), board, mode=0o600)
                update_user_meta(uid, itemCount=len(board.get("items") or []),
                                 backfill=_backfill_meta(board),
                                 lastBackfill=bfa.progress(board).get("updatedAt") or stamp())
    finally:
        if opened:
            try:
                nm.s.close()          # 백필용으로 새로 연 세션은 닫는다(동기화 세션은 그대로)
            except Exception:         # noqa: BLE001
                pass

    if report.get("added") or report.get("done"):
        pr = bfa.progress(board)
        print(f"[backfill] {uid} +{report['added']}건 · 누적 {pr['archived']}건(과거) · "
              f"훑은 페이지 {report['pages']} · 완료={pr['done']}" +
              (f" · {report['warnings'][0]}" if report.get("warnings") else ""), flush=True)
    return {"ok": True, **report}


def run_sync(uid: str, *, reason: str = "auto", force_contacts: bool = False) -> dict:
    """한 사용자의 보드를 갱신한다(sync_mail.py 의 sync() 와 같은 순서, 평문 board.json).

    sync_mail 모듈은 분류 규칙을 전역(OWNER·VIPS·MENTION_RE)에 두므로 SYNC_LOCK 으로
    한 번에 한 사용자만 처리한다(팀 규모에서 충분하고, 등급이 섞이지 않는다).
    """
    password = load_cred(uid)
    if not password:
        raise RuntimeError("웹메일 비밀번호가 저장되어 있지 않습니다 — 다시 로그인해 주세요.")

    board_path = user_path(uid, "board.json")
    board = read_json(board_path, None)
    if not isinstance(board, dict):
        board = {}
    first = not board.get("items")

    set_status(uid, state=("seeding" if first else "syncing"),
               message=("초기 메일 수집 중…" if first else "새 메일 확인 중…"),
               phase="login", progress=3, startedAt=stamp(), lastSync=get_status(uid).get("lastSync"),
               _persist=True)

    with SYNC_LOCK:
        nm = NMAIL_CLASS(uid, password, ip_check=str(CFG["ip_check"]))
        nm.login()
        SESSION_HANDOFF[uid] = (nm, time.time())   # 같은 세션으로 백필 1배치를 이어서

        # ── 주소록 — 조직도는 공용 파일(하루 1회 수집), 개인주소록은 이 사용자만
        set_status(uid, phase="contacts", progress=8, message="주소록 확인 중…")
        shared = load_shared_org()
        personal_box = load_personal(uid)
        personal = list(personal_box["contacts"])
        need_org = force_contacts or shared_org_stale() or not shared["contacts"]
        personal_age_ok = False
        if personal_box["fetchedAt"]:
            try:
                when = dt.datetime.strptime(personal_box["fetchedAt"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
                personal_age_ok = (kst_now() - when).days < 1
            except ValueError:
                personal_age_ok = False
        if need_org:
            try:
                org, groups = fetch_org_contacts(nm)
            except Exception as e:  # noqa: BLE001 - 주소록 실패가 메일 수집을 막지 않는다
                org, groups = [], 0
                print(f"[sync] {uid} 조직도 수집 실패: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            if org:
                save_shared_org(org, groups=groups)
                shared = load_shared_org()
        if force_contacts or not personal_age_ok:
            try:
                got = fetch_personal_contacts(nm)
            except Exception as e:  # noqa: BLE001
                got = None
                print(f"[sync] {uid} 개인주소록 수집 실패: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            if got is not None:
                personal = got
                save_personal(uid, personal)
        contacts_list = sm.merge_contacts(list(shared["contacts"]), list(personal))
        contacts = sm.load_contacts({"contacts": contacts_list})

        # ── 로그인 사용자(이름·직급·부서) — 화면과 본문 지목 판정의 기준
        owner = sm.detect_owner(nm, uid, contacts=contacts_list, data=board)
        sm.configure(owner, classify_cfg(uid))
        owner_box = {k: sm.OWNER.get(k) for k in ("name", "title", "dept", "company", "email")}

        now = sm.kst_now()
        if first:
            board = sm.new_payload(owner=dict(sm.OWNER), seed_days=CFG["seed_days"], now=now)
        board["owner"] = dict(sm.OWNER)
        board["account"] = (sm.OWNER.get("email") or board.get("account")
                            or f"{uid}@{sm.DOMAIN}")
        board.pop("contacts", None)      # 주소록은 /api/contacts 로 내려준다(공용 조직도 갱신 즉시 반영)
        board.setdefault("items", [])
        board.setdefault("followups", [])
        board.setdefault("criteria", {})
        board.setdefault("thread_cache", {})      # 스레드별 누적 타임라인(v1.2.11 증분 분석)
        update_user_meta(uid, owner=owner_box, needsRelogin=False, email=board["account"])

        # 직급 자동 추론 — 사내 조직도의 '직위' 칸이 비어 이름만 뜨는 경우를 보완한다.
        #  이미 쌓인 기록부터 먼저 보고(신규 메일이 없어도 다음 동기화에 채워진다), 첫 실행이면 아래에서 한 번 더.
        if sm.ensure_owner_title(board):
            owner_box = {k: sm.OWNER.get(k) for k in ("name", "title", "dept", "company", "email")}
            update_user_meta(uid, owner=owner_box)
            # 신규 메일이 없어 아래에서 바로 끝나도 직급이 남도록 지금 저장한다
            atomic_write_json(board_path, board, mode=0o600)
            print(f"[sync] {uid} 직급 자동 추론 — {sm.OWNER.get('name')} {sm.OWNER.get('title')}", flush=True)

        # ── 새 메일
        base = sm.max_mf_no(board)
        set_status(uid, phase="list", progress=12, message="편지함을 확인하는 중…")
        if first:
            inbox = nm.seed_mailbox("inbox", days=CFG["seed_days"], limit=CFG["seed_max"])
            sent = nm.seed_mailbox("sent", days=CFG["seed_days"], limit=max(5, CFG["seed_max"] // 3))
        else:
            inbox = [m for m in nm.list_mailbox("inbox", pages=2, stop_below=base) if m["mf_no"] > base]
            sent = [m for m in nm.list_mailbox("sent", pages=2, stop_below=base) if m["mf_no"] > base]
            inbox = inbox[: CFG["max_new"]]
            sent = sent[: CFG["max_new"]]

        if not inbox and not sent and not first:
            try:      # 새 메일이 없어도 예전 요약·답장 초안은 한 번 정리한다 (v1.2.7 · v1.2.8)
                fixed = sm.refresh_copied_summaries(board["items"], contacts)
                fixed += sm.refresh_reply_drafts(board["items"])
                # v1.2.21 — 예전 수집분도 본문 인용 스레드(From:/Sent:)를 히스토리에 채운다
                fixed += sm.enrich_threads_from_body(board["items"])
                # v1.2.17 — 사진 첨부 썸네일: 새 메일이 없는 실행에서도 빈 칩·주소 없는 사진 첨부를 몇 건씩 고친다
                fixed += sm.refresh_attachment_meta(nm, board["items"], limit=4)
                if fixed:
                    atomic_write_json(board_path, board, mode=0o600)
            except Exception as e:  # noqa: BLE001 - 정리가 실패해도 동기화 상태는 그대로 알린다
                print(f"[sync] {uid} 요약 정리 실패: {type(e).__name__}", file=sys.stderr)
            set_status(uid, state="ready", phase="done", progress=100,
                       message="새 메일 없음 — 최신 상태입니다", lastSync=board.get("updatedAt"),
                       _persist=True)
            return public_status(uid)      # set_status 이후 상태를 돌려준다(syncing 이 아니라)

        gem = GEMINI_CLASS(CFG["gemini_key"], CFG["gemini_model"]) if CFG["gemini_key"] else None
        report: dict[str, Any] = {"gemini_calls": 0, "warnings": []}
        total = max(1, len(inbox) + len(sent))
        done = 0

        def analyze(mail: dict, kind: str) -> dict:
            # v1.2.21 — 기록 보관만 AI 생략, 나머지는 3단 정밀 분석(스레드 증분) · 첨부 핵심 내용 포함
            return sm.analyze_mail(gem, mail, kind, contacts,
                                   thread_cache=board["thread_cache"], stats=report, fetcher=nm)

        def tick(phase: str) -> None:
            nonlocal done
            done += 1
            pct = min(95, 12 + int(80 * done / total))
            set_status(uid, phase=phase, progress=pct,
                       message=f"메일 분석 중… ({done}/{total})")

        added = 0
        merged = 0
        title_sent: list[str] = []      # 직급 추론용 서명 글 — 보낸편지함
        title_inbox: list[str] = []     # 직급 추론용 호칭·인용 글 — 받은편지함
        for row in inbox:
            detail = nm.read_mail("inbox", row["mf_no"])
            if not detail.get("subject"):
                detail["subject"] = row["subject"]
            title_inbox.append(str(detail.get("subject") or "") + "\n" + str(detail.get("bodyText") or ""))
            if CFG["images"] and detail.get("images"):
                detail["imagesData"] = sm.collect_images(nm, detail["images"], report)
            analysis = analyze(detail, "받은편지함")
            item = sm.build_item(detail, analysis, contacts, now)
            target = sm.find_thread_target(board["items"], detail["subject"], detail["date"])
            if target is not None:
                sm.merge_into_thread(target, item)
                merged += 1
            else:
                board["items"].append(item)
                added += 1
            tick("inbox")

        replied = 0
        for row in sent:
            detail = nm.read_mail("sent", row["mf_no"])
            if not detail.get("subject"):
                detail["subject"] = row["subject"]
            title_sent.append(str(detail.get("subject") or "") + "\n" + str(detail.get("bodyText") or ""))
            analysis = analyze(detail, "보낸편지함")
            if sm.apply_sent_reply(board["items"], detail, analysis, contacts):
                replied += 1
            tick("sent")

        refreshed = sm.refresh_copied_summaries(board["items"], contacts)   # 예전 '본문 복붙' 요약 → 3인칭 (v1.2.7)
        if refreshed:
            print(f"[sync] {uid} 예전 요약 {refreshed}건을 3인칭 요약으로 정리", flush=True)
        drafts_fixed = sm.refresh_reply_drafts(board["items"])              # 옛 답장 초안 → 실무형 3안 (v1.2.8)
        if drafts_fixed:
            print(f"[sync] {uid} 답장 초안 {drafts_fixed}건을 실무형 3안으로 정리", flush=True)
        # v1.2.17 — 📎 사진 첨부 썸네일: 이름이 빈 칩·주소 없는 사진 첨부를 실행마다 몇 건씩 다시 읽어 고친다
        #  (새로 오는 메일은 parse_read 가 url 까지 담지만, 이미 쌓인 메일은 다시 읽지 않으면 옛 기록이 남는다)
        try:
            atts_fixed = sm.refresh_attachment_meta(nm, board["items"], limit=4)
        except Exception as e:  # noqa: BLE001 - 첨부 보정 실패가 동기화를 막지 않는다
            atts_fixed = 0
            print(f"[sync] {uid} 첨부 목록 보정 경고: {type(e).__name__}: {str(e)[:160]}", flush=True)
        if atts_fixed:
            print(f"[sync] {uid} 사진 첨부 목록 {atts_fixed}건 다시 읽음", flush=True)
        # ── 메타
        #  첫 실행은 지금 모은 메일에서 직급을 한 번 더 추론한다(위에는 쌓인 기록이 없었으므로)
        if sm.ensure_owner_title(board, sent_texts=title_sent, inbox_texts=title_inbox):
            owner_box = {k: sm.OWNER.get(k) for k in ("name", "title", "dept", "company", "email")}
            update_user_meta(uid, owner=owner_box)
            print(f"[sync] {uid} 직급 자동 추론 — {sm.OWNER.get('name')} {sm.OWNER.get('title')}", flush=True)
        set_status(uid, phase="save", progress=97, message="보드를 저장하는 중…")
        stamp_hm = now.strftime("%Y-%m-%d %H:%M")
        board["updatedAt"] = f"{stamp_hm} (KST)"
        crit = board.setdefault("criteria", {})
        for older, newer in (("prev4VerifiedAt", "prev3VerifiedAt"), ("prev3VerifiedAt", "prev2VerifiedAt"),
                             ("prev2VerifiedAt", "prevVerifiedAt"), ("prevVerifiedAt", "verifiedAt")):
            if crit.get(newer):
                crit[older] = crit[newer]
        if first:
            board["source"] = (f"mail.chunil.co.kr 받은편지함 (최근 {CFG['seed_days']}일 · 이전 기록은 검색)")
            board["rangeFrom"] = (now - dt.timedelta(days=CFG["seed_days"])).strftime("%Y-%m-%d")
            crit["verifiedAt"] = (
                f"{stamp_hm} (KST) 통합 포털 최초 구축 — 최근 {CFG['seed_days']}일 받은편지함 "
                f"{len(inbox)}건을 읽어 {added}건 등록 · 보낸편지함 {len(sent)}건 대조(회신 반영 {replied}건)"
                f" · 주소록 조직도 {len(shared['contacts'])}명 + 개인 {len(personal)}명 연결"
                + (f" · Gemini {report['gemini_calls']}회" if gem else " · 요약 기본 모드")
            )
        else:
            crit["verifiedAt"] = (
                f"{stamp_hm} (KST) 포털 서버 자동 갱신 — 받은편지함 신규 {len(inbox)}건 중 {added}건 추가"
                + (f"·스레드 병합 {merged}건" if merged else "")
                + f" · 보낸편지함 신규 {len(sent)}건(회신 반영 {replied}건)"
                + (f" · Gemini {report['gemini_calls']}회" if gem else " · 요약 기본 모드")
            )
        board["items"].sort(key=lambda it: (str(it.get("lastAt") or it.get("receivedAt") or ""), str(it.get("id"))),
                            reverse=True)
        pruned = sanitize_board_images(board)      # 예전 수집분의 슬로건 배너·아이콘도 함께 정리
        if pruned:
            print(f"[sync] {uid} 저장된 본문 사진 {pruned}장 정리", flush=True)
        atomic_write_json(board_path, board, mode=0o600)
        update_user_meta(uid, needsRelogin=False,
                         lastSync=board["updatedAt"], itemCount=len(board["items"]))
        set_status(uid, state="ready", phase="done", progress=100,
                   message=(f"초기 수집 완료 — 메일 {len(board['items'])}건" if first else "최신 상태입니다"),
                   lastSync=board["updatedAt"], newCount=len(inbox) + len(sent), _persist=True)
        print(f"[sync] {uid} {'초기' if first else '갱신'} 완료 — 신규 {len(inbox)}/{len(sent)}건, "
              f"항목 {len(board['items'])}건, 경고 {len(report['warnings'])}건", flush=True)
    return public_status(uid)


# ────────────────────────────────────────────────────────────── ✨ AI 유사 사례 정밀 분석
#  화면의 '✨ AI 정밀 분석' 을 누를 때만 움직인다(평소 0 호출). 결과는 메일별로 캐시한다.
SIMILAR_CACHE_NAME = "similar_cache.json"
SIMILAR_CANDIDATES = 25          # Gemini 에 보낼 1차 후보 수
SIMILAR_MAX = 5                  # 화면에 보일 최대 건수
SIMILAR_BODY_CHARS = 1000        # 대상 메일 본문 전달 상한
SIMILAR_CACHE_MAX = 300          # 캐시에 남기는 메일 수
SIMILAR_TOKEN_RE = re.compile(r"[0-9a-z]{2,}|[가-힣]{2,}")
SIMILAR_CODE_RE = re.compile(r"\b\d{6,10}\b")
SIMILAR_STOP = set(("그리고 그런데 그러나 하지만 또한 대한 대하 위하 통해 관련 관하여 이번 지난 오늘 내일 어제 "
                    "있습니다 합니다 드립니다 바랍니다 부탁 요청 확인 공유 안내 송부 전달 회신 참고 아래 내용 메일 자료 "
                    "첨부 제목 본문 답변 감사 드림 수고 노고 업무 진행 사항 현황 건의 등등 것 것 같 같 습니다 함.").split())

SIMILAR_SYSTEM = """당신은 천일정기화물자동차(주) 납입1팀 김성규 과장의 업무 기록 정리 비서다.
지금 보고 있는 메일과 후보 메일 목록을 비교해, **실무 맥락상 실제로 참고 가치가 있는** 과거 사례만 골라낸다.

[고르는 기준]
- 같은 품번·ASN·BL·모델코드, 같은 거래처·협력사, 같은 업무 흐름(PTR 막차 납입·EOP 재고실사·용기 승인 등)이 이어지는 건
- 지금 메일의 판단·회신·조치에 직접 도움이 되는 배경(이전 합의·진행 상황·담당자·기한)을 담은 건

[반드시 빼는 것]
- 단어가 우연히 겹치기만 한 무관한 메일 (예: 'AI' vs 'Air', '음주측정' vs 'KPI', '납입'이 회사 이름 일부인 경우)
- 제목만 비슷하고 내용이 다른 건, 단순 공지·인사·시스템 알림
- 후보 목록에 없는 id 를 만들어 내지 않는다

[출력] 아래 형식의 JSON 만. picks 는 최대 5건, 참고 가치가 없으면 빈 배열.
{"picks": [{"id": "후보 id", "score": 1~100, "reason": "왜 도움이 되는지 한국어 1줄(40자 이내)"}]}"""

SIMILAR_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"},
                               "score": {"type": "integer"},
                               "reason": {"type": "string"}},
                "required": ["id", "score", "reason"],
            },
        }
    },
    "required": ["picks"],
}


def similar_cache_path(uid: str) -> str:
    return user_path(uid, SIMILAR_CACHE_NAME)


def similar_cache_load(uid: str) -> dict:
    rec = read_json(similar_cache_path(uid), {}) or {}
    items = rec.get("items") if isinstance(rec, dict) else None
    return {"v": 1, "items": items if isinstance(items, dict) else {}}


def similar_cache_save(uid: str, cache: dict) -> None:
    items = cache.get("items") or {}
    if len(items) > SIMILAR_CACHE_MAX:      # 최신 것만 남긴다
        keep = sorted(items.items(), key=lambda kv: str((kv[1] or {}).get("at") or ""), reverse=True)
        cache = {"v": 1, "items": dict(keep[:SIMILAR_CACHE_MAX])}
    atomic_write_json(similar_cache_path(uid), cache, mode=0o600)


def _similar_tokens(text: str) -> set[str]:
    out: set[str] = set()
    for w in SIMILAR_TOKEN_RE.findall(str(text or "").lower()):
        if len(w) >= 2 and w not in SIMILAR_STOP:
            out.add(w)
    return out


def _similar_codes(text: str) -> set[str]:
    return set(SIMILAR_CODE_RE.findall(str(text or "")))


def _similar_text(it: dict) -> tuple[str, str]:
    title = str(it.get("title") or "")
    body = " ".join(str(it.get(k) or "") for k in ("summary", "bodyText"))
    if it.get("tags"):
        body += " " + " ".join(str(t) for t in it["tags"])
    return title, body


def similar_candidates(board: dict, target: dict, *, limit: int = SIMILAR_CANDIDATES) -> list[dict]:
    """대상 메일과 제목·키워드·품번·발신자가 겹치는 과거 메일 상위 limit 건(대상·같은 스레드 제외)."""
    t_title, t_body = _similar_text(target)
    t_tok = _similar_tokens(t_title) | _similar_tokens(t_body)
    t_title_tok = _similar_tokens(t_title)
    t_codes = _similar_codes(t_title + " " + t_body + " " + str(target.get("subject") or ""))
    skip = {str(target.get("id"))}
    for m in target.get("thread") or []:
        if isinstance(m, dict) and m.get("id"):
            skip.add(str(m["id"]))
    sender = str(target.get("senderEmail") or "").lower()
    scored: list[tuple[float, dict]] = []
    for it in board.get("items") or []:
        if not isinstance(it, dict) or not it.get("id") or str(it["id"]) in skip:
            continue
        o_title, o_body = _similar_text(it)
        score = 0.0
        codes = _similar_codes(o_title + " " + o_body) & t_codes
        if codes:
            score += 8.0 * len(codes)
        shared = (_similar_tokens(o_title) | _similar_tokens(o_body)) & t_tok
        if shared:
            for w in shared:
                score += 3.0 if w in t_title_tok else 1.0
        if score <= 0:
            continue
        if sender and str(it.get("senderEmail") or "").lower() == sender:
            score += 4.0
        scored.append((score, it))
    scored.sort(key=lambda x: (x[0], str(x[1].get("receivedAt") or x[1].get("lastAt") or "")), reverse=True)
    return [it for _score, it in scored[:limit]]


def similar_payload(target: dict, candidates: list[dict]) -> dict:
    t_title, t_body = _similar_text(target)
    lines = [
        "[지금 보고 있는 메일]",
        f"- 제목: {t_title}",
        f"- 받은시각: {target.get('receivedAt') or ''}",
        f"- 발신: {target.get('sender') or ''} <{target.get('senderEmail') or ''}>",
        f"- 요약: {str(target.get('summary') or '')[:400]}",
        f"- 본문: {t_body[:SIMILAR_BODY_CHARS]}",
        "",
        f"[과거 메일 후보 {len(candidates)}건]",
    ]
    for i, it in enumerate(candidates, 1):
        o_title, o_body = _similar_text(it)
        lines.append(f"{i}. id={it.get('id')} | {str(it.get('receivedAt') or '')[:16]} | "
                     f"{str(it.get('senderName') or it.get('sender') or '')[:40]} | "
                     f"{str(it.get('category') or '')} | {o_title[:120]} | {o_body[:180]}")
    lines.append("")
    lines.append("위 후보 중 실제로 참고 가치가 있는 사례만 골라 JSON 으로 답하시오.")
    payload = {
        "systemInstruction": {"parts": [{"text": SIMILAR_SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": "\n".join(lines)}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": SIMILAR_SCHEMA,
            "maxOutputTokens": 2048,        # 한국어 사유가 길어져도 JSON 이 잘리지 않게
        },
    }
    if "flash" in str(CFG.get("gemini_model") or ""):
        payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
    return payload


def strip_json_fence(text: str) -> str:
    """Gemini 가 ```json … ``` 코드펜스로 감싸 보내도 JSON 본문만 남긴다."""
    t = str(text or "").strip()
    if not t.startswith("```"):
        return t
    t = t[3:]
    nl = t.find("\n")
    if nl != -1 and len(t[:nl].strip()) <= 16 and " " not in t[:nl].strip():
        t = t[nl + 1:]                       # ```json 같은 언어 태그 줄은 버린다
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    return t.strip()


def parse_gemini_json(text: str):
    """응답 텍스트 → JSON. 코드펜스나 앞뒤 설명 문장이 섞여도 건져 낸다."""
    raw = str(text or "").strip()
    cands = [raw, strip_json_fence(raw)]
    for s in list(cands):
        m = re.search(r"[\[{]", s)
        if not m:
            continue
        body = s[m.start():]
        end = max(body.rfind("}"), body.rfind("]"))
        cands.append(body[:end + 1] if end > 0 else body)
    for cand in cands:
        try:
            return json.loads(cand)
        except (TypeError, ValueError):
            pass
    raise sm.GeminiError("Gemini 응답을 JSON 으로 읽지 못했습니다(형식 오류)")


def gemini_pick_similar(target: dict, candidates: list[dict]) -> list[dict]:
    """Gemini 에 후보를 보내 참고 가치가 있는 것만 받아 온다(최대 SIMILAR_MAX)."""
    gem = GEMINI_CLASS(CFG["gemini_key"], CFG["gemini_model"])
    hook = getattr(gem, "similar", None)
    if callable(hook):                       # 테스트·대체 구현
        raw = hook(target, candidates)
    else:
        data = gem._request(similar_payload(target, candidates))
        cand = (data.get("candidates") or [{}])[0] if isinstance(data, dict) else {}
        parts = ((cand.get("content") or {}).get("parts") or [])
        text = "".join(str(p.get("text") or "") for p in parts).strip()
        if not text:
            reason = str(cand.get("finishReason") or "")
            raise sm.GeminiError("Gemini 응답이 비어 있습니다" + (f" (finishReason={reason})" if reason else ""))
        try:
            raw = parse_gemini_json(text)
        except sm.GeminiError:
            if str(cand.get("finishReason") or "") == "MAX_TOKENS":
                raise sm.GeminiError("Gemini 응답이 길어 잘렸습니다(MAX_TOKENS)") from None
            raise
    rows = raw.get("picks") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    allowed = {str(it.get("id")) for it in candidates}
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen or mid not in allowed:
            continue                          # 모델이 지어낸 id 는 버린다
        try:
            score = int(float(row.get("score") or 0))
        except (TypeError, ValueError):
            score = 0
        score = min(100, max(1, score))
        reason = re.sub(r"\s+", " ", str(row.get("reason") or "")).strip()[:200]
        seen.add(mid)
        out.append({"id": mid, "score": score, "reason": reason})
        if len(out) >= SIMILAR_MAX:
            break
    return out


def similar_error_message(e: BaseException) -> str:
    """✨ AI 정밀 분석 실패 원인을 사용자가 읽을 수 있는 한 줄로 바꿔 준다.

    키·요청 본문 같은 비밀값은 메시지에 담지 않는다 — 원인 유형과 예외 이름만 쓴다.
    """
    text = f"{type(e).__name__}: {e}".lower()
    codes: set[int] = set()
    status = getattr(getattr(e, "response", None), "status_code", None)
    if isinstance(status, int):
        codes.add(status)
    m = re.search(r"\b(?:http|status|code)[ :]*([45]\d\d)\b", text)
    if m:
        codes.add(int(m.group(1)))
    if 429 in codes or "resource_exhausted" in text or "quota" in text or "rate limit" in text:
        return "Gemini API 요청 한도에 도달했습니다. 잠시 후 다시 시도해 주세요."
    if codes & {401, 403} or any(k in text for k in (
            "api_key_invalid", "api key not valid", "invalid api key",
            "permission_denied", "unauthenticated")):
        return ("Gemini API 키가 유효하지 않거나 권한이 없습니다. "
                "서버 .env 의 GEMINI_API_KEY 를 확인해 주세요.")
    if any(k in text for k in (
            "timeout", "timed out", "connection", "network", "getaddrinfo",
            "name or service not known", "temporary failure", "unreachable",
            "ssl", "proxy", "remotedisconnected", "connectionreset")):
        return "Gemini 서버 응답이 지연되고 있습니다. 잠시 후 다시 시도해 주세요."
    if 404 in codes or "no longer available" in text or ("model" in text and "not found" in text):
        return ("Gemini 모델(404) 설정을 확인해 주세요. 사용하던 모델이 종료되었을 수 있습니다 "
                f"(현재 {sm.DEFAULT_MODEL} — 서버 .env 의 GEMINI_MODEL).")
    return f"AI 분석에 실패했습니다({type(e).__name__}). 잠시 후 다시 시도해 주세요."


def similar_analyze(uid: str, mail_id: str) -> dict:
    """✨ AI 정밀 분석 1회 — 캐시 → 후보 추리기 → Gemini → 저장."""
    mail_id = str(mail_id or "").strip()
    if not mail_id:
        return {"ok": False, "code": "bad_request", "message": "메일 id 가 필요합니다."}
    board = read_json(user_path(uid, "board.json"), None)
    if not isinstance(board, dict):
        return {"ok": False, "code": "no_board", "message": "보드를 아직 읽지 못했습니다. 잠시 후 다시 시도해 주세요."}
    target = next((it for it in board.get("items") or []
                   if isinstance(it, dict) and str(it.get("id")) == mail_id), None)
    if target is None:
        return {"ok": False, "code": "not_found", "message": "이 메일을 보드에서 찾지 못했습니다."}

    cache = similar_cache_load(uid)
    hit = cache["items"].get(mail_id)
    if isinstance(hit, dict) and isinstance(hit.get("results"), list):
        return {"ok": True, "cached": True, "id": mail_id, "count": len(hit["results"]),
                "model": hit.get("model") or "", "at": hit.get("at") or "",
                "results": hit["results"]}

    if not CFG["gemini_key"]:
        return {"ok": False, "code": "no_key",
                "message": "Gemini API 키가 설정되지 않아 AI 정밀 분석을 쓸 수 없습니다 — 서버 .env 에 GEMINI_API_KEY 를 넣고 서버를 다시 시작해 주세요."}

    cands = similar_candidates(board, target)
    if not cands:
        result = {"ok": True, "cached": False, "id": mail_id, "count": 0, "results": [],
                  "candidates": 0, "at": stamp(), "model": CFG["gemini_model"]}
        cache["items"][mail_id] = {"at": result["at"], "model": result["model"], "results": []}
        similar_cache_save(uid, cache)
        return result

    picks = gemini_pick_similar(target, cands)
    known = {str(it.get("id")): it for it in cands}
    results = []
    for p in picks:
        src = known.get(p["id"]) or {}
        results.append({**p, "title": str(src.get("title") or ""),
                        "receivedAt": str(src.get("receivedAt") or ""),
                        "sender": str(src.get("senderName") or src.get("sender") or ""),
                        "mailbox": str(src.get("mailbox") or "inbox"),
                        "archived": bool(src.get("archived"))})
    rec = {"at": stamp(), "model": CFG["gemini_model"], "results": results,
           "candidates": len(cands)}
    cache["items"][mail_id] = rec
    similar_cache_save(uid, cache)
    print(f"[similar] {uid} {mail_id} 후보 {len(cands)}건 → AI 선별 {len(results)}건", flush=True)
    return {"ok": True, "cached": False, "id": mail_id, "count": len(results),
            "model": rec["model"], "at": rec["at"], "candidates": len(cands), "results": results}


# ────────────────────────────────────────────────────────────── ✨ 메일 1통 온디맨드 3단 분석
#  ⚪ 기록 보관·타 부서 참조로 AI 분석을 건너뛴 메일을, 사용자가 [✨ AI 요약 실행] 을 눌렀을 때만
#  전체 스레드 + 조직 컨텍스트로 정밀 분석한다. 결과는 board.json 에 저장되어 다시 누르면 0 호출.
ANALYSIS_FIELDS = ("summary", "headline", "background", "mainDept", "deptBasis", "ourDeptAction", "myAction",
                   "threadGist", "actionItems", "replyDrafts", "replyStatus", "dueDate", "deadline",
                   "category", "urgency", "tags")


def analysis_fields(item: dict) -> dict:
    """화면이 그대로 덮어쓸 수 있는 분석 필드만 골라 낸다."""
    out = {k: item.get(k) for k in ANALYSIS_FIELDS if item.get(k) is not None}
    out["aiSkipped"] = bool(item.get("aiSkipped"))
    return out


def mail_analyze(uid: str, mail_id: str, *, force: bool = False) -> dict:
    """메일 1통 정밀 분석.

    force=False — 이미 정밀 분석된 메일이면 저장된 결과를 그대로 돌려준다(0 호출).
    force=True  — [✨ AI 다시 분석] (v1.2.22). 옛 방식으로 요약된 메일도 스레드 캐시를 무시하고
                  본문 전체를 다시 읽어 배경·실질 주담당·핵심 관건을 새로 채운다(Gemini 1회).
    """
    mail_id = str(mail_id or "").strip()
    if not mail_id:
        return {"ok": False, "code": "bad_request", "message": "메일 id 가 필요합니다."}
    path = user_path(uid, "board.json")
    board = read_json(path, None)
    if not isinstance(board, dict):
        return {"ok": False, "code": "no_board",
                "message": "보드를 아직 읽지 못했습니다. 잠시 후 다시 시도해 주세요."}
    item = next((it for it in board.get("items") or []
                 if isinstance(it, dict) and str(it.get("id")) == mail_id), None)
    if item is None:
        return {"ok": False, "code": "not_found", "message": "이 메일을 보드에서 찾지 못했습니다."}
    if not force and not item.get("aiSkipped") and sm.is_detailed_summary(item.get("summary")):
        return {"ok": True, "cached": True, "id": mail_id, "analysis": analysis_fields(item)}
    if not CFG["gemini_key"]:
        return {"ok": False, "code": "no_key",
                "message": "Gemini API 키가 설정되지 않아 AI 분석을 쓸 수 없습니다 — "
                           "서버 .env 에 GEMINI_API_KEY 를 넣고 서버를 다시 시작해 주세요."}

    contacts_list = list((merged_contacts(uid).get("contacts") or []))
    contacts = sm.load_contacts({"contacts": contacts_list})
    mail = {
        "mf_no": str(mail_id).split("-")[-1],
        "subject": item.get("title") or "",
        "bodyText": item.get("analysisBody") or item.get("bodyText") or "",
        "from_raw": item.get("senderName") or item.get("sender") or "",
        "from_email": item.get("senderEmail") or "",
        "to": item.get("to") or [],
        "cc": item.get("cc") or [],
        "date": item.get("receivedAt") or "",
        "attachments": item.get("attachments") or [],
    }
    # v1.2.21 — 수동 정밀 분석도 첨부(.xlsx/.pdf/.docx…) 내용을 열어 본다.
    #   저장된 첨부 요약이 있으면 그대로 쓰고, 없으면 웹메일 세션으로 원문 1통을 다시 읽어 뽑는다.
    if item.get("attachmentText"):
        mail["attachmentText"] = item["attachmentText"]
    fetcher = None
    if not mail.get("attachmentText"):
        try:
            pw = load_cred(uid)
            if pw:
                fetcher = NMAIL_CLASS(uid, pw, ip_check=str(CFG["ip_check"]))
                fetcher.login()
                fresh = fetcher.read_mail(item.get("mailbox") or "inbox", mail["mf_no"])
                if fresh.get("attachments"):
                    mail["attachments"] = fresh["attachments"]
        except Exception as e:  # noqa: BLE001 - 첨부를 못 열면 본문만으로 분석한다
            fetcher = None
            print(f"[analyze] {uid} {mail_id} 첨부 확인 실패: {type(e).__name__}", file=sys.stderr)
    stats: dict[str, Any] = {"gemini_calls": 0, "warnings": []}
    with SYNC_LOCK:                 # sync_mail 의 전역 분류 상태(OWNER·ME)를 동기화와 섞지 않는다
        owner = board.get("owner") or user_meta(uid).get("owner") or {}
        sm.configure(owner, classify_cfg(uid))
        gem = GEMINI_CLASS(CFG["gemini_key"], CFG["gemini_model"])
        analysis = sm.analyze_mail(gem, mail, "받은편지함", contacts,
                                   thread_cache=board.setdefault("thread_cache", {}),
                                   stats=stats, force_deep=True, fetcher=fetcher,
                                   fresh=bool(force))    # v1.2.22 — 다시 분석은 누적 캐시 대신 본문 전체
    if analysis.get("aiSkipped"):     # force_deep 인데도 건너뛴 경우는 오류로 알린다
        raise RuntimeError("AI 분석을 실행하지 못했습니다 (생략 판정)")
    for key in ANALYSIS_FIELDS:
        val = analysis.get(key)
        if val is None:
            continue
        if key in ("actionItems", "replyDrafts"):
            item[key] = val if isinstance(val, (list, dict)) else item.get(key)
        elif val != "":
            item[key] = val
    item["aiSkipped"] = False
    item.pop("aiSkipReason", None)
    item["analyzedAt"] = stamp()
    atomic_write_json(path, board, mode=0o600)
    print(f"[analyze] {uid} {mail_id} 정밀 분석 완료 — Gemini {stats.get('gemini_calls', 0)}회", flush=True)
    return {"ok": True, "cached": False, "id": mail_id, "analysis": analysis_fields(item),
            "model": CFG["gemini_model"], "at": item["analyzedAt"]}


# ────────────────────────────────────────────────────────────── 백그라운드 워커
def worker_tick(now: float | None = None) -> list[str]:
    """도래한 사용자들을 한 번 순회하며 동기화를 시작한다(테스트에서 직접 호출 가능).

    한 번에 max_parallel_syncs 명만 시작하고, 못 시작한 사용자는 60초 뒤 재시도한다
    — 로그인 직후 수집이 밀리지 않도록 로그인·수동 경로는 force 로 따로 들어온다.
    """
    now = time.time() if now is None else now
    started: list[str] = []
    for uid in list_users():
        if now < NEXT_DUE.get(uid, 0):
            continue
        if not os.path.exists(user_path(uid, "cred.enc")):
            NEXT_DUE[uid] = now + CFG["sync_interval"]
            continue
        if needs_relogin(uid):
            NEXT_DUE[uid] = now + CFG["sync_interval"]
            continue
        if start_sync(uid, "auto"):
            NEXT_DUE[uid] = now + CFG["sync_interval"]
            started.append(uid)
        else:
            NEXT_DUE[uid] = now + 60
    return started


def worker_loop(stop: threading.Event) -> None:
    """10분(설정값)마다 등록된 모든 사용자의 새 메일을 수집한다."""
    now = time.time()
    for i, uid in enumerate(list_users()):
        if os.path.exists(user_path(uid, "cred.enc")):
            NEXT_DUE[uid] = now + 15 + i * 8          # 시작 직후 몰리지 않게 살짝 흩뿌린다
    while not stop.is_set():
        worker_tick()
        stop.wait(15)


def start_worker() -> threading.Thread:
    STOP.clear()
    t = threading.Thread(target=worker_loop, args=(STOP,), name="portal-worker", daemon=True)
    t.start()
    return t


# ────────────────────────────────────────────────────────────── 자동 업데이트
#  GitHub(mail-board-template) 의 portal/ 폴더가 유일한 배포본이다.
#  흐름: manifest.json → 파일별 sha256 비교 → 바뀐 파일만 임시 파일로 받아 검사 후 교체.
#  data/·.env·__pycache__ 는 손대지 않고, 실패는 로그만 남기고 다음 주기에 다시 시도한다.
def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    """파일의 sha256(16진 문자열). 없거나 못 읽으면 빈 문자열."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def fetch_remote(url: str, *, timeout: float | None = None) -> bytes:
    """원격 파일 하나를 바이트로 받는다(테스트는 이 함수를 바꿔 끼운다)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": f"ChunilPortal/{VERSION}",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout or UPDATE_TIMEOUT) as res:
        status = int(getattr(res, "status", 200) or 200)
        if status != 200:
            raise OSError(f"HTTP {status}")
        data = res.read(UPDATE_MAX_BYTES + 1)
    if len(data) > UPDATE_MAX_BYTES:
        raise OSError(f"파일이 너무 큽니다({len(data)} bytes)")
    return data


def _loads_json(raw: bytes | str | dict) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8-sig")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("JSON 객체가 아닙니다")
    return obj


def manifest_hashes(manifest: dict) -> dict[str, str]:
    """manifest.json → {상대경로: sha256}. files 값은 {sha256: …} 또는 문자열 둘 다 허용."""
    files = manifest.get("files")
    out: dict[str, str] = {}
    if not isinstance(files, dict):
        return out
    for name, rec in files.items():
        digest = rec.get("sha256") if isinstance(rec, dict) else rec
        if not isinstance(digest, str):
            continue
        digest = digest.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        key = str(name).replace("\\", "/")
        if key.startswith("./"):
            key = key[2:]
        out[key] = digest
    return out


def update_target(name: str) -> str:
    """상대 경로 → UPDATE_ROOT 안의 실제 경로. 폴더 밖(../)으로 나가는 이름은 거부한다."""
    rel = str(name).replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/") or ":" in rel:
        raise ValueError(f"허용되지 않은 경로: {name}")
    root = os.path.realpath(UPDATE_ROOT)
    full = os.path.realpath(os.path.join(root, *rel.split("/")))
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"허용되지 않은 경로: {name}")
    return full


def write_update_file(target: str, data: bytes) -> None:
    """임시 파일로 쓰고 (파이썬이면 py_compile 검사 후) os.replace 로 교체한다.

    검사에서 걸리거나 쓰기가 실패하면 원래 파일은 그대로 남는다 — 서버가 깨진 파일을
    물려받아 못 뜨는 상황을 막는 것이 이 함수의 목적이다.
    """
    folder = os.path.dirname(target) or UPDATE_ROOT
    os.makedirs(folder, exist_ok=True)
    tmp = f"{target}.new.{os.getpid()}"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if target.lower().endswith(".py"):
            pyc = f"{tmp}.pyc"
            try:
                py_compile.compile(tmp, cfile=pyc, doraise=True)
            finally:
                _rm(pyc)
        os.replace(tmp, target)
    finally:
        _rm(tmp)


def _record_update(rep: dict) -> None:
    with UPDATE_LOCK:
        UPDATE_STATE["checks"] = int(UPDATE_STATE.get("checks") or 0) + 1
        UPDATE_STATE["lastCheckAt"] = rep.get("checkedAt") or stamp()
        UPDATE_STATE["lastCheckOk"] = bool(rep.get("ok"))
        UPDATE_STATE["lastError"] = "; ".join(rep.get("errors") or [])[:300]
        UPDATE_STATE["lastChanged"] = list(rep.get("changed") or [])
        UPDATE_STATE["lastApplied"] = list(rep.get("applied") or [])
        if rep.get("remoteVersion"):
            UPDATE_STATE["remoteVersion"] = rep["remoteVersion"]
        if rep.get("restart"):
            UPDATE_STATE["restartRequired"] = True


def check_self_update(*, url: str | None = None, apply: bool = True,
                      restart: bool = False) -> dict:
    """manifest.json 을 확인해 바뀐 파일만 교체한다. 어떤 예외도 밖으로 던지지 않는다.

    돌려주는 값: {ok, url, checkedAt, changed[], applied[], errors[], restart,
                  remoteVersion, upToDate, files}
    · apply=False  : 무엇이 바뀔지만 본다(다운로드·교체 없음)
    · restart=True : 코드(.py) 교체가 있으면 새 프로세스로 갈아탄다
    """
    base = (url or CFG.get("update_url") or UPDATE_URL_DEFAULT).strip().rstrip("/")
    rep: dict[str, Any] = {"ok": False, "url": base, "checkedAt": stamp(), "changed": [],
                           "applied": [], "errors": [], "restart": False,
                           "remoteVersion": "", "upToDate": False, "files": 0}
    try:
        manifest = _loads_json(fetch_remote(f"{base}/manifest.json"))
        remote = manifest_hashes(manifest)
    except Exception as e:  # noqa: BLE001 - 네트워크·GitHub 장애는 로그만 남긴다
        rep["errors"].append(f"manifest.json: {type(e).__name__}: {str(e)[:160]}")
        _record_update(rep)
        return rep
    rep["remoteVersion"] = str(manifest.get("version") or "")
    rep["files"] = len(remote)

    for name in UPDATE_FILES:
        want = remote.get(name)
        if not want:
            continue
        try:
            local = sha256_file(update_target(name))
        except ValueError as e:
            rep["errors"].append(str(e))
            continue
        if local != want:
            rep["changed"].append(name)
    rep["ok"] = True

    if not rep["changed"]:
        rep["upToDate"] = True
        _record_update(rep)
        return rep
    if not apply:
        _record_update(rep)
        return rep

    for name in rep["changed"]:
        want = remote.get(name) or ""
        try:
            target = update_target(name)
            data = fetch_remote(f"{base}/{name}")
            got = sha256_bytes(data)
            if got != want:
                raise ValueError(f"내려받은 파일이 manifest 와 다름({got[:12]}… ≠ {want[:12]}…)")
            write_update_file(target, data)
            rep["applied"].append(name)
            if name == "index.html":
                INDEX_CACHE.update({"mtime": 0.0, "bytes": None})   # 다음 접속부터 새 화면
        except Exception as e:  # noqa: BLE001 - 한 파일 실패가 나머지를 막지 않는다
            rep["errors"].append(f"{name}: {type(e).__name__}: {str(e)[:200]}")

    # 교체된 .py 는 메모리에 이미 올라간 옛 모듈을 쓰고 있으므로 재시작이 필요하다
    rep["restart"] = any(n.endswith(".py") for n in rep["applied"])
    _record_update(rep)
    if rep["restart"] and restart:
        restart_self()
    return rep


def _no_window_creationflags() -> int:
    """Windows 에서 불필요한 콘솔 창을 띄우지 않는 플래그(콘솔에서 띄운 서버는 그대로)."""
    if os.name != "nt":
        return 0
    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleWindow():   # 지금 창이 있으면 콘솔을 물려준다
            return 0
    except Exception:  # noqa: BLE001 - ctypes 실패는 무시하고 창 없는 실행으로
        pass
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))


def restart_self(*, delay: float = 0.0) -> bool:
    """새 프로세스(같은 파이썬·같은 인자)를 띄우고 현재 HTTP 서버를 내린다.

    실패하면 로그만 남기고 지금 서버로 계속 돈다(업데이트는 다음 주기에 다시).
    새 프로세스는 PORTAL_RESTART_HANDOFF=1 로 떠서, 옛 프로세스가 포트를 놓을 때까지
    잠깐 기다렸다가 bind 한다 — 그래서 사용자는 잠깐의 연결 실패만 겪는다.
    """
    global _RESTARTING
    if _RESTARTING:
        return False
    _RESTARTING = True
    argv = [sys.executable or "python"] + list(sys.argv)
    env = dict(os.environ)
    env["PORTAL_RESTART_HANDOFF"] = "1"
    kwargs: dict[str, Any] = {"cwd": UPDATE_ROOT, "env": env, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = _no_window_creationflags()
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except Exception as e:  # noqa: BLE001 - 재시작 실패가 서버를 죽이면 안 된다
        print(f"[update] 새 프로세스를 띄우지 못했습니다 — 이 서버로 계속 돕니다: "
              f"{type(e).__name__}: {str(e)[:200]}", file=sys.stderr, flush=True)
        _RESTARTING = False
        with UPDATE_LOCK:
            UPDATE_STATE["restartRequired"] = False
            UPDATE_STATE["lastError"] = f"restart: {type(e).__name__}: {str(e)[:160]}"
        return False
    with UPDATE_LOCK:
        UPDATE_STATE["restartRequired"] = False
    print(f"[update] 새 프로세스 시작(pid={proc.pid}) — 옛 서버를 내립니다", flush=True)
    threading.Thread(target=_handoff_shutdown, args=(delay,), name="portal-restart",
                     daemon=True).start()
    return True


def _handoff_shutdown(delay: float = 0.0) -> None:
    """새 프로세스가 뜰 시간을 조금 준 뒤 serve_forever 를 멈춘다(메인 스레드가 종료)."""
    time.sleep(max(0.0, delay))
    STOP.set()
    httpd = HTTPD
    if httpd is not None:
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass


def public_update_state() -> dict:
    """화면·/api/health 에 내려줄 자동 업데이트 상태(비밀 없음)."""
    with UPDATE_LOCK:
        st = dict(UPDATE_STATE)
    st.update({"enabled": bool(CFG.get("auto_update")), "version": VERSION,
               "url": CFG.get("update_url") if CFG.get("auto_update") else "",
               "interval": int(CFG.get("update_interval") or UPDATE_INTERVAL_SEC)})
    return st


def update_loop(stop: threading.Event) -> None:
    """PORTAL_UPDATE_INTERVAL(기본 5분)마다 manifest 를 확인한다. 첫 확인은 20초 뒤."""
    interval = max(60, int(CFG.get("update_interval") or UPDATE_INTERVAL_SEC))
    stop.wait(20)
    while not stop.is_set():
        with UPDATE_LOCK:
            UPDATE_STATE["nextCheckAt"] = time.strftime("%H:%M:%S",
                                                        time.localtime(time.time() + interval))
        if CFG.get("auto_update"):
            try:
                if UPDATE_STATE.get("restartRequired") and not RUNNING:
                    if restart_self():
                        return
                rep = check_self_update(restart=False)
                _log_update(rep)
                if rep.get("restart") and not RUNNING and not STOP.is_set():
                    if restart_self():
                        return
            except Exception as e:  # noqa: BLE001 - 업데이트 실패가 서버를 멈추면 안 된다
                print(f"[update] 경고: {type(e).__name__}: {str(e)[:200]}",
                      file=sys.stderr, flush=True)
        stop.wait(interval)


def _log_update(rep: dict) -> None:
    ver = f" · 원격 v{rep['remoteVersion']}" if rep.get("remoteVersion") else ""
    if rep.get("applied"):
        print(f"[update] 적용 {', '.join(rep['applied'])}{ver}", flush=True)
    if rep.get("errors"):
        print(f"[update] 건너뜀/실패 — {'; '.join(rep['errors'])[:300]}{ver}",
              file=sys.stderr, flush=True)


def start_update_worker() -> threading.Thread:
    t = threading.Thread(target=update_loop, args=(STOP,), name="portal-update", daemon=True)
    t.start()
    return t


# ────────────────────────────────────────────────────────────── 정적 파일
def render_index() -> bytes:
    """index.html 을 그대로 내보내되, 맨 앞에 포털 모드 플래그만 넣는다(중앙 업데이트 유지)."""
    path = os.path.join(HERE, "index.html")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return "<!doctype html><meta charset='utf-8'><h1>index.html 이 없습니다</h1>".encode("utf-8")
    if INDEX_CACHE["mtime"] != mtime or not INDEX_CACHE["bytes"]:
        with open(path, "rb") as f:
            raw = f.read()
        flag = (b"<script>window.__PORTAL_MODE__=true;window.__PORTAL_VERSION__="
                + json.dumps(VERSION).encode("utf-8") + b";</script>")
        i = raw.lower().find(b"<head>")
        raw = (raw[: i + 6] + flag + raw[i + 6:]) if i >= 0 else (flag + raw)
        INDEX_CACHE.update({"mtime": mtime, "bytes": raw})
    return INDEX_CACHE["bytes"]


def guess_type(path: str) -> str:
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json",
                                              "image/svg+xml"):
        ctype += "; charset=utf-8"
    return ctype


# ────────────────────────────────────────────────────────────── 📎 첨부 사진 중계 (v1.2.17)
#  상세 화면의 사진 첨부 썸네일은 브라우저가 웹메일 쿠키를 갖고 있지 않아 직접 받을 수 없다.
#  그래서 서버가 로그인한 사용자의 웹메일 세션으로 바이트만 받아 그대로 흘려보낸다.
#  · 주소는 mail.chunil.co.kr 의 첨부 내려받기 경로만(그 밖은 403)
#  · 응답은 사진(image/*)만, 5MB 까지
ATTACH_MAX_BYTES = 5 * 1024 * 1024
ATTACH_HOSTS = ("mail.chunil.co.kr",)
ATTACH_PATHS = ("/mail_attach_down.php", "/include/download.php")
ATTACH_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/x-ms-bmp")
ATTACH_TTL = 600
ATTACH_CACHE: dict[str, tuple[Any, float]] = {}
ATTACH_LOCK = threading.Lock()


def attachment_url_ok(raw: str) -> bool:
    """중계해도 되는 첨부 주소인지 — 사내 웹메일 호스트 + 첨부 내려받기 경로만."""
    try:
        p = urlsplit(raw)
        port = p.port
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    if (p.hostname or "").lower() not in ATTACH_HOSTS:
        return False
    if port not in (None, 80, 443):
        return False
    return p.path in ATTACH_PATHS


def sniff_image_type(data: bytes) -> str:
    """헤더 Content-Type 이 없거나 octet-stream 일 때 파일 시그니처로 사진인지 본다."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return ""


def attachment_session(uid: str):
    """첨부 중계용 웹메일 세션 — 10분 재사용해 사진마다 로그인하지 않는다."""
    now = time.time()
    with ATTACH_LOCK:
        hit = ATTACH_CACHE.get(uid)
        if hit and now - hit[1] < ATTACH_TTL:
            return hit[0]
    password = load_cred(uid)
    if not password:
        raise PermissionError("cred")
    with SYNC_LOCK:                       # 웹메일 로그인은 sync 와 같은 규칙으로 한 번에 하나
        nm = NMAIL_CLASS(uid, password, ip_check=str(CFG["ip_check"]))
        nm.login()
    with ATTACH_LOCK:
        ATTACH_CACHE[uid] = (nm, time.time())
    return nm


def fetch_attachment(uid: str, url: str) -> tuple[bytes, str]:
    """웹메일의 첨부 바이트를 가져온다 → (bytes, content_type). 실패는 예외로 알린다."""
    nm = attachment_session(uid)
    try:
        r = nm.s.get(url, timeout=20, allow_redirects=False)
    except Exception as e:  # noqa: BLE001 - 네트워크 오류는 502 로 안내
        raise RuntimeError(f"fetch:{type(e).__name__}") from e
    if getattr(r, "status_code", 0) != 200:
        # 세션이 만료됐을 수 있으니 다음 요청은 새로 로그인하게 한다
        with ATTACH_LOCK:
            ATTACH_CACHE.pop(uid, None)
        raise LookupError("status")
    ctype = str((getattr(r, "headers", {}) or {}).get("Content-Type") or "").split(";")[0].strip().lower()
    data = r.content or b""
    if not data:
        raise LookupError("empty")
    if len(data) > ATTACH_MAX_BYTES:
        raise ValueError("too-big")
    if ctype not in ATTACH_TYPES:
        ctype = sniff_image_type(data)
        if not ctype:
            raise TypeError("not-image")
    return data, ctype


# ────────────────────────────────────────────────────────────── HTTP
class PortalHandler(BaseHTTPRequestHandler):
    server_version = f"ChunilPortal/{VERSION}"
    protocol_version = "HTTP/1.1"

    # -- 도우미 ---------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        sys.stderr.write("%s %s\n" % (stamp("%H:%M:%S"), fmt % args))

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _sid(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        if not raw:
            return None
        c = SimpleCookie()
        try:
            c.load(raw)
        except Exception:  # noqa: BLE001
            return None
        m = c.get(COOKIE_NAME)
        return m.value if m else None

    def _session(self) -> dict | None:
        return get_session(self._sid())

    def _send(self, code: int, body: bytes, ctype: str, *, extra: dict | None = None,
              cache: str = "no-store", gzip_ok: bool = True) -> None:
        hdrs = {"Content-Type": ctype, "Cache-Control": cache, **SECURITY_HEADERS}
        if extra:
            hdrs.update(extra)
        accept = (self.headers.get("Accept-Encoding") or "").lower()
        if gzip_ok and "gzip" in accept and len(body) > 1024 and not ctype.startswith("image/"):
            body = gzip.compress(body, 6)
            hdrs["Content-Encoding"] = "gzip"
        if gzip_ok and not ctype.startswith("image/"):
            # 같은 URL 이 압축본/평문 두 가지로 내려갈 수 있음을 중간 캐시에 알린다
            hdrs["Vary"] = "Accept-Encoding"
        hdrs["Content-Length"] = str(len(body))
        self.send_response(code)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _not_modified(self, etag: str) -> None:
        """내용이 그대로면 본문 없이 304 — 1.2MB 대신 헤더 몇 줄만 오간다."""
        self.send_response(HTTPStatus.NOT_MODIFIED)
        for k, v in {"ETag": etag, "Vary": "Accept-Encoding", "Cache-Control": "no-cache",
                     **SECURITY_HEADERS}.items():
            self.send_header(k, v)
        self.end_headers()

    def _if_none_match(self, etag: str) -> bool:
        """If-None-Match 가 이 응답의 ETag 와 같은지(와일드카드 포함)."""
        raw = (self.headers.get("If-None-Match") or "").strip()
        if not raw:
            return False
        return raw == "*" or etag in [x.strip() for x in raw.split(",")]

    def _json(self, code: int, obj: Any, **kw: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", **kw)

    def _err(self, status: int, message: str, **extra: Any) -> None:
        # extra 의 code= 는 응답 본문의 기계용 사유 코드(HTTP status 와 다름) — 첫 인자 이름과 겹치지 않게 status 로 받는다
        self._json(status, {"ok": False, "message": message, **extra})

    def _read_body_raw(self) -> bytes:
        """요청 본문을 끝까지 읽는다 — 읽지 않고 남기면 keep-alive 연결에서 다음 요청으로 오인된다."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > 2_000_000:
            self.close_connection = True
            raise ValueError("too large")
        return self.rfile.read(length)

    def _body_json(self, limit: int = 64_000) -> dict:
        raw = getattr(self, "_raw_body", b"") or b""
        if len(raw) > limit:
            raise ValueError("too large")
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
        return obj if isinstance(obj, dict) else {}

    def _same_origin_ok(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        host = (self.headers.get("Host") or "").strip()
        try:
            o = urlsplit(origin)
        except ValueError:
            return False
        return bool(host) and o.netloc == host

    # -- 라우팅 ---------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        self._raw_body = b""
        try:
            if path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, render_index(), "text/html; charset=utf-8", cache="no-cache")
                return
            if path == "/favicon.ico":
                self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
                return
            if path == "/api/health":
                self._json(HTTPStatus.OK, {"ok": True, "app": APP_NAME, "version": VERSION,
                                           "users": len(list_users()),
                                           "poll": CFG["sync_interval"],
                                           "autoUpdate": bool(CFG["auto_update"]),
                                           "update": public_update_state()})
                return
            if path == "/api/me":
                s = self._session()
                if not s:
                    self._json(HTTPStatus.OK, {"ok": True, "loggedIn": False, "version": VERSION})
                    return
                uid = s["user"]
                meta = user_meta(uid)
                owner = meta.get("owner") or {}
                if not owner:
                    board = read_json(user_path(uid, "board.json"), {}) or {}
                    owner = board.get("owner") or {}
                self._json(HTTPStatus.OK, {"ok": True, "loggedIn": True, "id": uid,
                                           "owner": owner, "status": public_status(uid),
                                           "version": VERSION})
                return
            if path == "/api/status":
                uid = self._require_user()
                if not uid:
                    return
                self._json(HTTPStatus.OK, public_status(uid))
                return
            if path == "/api/data":
                self._api_data()
                return
            if path == "/api/contacts":
                uid = self._require_user()
                if not uid:
                    return
                etag = contacts_etag(uid)
                if self._if_none_match(etag):
                    self._not_modified(etag)
                    return
                self._json(HTTPStatus.OK, merged_contacts(uid), extra={"ETag": etag})
                return
            if path == "/api/state":
                uid = self._require_user()
                if not uid:
                    return
                self._json(HTTPStatus.OK, {"ok": True, **load_state(uid)})
                return
            if path == "/api/lock":
                # 🔒 화면 잠금 상태·PIN 유무 — PIN 해시는 절대 내려보내지 않는다
                uid = self._require_user()
                if not uid:
                    return
                self._json(HTTPStatus.OK, {"ok": True, **lock_info(uid)})
                return
            if path == "/api/attachment":
                self._api_attachment()
                return
            if path.startswith("/api/"):
                self._err(HTTPStatus.NOT_FOUND, "없는 API 입니다")
                return
            self._serve_static(path.lstrip("/"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            print(f"[http] GET {path} 오류: {type(e).__name__}: {e}", file=sys.stderr)
            self._err(HTTPStatus.INTERNAL_SERVER_ERROR, "서버 오류가 발생했습니다")

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        self._raw_body = b""
        try:
            self._raw_body = self._read_body_raw()
            if not self._same_origin_ok():
                self._err(HTTPStatus.FORBIDDEN, "허용되지 않은 요청입니다")
                return
            if path == "/api/login":
                self._api_login()
                return
            if path == "/api/logout":
                s = self._session()
                sid = self._sid()
                drop_session(sid)
                if s and s.get("user"):
                    try:
                        lock_end_session(s["user"])     # 로그아웃하면 화면 잠금 표시도 끝난다
                    except Exception:  # noqa: BLE001 - 로그아웃은 반드시 되어야 한다
                        pass
                self._json(HTTPStatus.OK, {"ok": True},
                           extra={"Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
                return
            if path == "/api/sync":
                uid = self._require_user()
                if not uid:
                    return
                started = start_sync(uid, "manual", force=True)
                self._json(HTTPStatus.OK, {"ok": True, "started": started, "status": public_status(uid)})
                return
            if path == "/api/similar":
                self._api_similar()
                return
            if path == "/api/mail/analyze":
                self._api_mail_analyze()
                return
            if path == "/api/state":
                uid = self._require_user()
                if not uid:
                    return
                self._api_state(uid)
                return
            if path == "/api/lock":
                uid = self._require_user()
                if not uid:
                    return
                self._api_lock(uid)
                return
            self._err(HTTPStatus.NOT_FOUND, "없는 API 입니다")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError:
            self._err(HTTPStatus.BAD_REQUEST, "요청 형식이 올바르지 않습니다")
        except Exception as e:  # noqa: BLE001
            print(f"[http] POST {path} 오류: {type(e).__name__}: {e}", file=sys.stderr)
            self._err(HTTPStatus.INTERNAL_SERVER_ERROR, "서버 오류가 발생했습니다")

    # -- API 구현 -------------------------------------------------------
    def _require_user(self) -> str | None:
        s = self._session()
        if not s:
            self._err(HTTPStatus.UNAUTHORIZED, "로그인이 필요합니다", login=True)
            return None
        return s["user"]

    def _api_attachment(self) -> None:
        """📎 사진 첨부 중계 — 로그인 세션 + 사내 웹메일 주소만, 사진만, 5MB 까지."""
        uid = self._require_user()
        if not uid:
            return
        qs = parse_qs(urlsplit(self.path).query)
        url = (qs.get("url") or [""])[0].strip()
        if not url:
            self._err(HTTPStatus.BAD_REQUEST, "첨부 주소가 없습니다", code="no_url")
            return
        if not attachment_url_ok(url):
            self._err(HTTPStatus.FORBIDDEN, "허용되지 않은 첨부 주소입니다", code="bad_url")
            return
        try:
            data, ctype = fetch_attachment(uid, url)
        except PermissionError:
            self._err(HTTPStatus.UNAUTHORIZED, "웹메일 비밀번호가 없습니다 — 다시 로그인해 주세요", login=True)
            return
        except LookupError:
            self._err(HTTPStatus.NOT_FOUND, "첨부 사진을 찾지 못했습니다", code="not_found")
            return
        except ValueError:
            self._err(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "첨부 사진이 너무 큽니다(5MB 까지)", code="too_big")
            return
        except TypeError:
            self._err(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "사진 형식이 아닙니다", code="not_image")
            return
        except RuntimeError:
            self._err(HTTPStatus.BAD_GATEWAY, "웹메일에서 사진을 받지 못했습니다", code="upstream")
            return
        self._send(HTTPStatus.OK, data, ctype, cache="private, max-age=600")

    def _api_login(self) -> None:
        body = self._body_json(limit=8192)
        uid = normalize_id(body.get("id"))
        pw = str(body.get("password") or "")
        remember = bool(body.get("remember"))
        if not uid:
            self._err(HTTPStatus.BAD_REQUEST, "사내 메일 아이디를 확인해 주세요 (예: honggildong)")
            return
        if not pw:
            self._err(HTTPStatus.BAD_REQUEST, "비밀번호를 입력해 주세요")
            return
        ip = self._client_ip()
        key = f"{ip}|{uid}"
        if login_blocked(key):
            self._err(HTTPStatus.TOO_MANY_REQUESTS,
                      "로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요")
            return
        try:
            nm = NMAIL_CLASS(uid, pw, ip_check=str(CFG["ip_check"]))
            nm.login()
        except sm.NmailError as e:
            record_login_fail(key)
            msg = "아이디 또는 비밀번호가 맞지 않습니다."
            if "IP" in str(e) or "보안" in str(e):
                msg = ("웹메일이 이 서버의 접속 IP를 막았을 수 있습니다. "
                       "전산 담당자에게 IP 보안접속 허용을 요청해 주세요.")
            self._err(HTTPStatus.UNAUTHORIZED, msg)
            print(f"[login] {uid} 실패: {str(e)[:200]}", file=sys.stderr)
            return
        except Exception as e:  # noqa: BLE001
            self._err(HTTPStatus.BAD_GATEWAY, "웹메일 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요")
            print(f"[login] {uid} 연결 오류: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            return
        clear_login_fails(key)

        os.makedirs(user_dir(uid), exist_ok=True)
        try:
            save_cred(uid, pw)
        except OSError as e:
            print(f"[login] {uid} 비밀번호 보관 실패: {e}", file=sys.stderr)
        meta = user_meta(uid)
        update_user_meta(uid, needsRelogin=False,
                         firstLoginAt=meta.get("firstLoginAt") or stamp(),
                         lastLogin=stamp(), email=f"{uid}@{sm.DOMAIN}")
        sid = create_session(uid, remember)
        lock_end_session(uid)      # 🔒 새로 로그인했으면 잠금 표시를 푼다(간편 PIN 해시는 그대로 둔다)
        board = read_json(user_path(uid, "board.json"), None)
        pending = not (isinstance(board, dict) and board.get("items"))
        start_sync(uid, "seed" if pending else "login", force=True)
        cookie = f"{COOKIE_NAME}={sid}; Path=/; HttpOnly; SameSite=Lax"
        if remember:
            cookie += f"; Max-Age={CFG['remember_days'] * 86400}"
        self._json(HTTPStatus.OK, {"ok": True, "id": uid, "pending": pending,
                                   "owner": user_meta(uid).get("owner") or {},
                                   "status": public_status(uid)},
                   extra={"Set-Cookie": cookie})
        print(f"[login] {uid} 성공 — {'최초 수집 시작' if pending else '보드 갱신 시작'}", flush=True)

    def _api_data(self) -> None:
        uid = self._require_user()
        if not uid:
            return
        path = user_path(uid, "board.json")
        try:
            st = os.stat(path)
        except OSError:
            self._json(HTTPStatus.ACCEPTED, {"ok": False, "pending": True, "status": public_status(uid)})
            return
        mtime_ns, size = st.st_mtime_ns, st.st_size
        etag = '"%x-%x"' % (mtime_ns, size)
        if self._if_none_match(etag):           # 바뀐 게 없으면 파일도 압축도 하지 않는다
            self._not_modified(etag)
            return
        extra = {"X-Portal-User": uid, "ETag": etag}
        accept = (self.headers.get("Accept-Encoding") or "").lower()
        gz = None
        if "gzip" in accept:
            with GZ_LOCK:
                hit = GZ_CACHE.get(path)
                if hit and hit[0] == mtime_ns and hit[1] == size:
                    gz = hit[2]
            if gz is not None:                  # 지난번에 만든 압축본 재사용 (11MB → 1.2MB)
                self._send(HTTPStatus.OK, gz, "application/json; charset=utf-8",
                           extra={**extra, "Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
                           gzip_ok=False)
                return
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._json(HTTPStatus.ACCEPTED, {"ok": False, "pending": True, "status": public_status(uid)})
            return
        if not body.strip().startswith(b"{"):
            self._json(HTTPStatus.ACCEPTED, {"ok": False, "pending": True, "status": public_status(uid)})
            return
        if gz is None and "gzip" in accept:
            gz = gzip.compress(body, 6)
            with GZ_LOCK:
                if len(GZ_CACHE) >= GZ_CACHE_MAX:
                    for old in list(GZ_CACHE)[:max(1, GZ_CACHE_MAX // 4)]:
                        GZ_CACHE.pop(old, None)
                GZ_CACHE[path] = [mtime_ns, size, gz, etag]
            self._send(HTTPStatus.OK, gz, "application/json; charset=utf-8",
                       extra={**extra, "Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
                       gzip_ok=False)
            return
        self._send(HTTPStatus.OK, body, "application/json; charset=utf-8", extra=extra)

    def _api_similar(self) -> None:
        """✨ AI 정밀 분석 — 버튼을 눌렀을 때만 한 번 돌고, 결과는 메일별로 캐시된다."""
        uid = self._require_user()
        if not uid:
            return
        try:
            body = self._body_json(limit=8192)
        except ValueError:
            self._err(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "요청이 너무 큽니다")
            return
        mail_id = str(body.get("id") or "").strip()
        if not mail_id:
            self._err(HTTPStatus.BAD_REQUEST, "메일 id 가 필요합니다.")
            return
        try:
            result = similar_analyze(uid, mail_id)
        except Exception as e:  # noqa: BLE001 - Gemini·네트워크 실패는 재시도할 수 있게 알려 준다
            print(f"[similar] {uid} {mail_id} 실패: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
            self._json(HTTPStatus.BAD_GATEWAY,
                       {"ok": False, "code": "ai_failed",
                        "message": similar_error_message(e)})
            return
        code = HTTPStatus.OK
        if not result.get("ok"):
            code = {"not_found": HTTPStatus.NOT_FOUND,
                    "bad_request": HTTPStatus.BAD_REQUEST,
                    "no_key": HTTPStatus.NOT_IMPLEMENTED}.get(str(result.get("code")), HTTPStatus.BAD_REQUEST)
        self._json(code, result)
        print(f"[similar] {uid} {mail_id} → {result.get('count', 0)}건"
              f"{' (캐시)' if result.get('cached') else ''}", flush=True)

    def _api_mail_analyze(self) -> None:
        """✨ AI 요약 실행 / ✨ AI 다시 분석 — 메일 1통을 정밀 분석한다(결과는 보드에 저장).

        body: {"id": "m-…", "force": true|false}
          force=true 면 이미 분석된 메일도 새 기준으로 다시 분석한다(v1.2.22).
        """
        uid = self._require_user()
        if not uid:
            return
        try:
            body = self._body_json(limit=8192)
        except ValueError:
            self._err(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "요청이 너무 큽니다")
            return
        try:
            result = mail_analyze(uid, str(body.get("id") or ""), force=bool(body.get("force")))
        except Exception as e:  # noqa: BLE001 - Gemini·네트워크 실패는 다시 시도할 수 있게 알려 준다
            print(f"[analyze] {uid} 실패: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
            self._json(HTTPStatus.BAD_GATEWAY,
                       {"ok": False, "code": "ai_failed",
                        "message": similar_error_message(e)})
            return
        code = HTTPStatus.OK
        if not result.get("ok"):
            code = {"not_found": HTTPStatus.NOT_FOUND,
                    "bad_request": HTTPStatus.BAD_REQUEST,
                    "no_key": HTTPStatus.NOT_IMPLEMENTED,
                    "no_board": HTTPStatus.CONFLICT}.get(str(result.get("code")), HTTPStatus.BAD_REQUEST)
        self._json(code, result)

    def _api_lock(self, uid: str) -> None:
        """🔒 화면 잠금 — PIN 설정·검증·변경·잠금 상태.

        PIN 평문은 저장도 응답도 하지 않는다(PBKDF2-SHA256 해시만). 화면은 이 응답의
        hasPin·locked 만 보고, 실제 대조는 서버 안에서 한다.
        """
        try:
            body = self._body_json(limit=4096)
        except ValueError:
            body = {}
        action = str(body.get("action") or "info").strip().lower()
        if action in ("", "info"):
            self._json(HTTPStatus.OK, {"ok": True, **lock_info(uid)})
            return
        if action == "lock":
            self._json(HTTPStatus.OK, {"ok": True, **lock_set_state(uid, True)})
            return
        if action == "unlock":
            pin = str(body.get("pin") or "").strip()
            if not LOCK_PIN_RE.match(pin):
                self._err(HTTPStatus.BAD_REQUEST, "4자리 숫자 PIN을 입력해 주세요", code="bad_pin")
                return
            if not lock_info(uid)["hasPin"]:
                self._err(HTTPStatus.CONFLICT, "아직 잠금 PIN이 없습니다 — 먼저 PIN을 설정해 주세요", code="needs_set")
                return
            if lock_fail_count(uid) >= LOCK_FAIL_MAX:
                self._err(HTTPStatus.TOO_MANY_REQUESTS,
                          "PIN을 여러 번 틀렸습니다. 로그아웃 후 다시 로그인해 주세요", code="too_many")
                return
            if not lock_check_pin(uid, pin):
                lock_record_fail(uid)
                self._err(HTTPStatus.FORBIDDEN, "PIN이 일치하지 않습니다", code="mismatch")
                return
            self._json(HTTPStatus.OK, {"ok": True, **lock_set_state(uid, False)})
            return
        if action in ("set", "change"):
            raw = body.get("newPin") if action == "change" else body.get("pin")
            if raw is None:
                raw = body.get("pin")
            pin = str(raw or "").strip()
            if not LOCK_PIN_RE.match(pin):
                self._err(HTTPStatus.BAD_REQUEST, "4자리 숫자 PIN을 입력해 주세요", code="bad_pin")
                return
            if action == "set":
                if not lock_set_pin(uid, pin):
                    self._err(HTTPStatus.CONFLICT,
                              "이미 잠금 PIN이 있습니다 — [🔑 잠금 PIN 변경]을 써 주세요", code="exists")
                    return
            else:
                lock_set_pin(uid, pin, replace=True)
            self._json(HTTPStatus.OK, {"ok": True, **lock_info(uid)})
            return
        self._err(HTTPStatus.BAD_REQUEST, "알 수 없는 잠금 요청입니다", code="bad_action")

    def _api_state(self, uid: str) -> None:
        try:
            body = self._body_json(limit=MAX_STATE_BYTES)
        except ValueError:
            self._err(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "상태 데이터가 너무 큽니다")
            return
        incoming = body.get("sections")
        if not isinstance(incoming, dict):
            self._err(HTTPStatus.BAD_REQUEST, "sections 가 필요합니다")
            return
        state = load_state(uid)
        sections = state.get("sections") or {}
        changed = 0
        for name, rec in incoming.items():
            if not isinstance(rec, dict) or not STATE_NAME_RE.match(str(name)):
                continue
            try:
                t = int(rec.get("t") or 0)
            except (TypeError, ValueError):
                t = 0
            raw = rec.get("raw")
            if raw is not None and not isinstance(raw, str):
                continue
            if len(raw or "") > 200_000:
                continue
            cur = sections.get(name) or {}
            if t >= int(cur.get("t") or 0):
                sections[name] = {"t": t, "raw": raw}
                changed += 1
        if changed:
            # 🔒 lock(화면 잠금 PIN 해시·잠금 여부)는 이 요청이 건드리는 값이 아니므로 그대로 보존한다
            prev = read_json(state_path(uid), {}) or {}
            state = dict(prev) if isinstance(prev, dict) else {}
            state["sections"] = sections
            state["updatedAt"] = stamp()
            atomic_write_json(state_path(uid), state, mode=0o600)
        self._json(HTTPStatus.OK, {"ok": True, **load_state(uid)})

    def _serve_static(self, rel: str) -> None:
        rel = rel.replace("\\", "/")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts) or parts[0].startswith("."):
            self._err(HTTPStatus.NOT_FOUND, "찾을 수 없습니다")
            return
        if parts[0] not in ("assets",):
            self._err(HTTPStatus.NOT_FOUND, "찾을 수 없습니다")
            return
        full = os.path.realpath(os.path.join(HERE, *parts))
        root = os.path.realpath(HERE) + os.sep
        if not full.startswith(root) or not os.path.isfile(full):
            self._err(HTTPStatus.NOT_FOUND, "찾을 수 없습니다")
            return
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            self._err(HTTPStatus.NOT_FOUND, "찾을 수 없습니다")
            return
        self._send(HTTPStatus.OK, body, guess_type(full), cache="public, max-age=86400")



def board_rules(uid: str) -> dict:
    """이 사용자가 화면(⚙️ 분류 규칙)에서 저장한 규칙 — /api/state sections.rules(JSON 문자열).

    화면의 mb_rules_v1 과 같은 내용이고, 수집·AI 생략 판정에도 같은 순서로 적용한다(v1.2.12).
    """
    rec = (load_state(uid).get("sections") or {}).get("rules") or {}
    raw = rec.get("raw") if isinstance(rec, dict) else None
    if not raw:
        return {}
    try:
        got = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def classify_cfg(uid: str) -> dict:
    """sync_mail.configure 에 넘길 분류 설정 — board_config.json + 이 사용자의 규칙."""
    cfg = dict(sm.load_board_config(CFG["board_config"]))
    cfg["rules"] = board_rules(uid)      # 규칙이 없으면 빈 규칙으로 되돌린다(다른 사용자 규칙이 남지 않게)
    return cfg


def state_path(uid: str) -> str:
    return user_path(uid, "state.json")


def contacts_etag(uid: str) -> str:
    """주소록 응답의 ETag — 공용 조직도·개인주소록 파일이 바뀔 때만 달라진다."""
    def mt(p: str) -> int:
        try:
            return os.stat(p).st_mtime_ns
        except OSError:
            return 0
    return '"%x-%x"' % (mt(shared_org_path()), mt(user_path(uid, "personal_contacts.json")))


def load_state(uid: str) -> dict:
    rec = read_json(state_path(uid), {}) or {}
    sections = rec.get("sections") if isinstance(rec, dict) else None
    return {"sections": sections if isinstance(sections, dict) else {},
            "updatedAt": (rec.get("updatedAt") if isinstance(rec, dict) else "") or ""}


# ────────────────────────────────────────────────────────────── 🔒 화면 잠금 (v1.2.16)
#  자리를 비울 때 화면을 덮고 4자리 PIN 으로 바로 푸는 기능.
#  state.json 의 `lock` 에 PBKDF2-SHA256(솔트 + 12만 회) 해시로만 남긴다 — 평문·복호화 가능한 값은 저장하지 않는다.
#  잠금 여부(locked)도 같은 곳에 두어, 화면이 F5·다른 기기에서 상태를 확인할 수 있다.
LOCK_ITER = 120_000
LOCK_PIN_RE = re.compile(r"^[0-9]{4}$")
_LOCK_FAILS: dict[str, list[float]] = {}      # uid → 최근 실패 시각들(메모리, 재시작하면 비운다)
LOCK_FAIL_WINDOW = 10 * 60                     # 초 — 이 시간 안의 실패만 센다
LOCK_FAIL_MAX = 10                             # 10회를 넘으면 잠시 막는다(서버 경유 무차별 대입 방지)


def lock_record(uid: str) -> dict:
    rec = read_json(state_path(uid), {}) or {}
    got = rec.get("lock") if isinstance(rec, dict) else None
    return got if isinstance(got, dict) else {}


def lock_info(uid: str) -> dict:
    """화면에 내려도 되는 잠금 정보 — 해시·솔트는 넣지 않는다."""
    got = lock_record(uid)
    return {"hasPin": bool(got.get("hash") and got.get("salt")),
            "locked": bool(got.get("locked")),
            "updatedAt": str(got.get("updatedAt") or ""),
            "lockedAt": str(got.get("lockedAt") or "")}


def _lock_write(uid: str, patch: dict) -> dict:
    """state.json 의 lock 만 갱신한다 — sections(확인·고정·캘린더·규칙)는 건드리지 않는다."""
    rec = read_json(state_path(uid), {}) or {}
    if not isinstance(rec, dict):
        rec = {}
    got = rec.get("lock") if isinstance(rec.get("lock"), dict) else {}
    got.update(patch)
    rec["lock"] = got
    atomic_write_json(state_path(uid), rec, mode=0o600)
    return got


def lock_hash(pin: str, salt_hex: str = "", iterations: int = LOCK_ITER) -> tuple[str, str, int]:
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), salt, int(iterations), dklen=32)
    return digest.hex(), salt.hex(), int(iterations)


def lock_set_pin(uid: str, pin: str, *, replace: bool = False) -> bool:
    """4자리 PIN 저장. 이미 있으면 replace=False 일 때 덮어쓰지 않고 False."""
    got = lock_record(uid)
    if got.get("hash") and not replace:
        return False
    digest, salt, iterations = lock_hash(pin)
    _lock_write(uid, {"hash": digest, "salt": salt, "iter": iterations, "updatedAt": stamp()})
    _LOCK_FAILS.pop(uid, None)
    return True


def lock_check_pin(uid: str, pin: str) -> bool:
    got = lock_record(uid)
    if not got.get("hash") or not got.get("salt"):
        return False
    try:
        iterations = int(got.get("iter") or LOCK_ITER)
    except (TypeError, ValueError):
        iterations = LOCK_ITER
    digest, _, _ = lock_hash(pin, str(got.get("salt")), iterations)
    return hmac.compare_digest(digest, str(got.get("hash") or ""))


def lock_set_state(uid: str, locked: bool) -> dict:
    patch: dict[str, Any] = {"locked": bool(locked)}
    patch["lockedAt" if locked else "unlockedAt"] = stamp()
    _lock_write(uid, patch)
    if not locked:
        _LOCK_FAILS.pop(uid, None)
    return lock_info(uid)


def lock_end_session(uid: str) -> None:
    """전체 비밀번호로 새로 로그인·로그아웃했으면 화면 잠금 표시를 푼다(PIN 해시는 그대로)."""
    if lock_record(uid).get("locked"):
        lock_set_state(uid, False)


def lock_fail_count(uid: str) -> int:
    now = time.time()
    rows = [t for t in _LOCK_FAILS.get(uid, []) if now - t < LOCK_FAIL_WINDOW]
    if rows:
        _LOCK_FAILS[uid] = rows
    else:
        _LOCK_FAILS.pop(uid, None)
    return len(rows)


def lock_record_fail(uid: str) -> int:
    _LOCK_FAILS.setdefault(uid, []).append(time.time())
    return lock_fail_count(uid)


# ────────────────────────────────────────────────────────────── 서버 기동
def lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.4)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass

    def score(ip: str) -> int:
        if ip.startswith(("192.168.", "10.")):
            return 0
        if ip.startswith("172."):
            return 1
        return 2

    return sorted(ips, key=score)


def build_server(host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    host = host if host is not None else CFG["host"]
    port = CFG["port"] if port is None else port
    httpd = ThreadingHTTPServer((host, port), PortalHandler)
    httpd.daemon_threads = True
    return httpd


def _port_answering(host: str, port: int) -> bool:
    """이 포트에 이미 응답하는 서버가 있는지(재시작 손넘김 때 옛 프로세스를 기다리는 용도)."""
    probe = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    try:
        with socket.create_connection((probe, port), timeout=0.4):
            return True
    except OSError:
        return False


def print_banner(port: int) -> None:
    ips = lan_ips()
    gem = f"사용 ({CFG['gemini_model']})" if CFG["gemini_key"] else "미설정 — 요약 기본 모드로 동작"
    if CFG["auto_update"]:
        upd = (f"켜짐 — {int(CFG['update_interval']) // 60}분마다 확인"
               f" (mail-board-template/portal)")
    else:
        upd = "꺼짐 (PORTAL_AUTO_UPDATE=0)"
    line = "=" * 62
    print(line)
    print(f"  {APP_NAME} — 팀 공용 포털 서버 v{VERSION}")
    print(line)
    print(f"  이 PC에서 열기      : http://127.0.0.1:{port}")
    if ips:
        print("  팀원에게 알려줄 주소:")
        for ip in ips[:3]:
            print(f"      http://{ip}:{port}")
    else:
        print("  팀원에게 알려줄 주소: (네트워크 주소를 찾지 못했습니다 — ipconfig 로 IPv4 확인)")
    print(f"  데이터 폴더         : {data_dir()}")
    print(f"  Gemini 분석         : {gem}")
    print(f"  자동 업데이트       : {upd}")
    print(f"  자동 메일 갱신      : {CFG['sync_interval'] // 60}분마다 (등록된 사용자 전체)")
    print(f"  처음 로그인하는 분  : 최근 {CFG['seed_days']}일 메일 {CFG['seed_max']}건까지 자동 수집")
    if CFG["backfill_limit"] > 0:
        print(f"  과거 메일 백필      : {CFG['sync_interval'] // 60}분마다 사용자별 {CFG['backfill_limit']}건씩 자동 채움")
    else:
        print("  과거 메일 백필     : 꺼짐 (BACKFILL_LIMIT=0)")
    if LOG_STREAM is not None:
        print(f"  로그 파일           : {LOG_STREAM.path}")
    print("  끄기                : 이 창에서 Ctrl+C (창 없는 실행이면 stop_portal.bat)")
    print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CHUNIL Smart Mail Portal — 팀 공용 통합 포털 서버")
    ap.add_argument("--host", default=None, help="바인딩 주소 (기본 0.0.0.0 — 같은 사내망에서 접속 허용)")
    ap.add_argument("--port", type=int, default=None, help="포트 (기본 8080 또는 환경변수 PORT)")
    ap.add_argument("--data-dir", default=None, help="데이터 폴더 (기본 ./data)")
    ap.add_argument("--no-worker", action="store_true", help="10분 자동 갱신 워커를 켜지 않는다")
    ap.add_argument("--no-update", action="store_true",
                    help="이번 실행에서는 자동 업데이트를 확인하지 않는다(PORTAL_AUTO_UPDATE=0 과 같음)")
    ap.add_argument("--check-update", action="store_true",
                    help="업데이트만 확인하고 끝낸다(내려받거나 교체하지 않음 · JSON 출력)")
    ap.add_argument("--log-file", default=None,
                    help="이후 로그를 이 파일에도 남긴다 (예: data\\server.log · 최대 5MB 로테이션)")
    ap.add_argument("--version", action="store_true", help="버전만 출력")
    args = ap.parse_args(argv)
    if args.version:
        print(VERSION)
        return 0
    if args.no_update:
        CFG["auto_update"] = False
    if args.check_update:
        rep = check_self_update(apply=False, restart=False)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep.get("ok") else 1
    if args.data_dir:
        CFG["data_dir"] = os.path.abspath(args.data_dir)
    if args.port:
        CFG["port"] = args.port
    ensure_data_dirs()
    # 창 없는 실행(pythonw · start_hidden.vbs)은 콘솔이 없어 sys.stdout 이 None 이다.
    # 그때는 말없이 사라지지 않도록 data\server.log 에 남긴다.
    log_path = args.log_file
    if log_path is None and sys.stdout is None:
        log_path = os.path.join(data_dir(), "server.log")
    if log_path:
        log_path = os.path.abspath(os.path.expanduser(log_path))
        try:
            enable_file_logging(log_path)
        except OSError as e:
            print(f"경고: 로그 파일을 열지 못했습니다({log_path}): {e}", file=sys.stderr)
    master_key()                                  # data/.server_key (0600) 준비
    restore_sessions()                            # 재시작·재부팅 전에 로그인한 세션 되살리기
    sanitize_all_boards()                         # 예전에 저장한 슬로건 배너·소형 아이콘 정리
    if not CFG["gemini_key"]:
        print("안내: GEMINI_API_KEY 가 없습니다 — 메일 요약은 기본 규칙 모드로 동작합니다"
              "(.env 에 넣으면 Gemini 분석이 켜집니다).", file=sys.stderr)
    httpd = None
    # 자동 업데이트로 재시작한 자식 프로세스는, 옛 프로세스가 포트를 놓을 때까지 잠깐 기다린다.
    handoff = os.environ.pop("PORTAL_RESTART_HANDOFF", "") == "1"
    if handoff:
        deadline = time.time() + 30
        while time.time() < deadline and _port_answering(CFG["host"], CFG["port"]):
            time.sleep(0.4)
    tries = 12 if handoff else 1
    for attempt in range(tries):
        try:
            httpd = build_server(args.host, CFG["port"])
            break
        except OSError as e:
            if attempt + 1 < tries:
                time.sleep(0.5)
                continue
            print(f"\n[X] {CFG['port']} 포트를 열지 못했습니다: {e}", file=sys.stderr)
            print("    · 이미 다른 프로그램(또는 실행 중인 이 서버)이 그 포트를 쓰고 있을 수 있습니다.", file=sys.stderr)
            print("    · .env 에 PORT=8081 처럼 다른 번호를 넣고 다시 실행하세요(팀원 주소도 :8081).", file=sys.stderr)
            return 2
    if httpd is None:
        return 2
    global HTTPD
    HTTPD = httpd
    real_port = httpd.server_address[1]
    print_banner(real_port)
    if not args.no_worker:
        start_worker()
    if CFG["auto_update"]:
        start_update_worker()   # 첫 확인은 시작 20초 뒤, 이후 PORTAL_UPDATE_INTERVAL(기본 5분)마다
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n서버를 종료합니다…")
    finally:
        STOP.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
