#!/usr/bin/env python3
"""천일 웹메일(Nmail) → Gemini API → data.enc.json 동기화 (GitHub Actions 무인 실행용 · 사내 배포판).

GitHub Actions 러너에서 30분마다 돌아,
   1) 기존 data.enc.json 을 MAILBOARD_PIN 으로 복호화해 지금 상태를 읽고,
   2) mail.chunil.co.kr 에 로그인해 **사내 주소록(조직도+개인주소록)** 이 비어 있으면 받아 두고,
   3) **로그인 사용자(이름·메일·부서·직급)** 를 먼저 읽고(주소록의 본인 항목으로 보완),
   4) 받은편지함·보낸편지함의 새 메일만 가져와,
   5) Gemini API 로 요약·할 일·기한·회신 필요 여부·답장 3톤을 만들고,
   6) build_data.py 와 같은 형식(v2 gzip + PBKDF2-SHA256 10만 회 + AES-256-GCM)으로
      data.enc.json 을 다시 암호화한다.

설계 원칙
 - 저장소는 공개일 수 있다. PIN·계정·API 키는 환경변수로만 받고,
   평문 data.json 은 만들지도 커밋하지도 않는다(메모리에서만 다룬다).
 - **특정 사람을 코드에 박지 않는다.** 이름·메일·부서는 웹메일 화면에서 읽어 data.owner 에 넣고,
   본문 지목 판정(MENTION_RE)도 그 이름으로 만든다. VIP·분류 기준은 board_config.json.
 - **첫 실행이면 스스로 채운다.** data.enc.json 이 없거나 비어 있으면(items: []) 최근 N일 메일을
   자동 수집해 새로 만든다(`--seed-days`, 기본 14일 · `--seed-max`, 기본 30건).
 - 새 메일이 0건이면 아무 파일도 쓰지 않고 끝난다(불필요한 커밋·배포 금지).
 - 분류(3단계·VIP 등급)는 make_data.py 의 확정 규칙을 그대로 옮겨 파이썬에서 결정론적으로
   계산한다. Gemini 는 요약·할 일·기한·답장 초안 같은 '해석'만 담당한다.
   → 모델이 흔들려도 등급이 바뀌지 않고, 등급 규칙을 고칠 때 프롬프트를 다시 손볼 필요가 없다.

환경변수
 CHUNIL_ID        웹메일 아이디 (예: kimpro — 메일 주소가 아니라 @ 앞부분)
 CHUNIL_PW        웹메일 비밀번호
 GEMINI_API_KEY   Google AI Studio(또는 사내 공유) API 키
 MAILBOARD_PIN    대시보드 복호화 PIN (대시보드 로그인 비밀번호)
 GEMINI_MODEL     (선택) 기본 gemini-3.8-flash
 NMAIL_IP_CHECK   (선택) IP보안접속 값. 기본 1
 BOARD_CONFIG     (선택) 설정 파일 경로. 기본 <저장소>/board_config.json

사용
  MAILBOARD_PIN=**** python3 scripts/sync_mail.py --data-enc data.enc.json
  MAILBOARD_PIN=**** python3 scripts/sync_mail.py --dry-run          # 수집·분석만, 파일은 안 씀
  MAILBOARD_PIN=**** python3 scripts/sync_mail.py --refresh-contacts # 사내 주소록을 새로 받아 갱신
  python3 scripts/sync_mail.py --selftest-roundtrip --data-enc data.enc.json
  python3 scripts/sync_mail.py --fixture-list _tmp/list.html --fixture-read _tmp/read.html
  python3 scripts/sync_mail.py --owner-probe     # 로그인 사용자 인식만 확인(메일 수집 안 함)
  python3 scripts/sync_mail.py --contacts-probe  # 사내 주소록 수집만 확인(몇 명 읽혔는지)
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import struct
import sys
import time
import unicodedata
from typing import Any

import requests
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# ────────────────────────────────────────────────────────────── 상수 (make_data.py 와 동일)
BASE = "https://mail.chunil.co.kr"
LOGIN_PAGE = BASE + "/login.php"
LOGIN_POST = BASE + "/login_ok.php"
LIST_URL = BASE + "/mail_list.php"
READ_URL = BASE + "/mail_read.php"
MAIL_LINK = BASE + "/mail_read.php?mb_id=%s&mf_no=%s"

# 사내 주소록(조직도 + 개인주소록) 화면 — 로그인 세션으로 그대로 긁는다
#   address_group.php : 부서 그룹 트리(JS TreeNode) — 각 그룹의 그룹번호(mg_no/adg_no)와 이름
#   member_list.php   : 조직도 그룹의 사내 구성원 목록(?s_mg_no=<그룹번호>&is_iframe=1, 20명/페이지)
#   addr_list.php     : 개인주소록 그룹의 연락처 목록(?s_adg_no=<그룹번호>&is_iframe=1, 20건/페이지)
GROUP_URL = BASE + "/address_group.php?type=G&tmp=tmp"
MEMBER_LIST_PATH = "/member_list.php"
ADDR_LIST_PATH = "/addr_list.php"
CONTACT_KINDS = {"G": "org", "P": "personal", "S": "shared"}

KST = dt.timezone(dt.timedelta(hours=9))
DOMAIN = "chunil.co.kr"       # 사내 메일 도메인 — 메일 주소는 {CHUNIL_ID}@chunil.co.kr

DEFAULT_COMPANY = "천일정기화물자동차"

# ── 로그인 사용자와 분류 설정 ────────────────────────────────────────────────
#  이 파일은 사내 배포판이라 특정 사람을 상정하지 않는다. 수집을 시작하기 전에
#  configure(detect_owner(...), load_board_config()) 를 한 번 호출해 아래 값을 채운다.
OWNER: dict = {"name": "", "title": "", "dept": "", "company": DEFAULT_COMPANY, "email": ""}
ME = ""                                        # 로그인 사용자의 메일 주소
MENTION_RE = re.compile(r"(?!x)x")             # 본문 지목 정규식 (이름에서 만든다)

# (이름 일치 문자열, 메일주소, 화면 표시 라벨, vipRank, To 조건 필요 여부) — 화면에는 중립 등급명만
VIPS: list[tuple[str, str, str, int, bool]] = []
P1_LABEL, P2_LABEL, P3_LABEL = "🔴 즉시 확인", "🟡 흐름 참고", "⚪ 단순 공유"
P3_TITLE_KEYWORDS: tuple[str, ...] = ("출하리스트", "납입요청", "미상차현황")

CFG: dict = {
    "company": DEFAULT_COMPANY,
    "to_is_urgent": True,      # 내가 To 로 직접 받은 메일도 즉시 확인으로 (새 사용자 기본)
    "p3_title_keywords": list(P3_TITLE_KEYWORDS),
    "vips": [],                # [{"name": "", "email": "", "label": "1-1 최우선", "rank": 1, "toOnly": false}]
}
P3_TITLE_REASON = P3_LABEL + " · 출하/납입 리스트"
P3_CATEGORIES = {"재고/실사", "현황/보고", "출하/보급"}
P3_CATEGORY_REASON = P3_LABEL + " · 현장 일상 공유"
EXTERNAL_CATS = {"PTR", "승인/검토", "결품/보고", "불량/품질", "조직/업무", "일정/공유", "반출/전용"}
CATEGORIES = ["승인/검토", "PTR", "결품/보고", "불량/품질", "재고/실사", "현황/보고", "출하/보급",
              "납입요청", "일정/공유", "조직/업무", "공지/안전", "자료요청", "자재/확인", "기타"]
# 이름 뒤에 붙는 직급 — 긴 것부터 먼저 걸리게 순서를 지킨다('과장보'→과장, '본부장'→부장 방지).
TITLE_WORDS = ("과장보", "부사장", "본부장", "상무", "전무", "이사", "부장", "차장", "과장", "대리",
               "사원", "팀장", "실장", "소장", "주임", "계장", "반장", "직장", "대표",
               "수석", "책임", "선임", "매니저", "프로", "조장")
SHIP_KEYWORDS = ("선적", "b/l", "bl no", "항공", "해상", "통관", "해외물류", "asn", "수출", "수입", "부킹", "bl ")
QUOTE_MARKERS = ("보낸 사람:", "보낸사람:", "-----original", "from:", "sent:", "________________________________")


# ────────────────────────────────────────────────────────────── 로고·서명 이미지 제외
#  회사 메일은 서명에 로고·CI·슬로건 배너·SNS 아이콘을 따라 붙인다. 이걸 본문 사진으로
#  세거나 Gemini 에 넘기면 "회사 로고입니다" 같은 의미 없는 분석이 붙고, 대시보드 본문에도
#  사진이 늘어난다. 그래서 ① 서명·인용 영역 안 ② 파일명/대체텍스트가 로고류
#  ③ 크기가 아이콘·가로 배너 ④ 내려받은 바이트가 12KB 미만 — 네 가지로 거른다.
SIGN_AREA_HINTS = ("sign", "signature", "sig", "gmail_quote", "gmail_signature", "footer")
SIGN_IMG_HINTS = ("logo", "ci", "sign", "signature", "slogan", "banner", "footer", "header",
                  "icon", "spacer", "btn", "button", "pixel", "tracking",
                  "로고", "슬로건", "서명", "엠블럼")
SHORT_HINTS = {"ci", "btn", "sig"}      # 3글자 이하 약어는 낱말 경계에서만('ascii'·'decision' 오인 방지)
SIGN_IMG_MIN_W = 120                    # 이보다 좁으면 구분선·아이콘
SIGN_IMG_MIN_H = 60                     # 이보다 낮으면 띠 모양 배너
SIGN_IMG_BANNER_H = 120                 # 높이 120 이하면서 가로가 세로의 4배 이상이면 가로 배너
IMG_MIN_BYTES = 12_000                  # 12KB 미만 — 로고·아이콘·추적 픽셀
#  내려받은 바이트의 **실제 픽셀 크기** 기준(서명에 붙는 'Hi CHUNIL, High green' 가로 배너 등)
IMG_SMALL_W = 130                       # 이보다 좁으면 소형 아이콘
IMG_SMALL_H = 70                        # 이보다 낮으면 소형 아이콘·구분선
IMG_BANNER_H = 140                      # 높이 140 이하에서
IMG_BANNER_RATIO = 2.5                  # 가로가 세로의 2.5배 이상이면 가로형 슬로건·서명 배너
IMG_BANNER_MAX_W = 700                  # 단, 폭 700 미만만 — 1200x110 같은 표 캡처는 본문 사진이다
DEFAULT_MODEL = "gemini-3.8-flash"


# ────────────────────────────────────────────────────────────── 암호 (build_data.py 와 동일 형식)
def derive(pin: str, salt: bytes, iterations: int) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(
        pin.encode("utf-8"))


def decrypt_payload(enc: dict, pin: str) -> dict:
    """data.enc.json(dict) → 평문 dict. v1(비압축)·v2(gzip) 모두 지원."""
    if not isinstance(enc, dict) or "ciphertext" not in enc:
        raise ValueError("data.enc.json 형식이 아닙니다")
    key = derive(pin, base64.b64decode(enc["salt"]), int(enc.get("iterations") or 100_000))
    raw = AESGCM(key).decrypt(base64.b64decode(enc["iv"]), base64.b64decode(enc["ciphertext"]), None)
    if enc.get("comp") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def encrypt_payload(payload: dict, pin: str, *, compress: bool = True, iterations: int = 100_000) -> dict:
    """dict → data.enc.json(dict). build_data.py 와 키 이름·순서·형식을 그대로 맞춘다."""
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    packed = gzip.compress(plain, 9, mtime=0) if compress else plain
    salt, iv = os.urandom(16), os.urandom(12)
    key = derive(pin, salt, iterations)
    ciphertext = AESGCM(key).encrypt(iv, packed, None)
    return {
        "v": 2 if compress else 1,
        "kdf": "PBKDF2-SHA256",
        "iterations": iterations,
        "cipher": "AES-256-GCM",
        "comp": "gzip" if compress else "none",
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "plainBytes": len(plain),
        "updatedAt": payload.get("updatedAt"),
    }


# ────────────────────────────────────────────────────────────── 웹메일 수집
# ────────────────────────────────────────────────────────────── 로그인 사용자 · 설정 · 초기 상태
def split_name_title(text: str) -> tuple[str, str]:
    """`김성규 과장` / `최수철부장` → (이름, 직급). 직급이 없으면 (원문, "")."""
    t = _clean(text or "")
    if not t:
        return "", ""
    m = re.fullmatch(r"([가-힣A-Za-z·.\s]{1,20}?)\s*(" + "|".join(TITLE_WORDS) + r")", t)
    if not m:
        return t, ""
    return m.group(1).strip(), m.group(2)


def build_mention_re(name: str, title: str = "") -> re.Pattern:
    """본문에서 나를 지목했는지 보는 정규식 — 로그인 사용자의 이름·성+직급으로 만든다.

    `김성규` → `김\\s*성\\s*규`, `김 과장` → `김\\s*과장`(성이 같고 직급이 같은 다른 사람은 드물다).
    """
    n = re.sub(r"\s+", "", name or "")
    pats: list[str] = []
    if len(n) >= 2:
        pats.append(r"\s*".join(re.escape(ch) for ch in n))
        if title:
            pats.append(re.escape(n[0]) + r"\s*" + re.escape(title))
    if not pats:
        return re.compile(r"(?!x)x")
    return re.compile("|".join(pats))


# ── 직급 자동 추론 ────────────────────────────────────────────────
#  Nmail 조직도의 '직위' 칸이 비어 있으면 owner['title'] 이 빈 채로 남아 화면에
#  '김성규' 처럼 이름만 뜬다. 그래서 본인이 보낸/받은 메일 글에서 '이름 + 직급'을
#  세어 가장 많이 나온 직급을 채운다(서명·호칭이 가장 믿을 만한 근거다).
_TITLE_RE_CACHE: dict[str, re.Pattern | None] = {}


def title_of_name_re(name: str) -> re.Pattern | None:
    """`김성규 과장` 처럼 이름 바로 뒤에 직급이 오는 패턴. 이름이 2글자 미만이면 None."""
    n = re.sub(r"\s+", "", name or "")
    if len(n) < 2:
        return None
    if n in _TITLE_RE_CACHE:
        return _TITLE_RE_CACHE[n]
    body = r"\s*".join(re.escape(ch) for ch in n)          # '김 성규' 도 허용
    pat = re.compile(rf"(?<![가-힣]){body}\s*({'|'.join(TITLE_WORDS)})")
    _TITLE_RE_CACHE[n] = pat
    return pat


def _title_sample_text(it: dict) -> str:
    parts = [str(it.get(k) or "") for k in ("title", "summary", "bodyText", "sender", "senderName")]
    for m in it.get("thread") or []:
        if isinstance(m, dict):
            parts.append(str(m.get("from") or ""))
            parts.append(str(m.get("gist") or ""))
    return "\n".join(parts)


def _count_titles(pat: re.Pattern, texts: list[str], weight: int) -> tuple[dict[str, int], dict[str, int]]:
    scores: dict[str, int] = {}
    first: dict[str, int] = {}
    order = 0
    for text in texts:
        for m in pat.finditer(text or ""):
            t = m.group(1)
            if t not in scores:
                scores[t], first[t] = 0, order
                order += 1
            scores[t] += weight
    return scores, first


def infer_owner_title(name: str, items: list[dict] | None = None, *,
                      sent_texts: list[str] | None = None,
                      inbox_texts: list[str] | None = None,
                      limit: int = 400) -> str:
    """메일 글에서 '이름 + 직급'을 세어 가장 많이 나온 직급을 고른다(없으면 "").

    · 보낸 메일(서명)에서 찾으면 그것을 먼저 믿는다 — 본인이 쓴 직급이 가장 정확하다.
    · 없으면 받은 메일(호칭·인용된 서명)에서 고른다.
    · 최근 메일부터 각각 limit 건만 본다(9,000건 전체를 훑지 않아 빠르다).
    `items` 는 보드 항목, sent_texts/inbox_texts 는 방금 읽은 메일 본문을 바로 넘길 때 쓴다.
    """
    pat = title_of_name_re(name)
    if pat is None:
        return ""
    sent = list(sent_texts or [])
    inbox = list(inbox_texts or [])
    rows = [it for it in (items or []) if isinstance(it, dict)]
    if rows:
        rows.sort(key=lambda it: str(it.get("receivedAt") or it.get("lastAt") or ""), reverse=True)
        sent += [_title_sample_text(it) for it in rows
                 if str(it.get("mailbox") or "").lower() == "sent"][:limit]
        inbox += [_title_sample_text(it) for it in rows
                  if str(it.get("mailbox") or "").lower() != "sent"][:limit]
    sent_scores, sent_first = _count_titles(pat, sent, 3)     # 보낸 메일 = 가중치 3
    if sent_scores:
        return max(sent_scores, key=lambda t: (sent_scores[t], -sent_first[t]))
    in_scores, in_first = _count_titles(pat, inbox, 1)       # 받은 메일 = 가중치 1
    if not in_scores:
        return ""
    return max(in_scores, key=lambda t: (in_scores[t], -in_first[t]))


def ensure_owner_title(data: dict | None = None, *,
                       sent_texts: list[str] | None = None,
                       inbox_texts: list[str] | None = None) -> str:
    """owner 직급이 비어 있으면 기록에서 추론해 채우고, 지목 판정 규칙까지 다시 세운다.

    이미 채워진 사용자는 아무 일도 하지 않는다(다음 동기화마다 불려도 안전).
    찾은 직급을 돌려준다(못 찾으면 "").
    """
    target = data if isinstance(data, dict) else {}
    owner = target.get("owner") if isinstance(target.get("owner"), dict) else OWNER
    if (owner.get("title") or "").strip():
        return ""
    name = (owner.get("name") or OWNER.get("name") or "").strip()
    title = infer_owner_title(name, list(target.get("items") or []),
                              sent_texts=sent_texts, inbox_texts=inbox_texts)
    if not title:
        return ""
    owner["title"] = title
    OWNER["title"] = title
    configure(dict(OWNER))            # 본문 지목 판정(성+직급)을 새 직급으로 다시 만든다
    for it in target.get("items") or []:      # 직급이 생기면 '김 과장' 지목 건이 1순위로 올라온다
        if isinstance(it, dict):
            try:
                classify(it)
            except Exception:  # noqa: BLE001 - 한 건 실패가 저장을 막지 않게
                continue
    if isinstance(target.get("owner"), dict):
        target["owner"]["title"] = title
    return title


def configure(owner: dict | None = None, cfg: dict | None = None) -> dict:
    """owner·설정을 전역 분류 규칙에 반영한다(수집 시작 전에 한 번)."""
    global OWNER, ME, MENTION_RE, VIPS, P3_TITLE_KEYWORDS, CFG
    if cfg:
        CFG = {**CFG, **{k: v for k, v in cfg.items() if v is not None}}
    OWNER = {**OWNER, **(owner or {})}
    name, title = (OWNER.get("name") or "").strip(), (OWNER.get("title") or "").strip()
    if name and not title:
        n2, t2 = split_name_title(name)
        if t2:
            name, title = n2, t2
    elif name and title and name.endswith(title):
        name = name[: -len(title)].strip()
    OWNER["name"], OWNER["title"] = name, title
    OWNER["company"] = (OWNER.get("company") or DEFAULT_COMPANY).strip() or DEFAULT_COMPANY
    ME = (OWNER.get("email") or "").strip().lower()
    MENTION_RE = build_mention_re(name, title)

    vips: list[tuple[str, str, str, int, bool]] = []
    for v in CFG.get("vips") or []:
        if not isinstance(v, dict):
            continue
        key = str(v.get("name") or "").strip()
        addr = str(v.get("email") or "").strip().lower()
        if not (key or addr):
            continue
        try:
            rank = int(v.get("rank") or 2)
        except (TypeError, ValueError):
            rank = 2
        label = str(v.get("label") or (f"1-{rank} 최우선" if rank == 1 else f"1-{rank} 주요"))
        vips.append((key, addr, label, rank, bool(v.get("toOnly"))))
    VIPS = vips
    kws = CFG.get("p3_title_keywords")
    if kws:
        P3_TITLE_KEYWORDS = tuple(str(k) for k in kws)
    return OWNER


# ────────────────────────────────────────────────────────────── 사내 주소록 파서
#  data["contacts"] 항목 구조(페이지가 읽는 키): {name, email, dept, kind, title}
#    kind: org(조직도) · personal(개인주소록) — index.html 의 분류 규칙 설정이 이 값으로 구분한다.
_CONTACT_TOTAL_RE = re.compile(r"전체\s*([\d,]+)\s*명")
_TREE_NODE_RE = re.compile(
    r"(newNode(?:_\d+)*)\s*=\s*(objTreeMenu_1|newNode(?:_\d+)*)\.addItem\(\s*"
    r"new TreeNode\('((?:[^'\\]|\\.)*)'")
_TREE_EVENT_RE = re.compile(r"addr_list_update\(\s*\\?['\"]([GPS])\\?['\"]\s*,\s*\\?['\"](\d+)")


def contact_total(html: str) -> int:
    """목록 화면의 `※ 전체 N명` → N (모르면 0)."""
    m = _CONTACT_TOTAL_RE.search(_clean(html or ""))
    if not m:
        return 0
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return 0


def parse_contact_tree(html: str) -> list[dict]:
    """주소록 그룹 트리 → [{no, name, kind, depth}] (kind: org/personal/shared).

    마크업은 JS 문자열이다 — `newNode_4_1 = newNode_4.addItem(new TreeNode('CMF파견', ...` 다음 줄의
    `parent.addr_list_update(\'G\', \'310\', ...)` 가 그 그룹의 종류(G/P/S)와 그룹번호다.
    변수 이름이 조직도·개인주소록에서 다시 쓰이므로(parent=objTreeMenu_1 이면 새 트리의 뿌리)
    이름이 겹쳐도 순서대로 덮어쓰며 읽으면 된다.
    """
    lines = (html or "").splitlines()
    nodes: dict[str, dict] = {}
    out: list[dict] = []
    for i, line in enumerate(lines):
        m = _TREE_NODE_RE.search(line)
        if not m:
            continue
        var, parent, raw_name = m.group(1), m.group(2), m.group(3)
        name = _clean(raw_name.replace("\\'", "'").replace('\\"', '"'))
        kind, no = "", ""
        for nxt in lines[i + 1: i + 5]:
            ev = _TREE_EVENT_RE.search(nxt)
            if ev:
                kind, no = CONTACT_KINDS.get(ev.group(1), ""), ev.group(2)
                break
        depth = 0 if parent == "objTreeMenu_1" else nodes.get(parent, {}).get("depth", 0) + 1
        nodes[var] = {"name": name, "kind": kind, "no": no, "depth": depth}
        if kind and no:
            out.append({"no": no, "name": name, "kind": kind, "depth": depth})
    return out


def parse_org_contacts(html: str) -> list[dict]:
    """member_list.php(조직도) → [{name, title, email}] — 직위 칸은 서버에 비어 있는 경우가 많다."""
    rows: list[dict] = []
    soup = BeautifulSoup(html or "", "html.parser")
    for tr in soup.find_all("tr"):
        if tr.find("input", attrs={"name": "chk_m_no[]"}) is None:
            continue
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 4:
            continue
        name = _clean(cells[1].get_text(" ", strip=True))
        title = _clean(cells[2].get_text(" ", strip=True))
        email = _addr_email(cells[3].get_text(" ", strip=True)) or _addr_email(str(cells[3]))
        if not name and not email:
            continue
        rows.append({"name": name, "title": title, "email": email})
    return rows


def parse_personal_contacts(html: str) -> list[dict]:
    """addr_list.php(개인주소록) → [{name, email, group}] (외부 담당자·협력사가 대부분)."""
    rows: list[dict] = []
    soup = BeautifulSoup(html or "", "html.parser")
    for tr in soup.find_all("tr"):
        if tr.find("input", attrs={"name": "chk_ad_no[]"}) is None:
            continue
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 4:
            continue
        group = _clean(cells[1].get_text(" ", strip=True))
        name = _clean(cells[2].get_text(" ", strip=True))
        email = _addr_email(cells[3].get_text(" ", strip=True)) or _addr_email(str(cells[3]))
        if not name and not email:
            continue
        rows.append({"name": name, "email": email, "group": group})
    return rows


def merge_contacts(org: list[dict], personal: list[dict]) -> list[dict]:
    """조직도·개인주소록을 한 목록으로 — 같은 메일은 한 줄(조직도 우선), 개인주소록은 조직도 중복 제외."""
    out: list[dict] = []
    by_email: dict[str, dict] = {}

    def put(rec: dict, kind: str) -> None:
        name = _clean(rec.get("name") or "")
        email = (rec.get("email") or "").strip().lower()
        dept = _clean(rec.get("dept") or "")
        title = _clean(rec.get("title") or "")
        if not name and not email:
            return
        cur = by_email.get(email) if email else None
        if cur is not None:
            if not cur.get("dept") and dept:
                cur["dept"] = dept
            if not cur.get("title") and title:
                cur["title"] = title
            if name and (not cur.get("name") or "@" in cur["name"]):
                cur["name"] = name
            return
        item = {"name": name, "email": email, "dept": dept, "kind": kind, "title": title}
        out.append(item)
        if email:
            by_email[email] = item

    for rec in org:
        put(rec, "org")
    for rec in personal:
        put(rec, "personal")
    return out


def contact_dept(name: str) -> str:
    """그룹 이름 → 부서명. 조직도에 있는 '-'·'.' 같은 자리표시 그룹은 부서가 아니다."""
    cleaned = _clean(name or "")
    if not cleaned or not re.search(r"[0-9A-Za-z가-힣]", cleaned):
        return ""
    return cleaned


def load_board_config(path: str | None = None) -> dict:
    """board_config.json → 분류 설정. 파일이 없으면 사내 표준 기본값을 쓴다."""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = ([path] if path else []) + [
        os.path.join(here, "..", "board_config.json"),
        os.path.join(os.getcwd(), "board_config.json"),
    ]
    for cand in cands:
        if not cand or not os.path.exists(cand):
            continue
        try:
            with open(cand, encoding="utf-8") as f:
                raw = json.load(f)
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError) as e:
            print(f"경고: 설정 파일을 읽지 못했습니다({cand}): {e}", file=sys.stderr)
            return {}
    return {}


def parse_profile(html: str) -> dict:
    """개인정보/환경설정 페이지 → {name, email, dept, title}.

    마크업이 바뀌거나 필드가 비어 있어도 되도록 input·select·표(th/td)를 모두 넓게 본다.
    """
    out = {"name": "", "email": "", "dept": "", "title": ""}
    if not html:
        return out
    soup = BeautifulSoup(html, "html.parser")
    keys = {
        "name": re.compile(r"(m_?name|mb_?name|user_?name|member_?name|^name$)", re.I),
        "email": re.compile(r"(m_?mail|mb_?mail|user_?mail|e_?mail|email)", re.I),
        "dept": re.compile(r"(dept|depart|buseo|team_?name|jikcha|group_?name)", re.I),
        "title": re.compile(r"(jikwi|jikgeup|jik|rank|title|position)", re.I),
    }
    for el in soup.find_all(["input", "select"]):
        field_name = (el.get("name") or el.get("id") or "").strip()
        if not field_name:
            continue
        low = field_name.lower()
        if any(k in low for k in ("pwd", "pass", "secret")):
            continue
        val = (el.get("value") or "").strip()
        if el.name == "select":
            sel = el.find("option", selected=True)
            val = _clean(sel.get_text(" ", strip=True)) if sel is not None else val
        val = _clean(val)
        if not val:
            continue
        for field, rx in keys.items():
            if not out[field] and rx.search(field_name):
                out[field] = val
                break
    labels = {"이름": "name", "성명": "name", "부서": "dept", "소속": "dept", "직급": "title",
              "직위": "title", "메일": "email", "이메일": "email", "메일주소": "email"}
    for th in soup.find_all(["th", "dt"]):
        key = _clean(th.get_text(" ", strip=True)).rstrip(":：")
        field = labels.get(key)
        if not field or out.get(field):
            continue
        td = th.find_next_sibling(["td", "dd"])
        if td is not None:
            out[field] = _clean(td.get_text(" ", strip=True))
    if not out["email"]:
        m = re.search(r"[\w.+-]+@" + re.escape(DOMAIN), html)
        if m:
            out["email"] = m.group(0)
    return out


def find_self_in_contacts(contacts: list | None, corp_email: str, name: str = "") -> dict | None:
    """사내 주소록에서 로그인 사용자 항목을 찾는다 — 사내 메일 일치 우선, 없으면 사내 메일 + 이름 일치.

    개인정보 페이지의 '메일'은 외부 복구 메일(hanmail.net 등)일 수 있어 그 주소로는 주소록을
    찾지 못한다. 그래서 `{아이디}@chunil.co.kr` 로 먼저 찾고, 없을 때만 이름으로 한 번 더 본다.
    """
    book = [c for c in (contacts or []) if isinstance(c, dict)]
    target = (corp_email or "").strip().lower()
    if target:
        for c in book:
            if (c.get("email") or "").strip().lower() == target:
                return c
    base = re.sub(r"\s+", "", split_name_title(name or "")[0] or _clean(name or ""))
    if len(base) >= 2:
        for c in book:
            if not (c.get("email") or "").lower().endswith("@" + DOMAIN):
                continue
            cname = re.sub(r"\s+", "", c.get("name") or "")
            if cname == base or (len(base) >= 3 and cname.startswith(base)):
                return c
    return None


def detect_owner(nm: "Nmail", chunil_id: str, *, contacts: list | None = None,
                 data: dict | None = None) -> dict:
    """로그인 사용자를 웹메일에서 읽는다 → data.owner {name,title,dept,company,email}.

    순서: 개인정보 페이지 → 상단 프로필 → **사내 주소록(본인 항목)** → 기존 data.owner → 아이디.
    owner['email'] 은 언제나 사내 메일(`{아이디}@chunil.co.kr`)이다 — 개인정보 페이지가 알려주는
    외부 복구 메일(hanmail.net 등)은 쓰지 않는다. To 판정·본문 지목 판정이 이 주소로 이뤄진다.
    하나도 못 읽어도 수집은 계속된다(이름이 비면 지목 판정만 꺼진다).
    """
    corp = f"{chunil_id}@{DOMAIN}".lower() if chunil_id else ""
    owner = {"name": "", "title": "", "dept": "", "company": DEFAULT_COMPANY, "email": corp}
    prof: dict = {}
    try:
        prof = parse_profile(nm.profile_page())
    except Exception as e:  # noqa: BLE001 - 화면 구조가 달라도 계속
        if os.environ.get("SYNC_DEBUG"):
            print(f"  · 개인정보 페이지 읽기 실패: {type(e).__name__}: {e}", file=sys.stderr)
    for k in ("name", "dept", "title"):
        if prof.get(k):
            owner[k] = prof[k]

    if not owner["name"]:
        try:
            ident = nm.header_identity()
        except Exception:  # noqa: BLE001
            ident = {}
        owner["name"] = (ident.get("name") or "").strip()
        owner["dept"] = owner["dept"] or (ident.get("dept") or "").strip()

    # 사내 주소록의 본인 항목 — 부서·직급·이름을 보완한다(사내 주소록 직위 칸은 비어 있을 수 있다).
    hit = find_self_in_contacts(contacts, corp, owner["name"])
    if hit:
        owner["name"] = owner["name"] or (hit.get("name") or "")
        owner["dept"] = owner["dept"] or (hit.get("dept") or "")
        owner["title"] = owner["title"] or (hit.get("title") or "")
        if not owner["email"]:
            owner["email"] = (hit.get("email") or "").lower()

    prev = (data or {}).get("owner")
    if isinstance(prev, dict):
        for k in ("name", "title", "dept", "company", "email"):
            if not owner.get(k) and prev.get(k):
                owner[k] = prev[k]

    owner["name"] = owner["name"] or chunil_id or ""
    if owner["name"] and not owner["title"]:
        n2, t2 = split_name_title(owner["name"])
        if t2:
            owner["name"], owner["title"] = n2, t2
    owner["email"] = (corp or owner["email"] or "").lower()   # 사내 메일 우선(외부 복구 메일은 버림)
    return owner


def new_payload(*, owner: dict | None = None, seed_days: int = 14,
                now: dt.datetime | None = None) -> dict:
    """첫 실행용 빈 상태 — 페이지가 읽는 구조(owner·criteria·items…)를 그대로 갖춘다."""
    now = now or kst_now()
    o = dict(owner or OWNER)
    return {
        "updatedAt": None,
        "account": o.get("email") or "",
        "owner": o,
        "source": f"mail.chunil.co.kr 받은편지함 (최근 {seed_days}일 · 이전 기록은 검색)",
        "rangeFrom": (now - dt.timedelta(days=seed_days)).strftime("%Y-%m-%d"),
        "archivedCount": 0,
        "archiveIndexCount": 0,
        "criteria": {},
        "items": [],
        "followups": [],
        "contacts": [],
    }


def load_or_init(path: str, pin: str, *, seed_days: int = 14) -> tuple[dict, bool]:
    """data.enc.json 을 읽는다. 없거나 비어 있으면(items: []) 새 상태로 시작한다.

    ★ 복호화에 실패하면(PIN 불일치) 절대 덮어쓰지 않고 멈춘다 — 기존 기록을 지우지 않기 위해서.
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return new_payload(seed_days=seed_days), True
    with open(path, encoding="utf-8") as f:
        try:
            enc = json.load(f)
        except ValueError as e:
            raise SystemExit(f"data.enc.json 형식이 아닙니다: {e}")
    try:
        data = decrypt_payload(enc, pin)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            "data.enc.json 을 MAILBOARD_PIN 으로 열지 못했습니다. "
            "대시보드 로그인 비밀번호(MAILBOARD_PIN)가 이 저장소의 암호문과 같은지 확인하세요. "
            "다른 PIN 이면 기존 기록을 지우지 않도록 여기서 멈춥니다. (" + type(e).__name__ + ")"
        )
    data.setdefault("items", [])
    data.setdefault("followups", [])
    data.setdefault("contacts", [])
    data.setdefault("criteria", {})
    if not data["items"]:
        data.setdefault("rangeFrom", (kst_now() - dt.timedelta(days=seed_days)).strftime("%Y-%m-%d"))
        return data, True
    return data, False


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class NmailError(RuntimeError):
    pass


class Nmail:
    def __init__(self, user: str, password: str, *, ip_check: str = "1", timeout: int = 30, debug_dir: str | None = None):
        self.user, self.password, self.ip_check = user, password, ip_check
        self.timeout, self.debug_dir = timeout, debug_dir
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        })

    # -- 내부 ------------------------------------------------------------
    def _get(self, url: str, **kw) -> requests.Response:
        r = self.s.get(url, timeout=self.timeout, **kw)
        r.encoding = self._encoding(r)
        return r

    def _post(self, url: str, data: dict, **kw) -> requests.Response:
        r = self.s.post(url, data=data, timeout=self.timeout, **kw)
        r.encoding = self._encoding(r)
        return r

    @staticmethod
    def _encoding(r: requests.Response) -> str:
        head = r.content[:2048].decode("ascii", "ignore").lower()
        m = re.search(r'charset=["\']?([a-z0-9_-]+)', head)
        if m:
            return m.group(1)
        return "utf-8"

    def _dump(self, name: str, text: str) -> None:
        if not self.debug_dir:
            return
        os.makedirs(self.debug_dir, exist_ok=True)
        path = os.path.join(self.debug_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    # -- 로그인 ----------------------------------------------------------
    def login(self) -> None:
        page = self._get(LOGIN_PAGE)
        if "m_pwd" not in page.text and "login_ok" not in page.text:
            # 이미 세션이 살아 있는 경우(러너에서는 드묾) — 목록으로 확인
            if self._logged_in():
                return
            raise NmailError("로그인 페이지를 읽지 못했습니다 (URL/네트워크 확인)")
        payload = {
            "m_id": self.user,
            "m_pwd": self.password,
            "back_url": "/",
            "is_save_id": "1",
        }
        if str(self.ip_check) == "1":
            payload["is_ip_check"] = "1"
        r = self._post(LOGIN_POST, payload, headers={"Referer": LOGIN_PAGE}, allow_redirects=True)
        self._dump("login_result.html", r.text)
        if not self._logged_in():
            try:
                body = r.content.decode("utf-8")
            except UnicodeDecodeError:
                body = r.content.decode("cp949", "replace")
            hint = ""
            if "IP" in body and ("보안" in body or "허용" in body):
                hint = " — IP 보안접속 설정에서 러너 IP가 막혔을 수 있습니다(NMAIL_IP_CHECK=0 으로 재시도)."
            elif "비밀번호" in body and ("틀" in body or "확인" in body or "일치" in body):
                hint = " — 아이디/비밀번호가 맞지 않습니다."
            alerts = re.findall(r"alert\(['\"]([^'\"]{1,160})['\"]\)", body)
            locs = re.findall(r"location\.(?:href|replace)\s*=?\s*\(?['\"]([^'\"]{1,120})['\"]", body)
            diag = f" [status={r.status_code}, url={r.url.split('?')[0]}"
            if alerts:
                diag += f", alert={alerts[0]!r}"
            if locs:
                diag += f", redirect={locs[0]!r}"
            diag += "]"
            raise NmailError("웹메일 로그인 실패 (받은편지함에 접근할 수 없음)" + hint + diag)

    def _logged_in(self) -> bool:
        r = self._get(LIST_URL + "?mb_id=inbox&page_size=5")
        if "login.php?back_url" in r.text or "login.php" in r.url:
            return False
        # 로그인 상태에서만 나오는 편지함 사이드바(mb_id=sent)가 있으면 통과
        return ("mb_id=sent" in r.text) and ("m_pwd" not in r.text)

    # -- 로그인 사용자 ------------------------------------------------------
    def profile_page(self) -> str:
        """개인정보(내 정보) 페이지 HTML — 이름·부서·직급을 읽기 위한 것."""
        r = self._get(BASE + "/member.php?mode=MODIFY")
        self._dump("profile.html", r.text)
        if "login.php?back_url" in r.text or "login.php" in (r.url or ""):
            return ""
        return r.text

    def header_identity(self) -> dict:
        """상단 프로필 줄(사람 아이콘 + 이름/부서)에서 로그인 사용자 이름을 읽는다.

        웹메일이 개인정보 페이지를 막아 둔 경우에도 상단 줄은 항상 있으므로 마지막 보험이다.
        """
        r = self._get(LIST_URL + "?mb_id=inbox&page_size=5")
        self._dump("header_identity.html", r.text)
        out = {"name": "", "dept": ""}
        soup = BeautifulSoup(r.text, "html.parser")
        li = soup.select_one("li.login-bi") or soup.select_one("ul.navbar-right li.dropdown")
        anchor = li.find("a") if li is not None else None
        if anchor is None:
            return out
        for b in anchor.find_all("b"):
            txt = _clean(b.get_text(" ", strip=True))
            if not txt or out["dept"]:
                continue
            # 「납입1팀」처럼 부서만 따로 적힌 경우(이름은 <font> 안에 온다)
            if re.search(r"(팀|부|본부|실|센터|사업부|파트)$", txt) and not split_name_title(txt)[1]:
                out["dept"] = txt
        box = anchor.find("font") or anchor
        name = _clean(box.get_text(" ", strip=True))
        name = re.sub(r"[\u25b2\u25bc\u25be\u25b4\s]+$", "", name).strip()
        out["name"] = name
        return out

    # -- 사내 주소록(조직도 + 개인주소록) ------------------------------------
    def contact_group_tree(self) -> list[dict]:
        """주소록 그룹 트리(address_group.php) → 조직도·개인주소록 그룹 목록."""
        r = self._get(GROUP_URL)
        self._dump("address_group.html", r.text)
        if "login.php?back_url" in r.text or "login.php" in (r.url or ""):
            raise NmailError("주소록 화면이 로그인 페이지로 돌아갔습니다 (세션 만료)")
        groups = parse_contact_tree(r.text)
        if not groups:
            raise NmailError("주소록 그룹 트리를 읽지 못했습니다 (화면 구조 변경?)")
        return groups

    def contact_rows(self, path: str, param: str, no: str, *, kind: str,
                     max_pages: int = 20, pause: float = 0.25) -> list[dict]:
        """한 그룹의 명단을 끝까지(20건/페이지) 읽는다.

        path: /member_list.php(조직도) · /addr_list.php(개인주소록), param: s_mg_no · s_adg_no
        """
        rows: list[dict] = []
        total = 0
        for page in range(1, max_pages + 1):
            url = f"{BASE}{path}?{param}={no}&is_iframe=1" + (f"&pageID={page}" if page > 1 else "")
            r = self._get(url)
            if page == 1:
                self._dump(f"contacts_{kind}_{no}_p1.html", r.text)
                total = contact_total(r.text)
            if "login.php?back_url" in r.text:
                raise NmailError("주소록 목록이 로그인 페이지로 돌아갔습니다 (세션 만료)")
            batch = parse_org_contacts(r.text) if kind == "org" else parse_personal_contacts(r.text)
            if not batch:
                break
            rows.extend(batch)
            if not total or len(rows) >= total:
                break
            time.sleep(pause)
        return rows

    def fetch_contacts(self, *, pause: float = 0.25, max_groups: int = 400,
                       report: dict | None = None) -> list[dict]:
        """사내 주소록 전체(조직도 + 개인주소록)를 읽어 data["contacts"] 형식으로 돌려준다.

        조직도는 부서 그룹마다 명단을 받아 그룹 이름을 dept 로 붙인다(서버 주소록에 직위 칸이 비어
        있어 직급은 이름에 붙은 표기에서만 얻어진다). 개인주소록은 조직도와 같은 메일은 버리고
        외부 담당자·협력사만 남긴다. 실패는 호출하는 쪽에서 경고로 처리한다.
        """
        groups = self.contact_group_tree()
        org_groups = [g for g in groups if g["kind"] == "org" and g["no"] not in ("", "0")]
        per_groups = [g for g in groups if g["kind"] == "personal" and g["no"] not in ("", "0")]

        org: list[dict] = []
        for g in org_groups[:max_groups]:
            try:
                got = self.contact_rows(MEMBER_LIST_PATH, "s_mg_no", g["no"], kind="org", pause=pause)
            except NmailError:
                raise
            except Exception as e:  # noqa: BLE001 - 한 그룹 실패가 전체를 멈추지 않게
                got = []
                if report is not None:
                    report.setdefault("warnings", []).append(
                        f"주소록 그룹 '{g['name']}'(mg_no={g['no']}) 읽기 실패: {type(e).__name__}")
            org.extend({**row, "dept": contact_dept(g["name"])} for row in got)
            time.sleep(pause)

        personal: list[dict] = []
        for g in per_groups:
            try:
                got = self.contact_rows(ADDR_LIST_PATH, "s_adg_no", g["no"], kind="personal", pause=pause)
            except NmailError:
                raise
            except Exception as e:  # noqa: BLE001
                got = []
                if report is not None:
                    report.setdefault("warnings", []).append(
                        f"개인주소록 '{g['name']}'(adg_no={g['no']}) 읽기 실패: {type(e).__name__}")
            personal.extend({**row, "dept": contact_dept(g["name"])} for row in got)
            time.sleep(pause)

        out = merge_contacts(org, personal)
        if report is not None:
            report["contacts"] = {
                "org": sum(1 for c in out if c["kind"] == "org"),
                "personal": sum(1 for c in out if c["kind"] == "personal"),
                "total": len(out), "groups": len(org_groups) + len(per_groups),
            }
        if not out:
            raise NmailError("주소록에서 이름·메일을 하나도 읽지 못했습니다")
        return out

    def seed_mailbox(self, box: str, *, days: int = 14, limit: int = 30) -> list[dict]:
        """최근 N일 목록을 limit 건까지만 읽는다(첫 실행 자동 채우기용)."""
        out: list[dict] = []
        for page in range(1, 6):
            rows = self.list_page(box, page=page, page_size=100, s_date=max(1, int(days)))
            if not rows:
                break
            out.extend(rows)
            if len(out) >= limit:
                break
            time.sleep(0.4)
        seen, uniq = set(), []
        for row in sorted(out, key=lambda x: x["mf_no"], reverse=True):
            if row["mf_no"] in seen:
                continue
            seen.add(row["mf_no"])
            uniq.append(row)
            if len(uniq) >= limit:
                break
        return uniq

    # -- 목록 ------------------------------------------------------------
    def list_page(self, box: str, *, page: int = 1, page_size: int = 100, s_date: int = 182) -> list[dict]:
        """목록 한 페이지(기본 100건). s_date 는 '최근 N일' 필터."""
        url = f"{LIST_URL}?mb_id={box}&page_size={page_size}&s_date={s_date}&pageID={page}"
        r = self._get(url)
        self._dump(f"list_{box}_s{s_date}_p{page}.html", r.text)
        if "login.php?back_url" in r.text:
            raise NmailError("목록 요청이 로그인 화면으로 돌아갔습니다 (세션 만료)")
        return parse_list(r.text, box)

    def list_mailbox(self, box: str, *, pages: int = 3, page_size: int = 100, stop_below: int = 0) -> list[dict]:
        """받은/보낸편지함 목록을 최신순으로 pages 페이지까지 읽는다.

        stop_below: 이 값 이하 mf_no 가 나오면 더 읽지 않는다(기준점 이후만 필요).
        """
        out: list[dict] = []
        for page in range(1, pages + 1):
            rows = self.list_page(box, page=page, page_size=page_size)
            if not rows:
                break
            out.extend(rows)
            if stop_below and min(row["mf_no"] for row in rows) <= stop_below:
                break
            time.sleep(0.4)
        # 중복 제거 + 최신순
        seen, uniq = set(), []
        for row in sorted(out, key=lambda x: x["mf_no"], reverse=True):
            if row["mf_no"] in seen:
                continue
            seen.add(row["mf_no"])
            uniq.append(row)
        return uniq

    # -- 본문 ------------------------------------------------------------
    def read_mail(self, box: str, mf_no: int) -> dict:
        r = self._get(READ_URL + f"?mb_id={box}&mf_no={mf_no}")
        self._dump(f"read_{box}_{mf_no}.html", r.text)
        detail = parse_read(r.text)
        detail["mf_no"] = mf_no
        detail["box"] = box
        detail["mailLink"] = MAIL_LINK % (box, mf_no)
        return detail

    # -- 첨부/이미지 ------------------------------------------------------
    def fetch_bytes(self, url: str, *, cap: int = 400_000) -> bytes | None:
        if url.startswith("/"):
            url = BASE + url
        try:
            r = self.s.get(url, timeout=self.timeout)
            if r.status_code != 200 or not r.content or len(r.content) > cap:
                return None
            return r.content
        except requests.RequestException:
            return None


# ────────────────────────────────────────────────────────────── 파서 (실제 웹메일 HTML 기준)
_DATE_RE = re.compile(r"(\d{2}|\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _norm_addr(raw: str) -> str:
    """`"이름" <a@b.c>` → `이름 <a@b.c>` (따옴표만 정리)."""
    s = _clean(raw).replace("&quot;", '"')
    s = re.sub(r'^"([^"]*)"\s*<', r"\1 <", s)
    return s.strip()


def _addr_email(raw: str) -> str:
    m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", raw or "")
    return m.group(0).lower() if m else ""


def _norm_date(text: str) -> str:
    """`26-11-23 09:58` / `2026-11-23 09:50:04` → `2026-11-23 09:58`."""
    m = _DATE_RE.search(_clean(text))
    if not m:
        return ""
    y, mo, d, hh, mm = m.groups()
    if len(y) == 2:
        y = "20" + y
    return f"{y}-{mo}-{d} {hh}:{mm}"


def parse_list(html: str, box: str = "inbox") -> list[dict]:
    """목록 페이지 → [{mf_no, sender_raw, sender_email, subject, receivedAt, size, unread, attach, important}]"""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    for tr in soup.select("table.table-condensed tr[id]"):
        m = re.fullmatch(r"tr(\d+)", (tr.get("id") or "").strip())
        if not m:
            continue
        mf_no = int(m.group(1))
        from_div = tr.select_one("div.mail-list-from")
        sender_raw = _norm_addr(from_div.get("title", "")) if from_div else ""
        if not sender_raw and from_div:
            sender_raw = _clean(from_div.get_text(" ", strip=True))
        subject, href_mf = "", None
        for a in tr.select("a[href]"):
            href = a.get("href") or ""
            if "mail_read.php" in href:
                subject = _clean(a.get_text(" ", strip=True))
                mm = re.search(r"mf_no=(\d+)", href)
                if mm:
                    href_mf = int(mm.group(1))
                break
        nums = [_clean(td.get_text(" ", strip=True)) for td in tr.select("td.nm_number")]
        received = _norm_date(nums[0]) if nums else ""
        size = nums[1].replace(" ", "") if len(nums) > 1 else ""
        unread = bool(tr.select_one("span.unread-mail"))
        attach = bool(tr.select_one("span.glyphicon-paperclip")) or bool(tr.select_one("span.bi-paperclip"))
        important = bool(tr.select_one("img.mail-importance.high")) or "중요도 높음" in str(tr)
        # 목록만 봐도 본문 미리보기가 들어 있다(편지읽기 페이지를 열지 않아도 되므로 읽음 처리가 없다)
        prev = tr.select_one("a.btn-icon-preview")
        preview = (prev.get("data-preview") or "").strip() if prev else ""
        rows.append({
            "mf_no": mf_no,
            "box": box,
            "sender_raw": sender_raw,
            "sender_email": _addr_email(sender_raw),
            "subject": subject,
            "receivedAt": received,
            "size": size,
            "unread": unread,
            "attach": attach,
            "important": important,
            "preview": preview,
            "mailLink": MAIL_LINK % (box, mf_no),
            "listMfMismatch": (href_mf is not None and href_mf != mf_no),
        })
    return rows


def _header_map(soup: BeautifulSoup) -> dict[str, Any]:
    """편지읽기 헤더 표(보낸날짜·보낸사람·받는사람·참조·제목·첨부파일)를 dict 로."""
    out: dict[str, Any] = {}
    table = None
    for cand in soup.select("table"):
        if any("보낸날짜" in _clean(th.get_text(" ", strip=True)) for th in cand.find_all("th")):
            table = cand
            break
    if table is None:
        table = soup.select_one("table.table-bordered")
    if not table:
        return out
    for tr in table.select("tr"):
        th = tr.find("th")
        if not th:
            continue
        key = _clean(th.get_text(" ", strip=True))
        td = th.find_next_sibling("td")
        if td is None:
            continue
        out[key] = td
    return out


def _img_px(value) -> int | None:
    """`width="120"` · `width="120px"` → 120. `100%`·빈 값은 None(크기를 단정하지 않는다)."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(?:px)?\s*$", str(value or ""), re.I)
    return int(float(m.group(1))) if m else None


def _img_size(img) -> tuple[int | None, int | None]:
    """HTML width/height 속성 또는 인라인 style 에서 픽셀 크기를 읽는다."""
    w, h = _img_px(img.get("width")), _img_px(img.get("height"))
    style = str(img.get("style") or "")
    if w is None:
        m = re.search(r"width\s*:\s*(\d+(?:\.\d+)?)\s*px", style, re.I)
        w = int(float(m.group(1))) if m else None
    if h is None:
        m = re.search(r"height\s*:\s*(\d+(?:\.\d+)?)\s*px", style, re.I)
        h = int(float(m.group(1))) if m else None
    return w, h


def _hint_hit(text: str, hints: tuple[str, ...]) -> bool:
    low = str(text or "").lower()
    if not low:
        return False
    for hint in hints:
        if hint in SHORT_HINTS:
            if re.search(rf"(?<![a-z]){re.escape(hint)}(?![a-z])", low):
                return True
        elif hint in low:
            return True
    return False


def _in_sign_area(img) -> bool:
    """인용·서명 컨테이너 안의 이미지인가 (`[id*=sign]`·`[class*=sign]`·blockquote·gmail_quote)."""
    for parent in img.parents:
        name = getattr(parent, "name", None)
        if not name:
            break
        if name in ("body", "html"):
            break
        if name == "blockquote":
            return True
        tokens = [parent.get("id") or ""] + list(parent.get("class") or [])
        for token in tokens:
            for part in re.split(r"[\s_\-.]+", str(token).lower()):
                if part in SIGN_AREA_HINTS:
                    return True
    return False


def is_signature_image(img) -> bool:
    """본문 사진이 아니라 로고·CI·슬로건·아이콘·서명 이미지인가."""
    if _in_sign_area(img):
        return True
    if _hint_hit(str(img.get("src") or "") + " " + str(img.get("alt") or ""), SIGN_IMG_HINTS):
        return True
    w, h = _img_size(img)
    if w is not None and w < SIGN_IMG_MIN_W:
        return True
    if h is not None and h < SIGN_IMG_MIN_H:
        return True
    if w and h and h <= SIGN_IMG_BANNER_H and w >= 4 * h:
        return True
    return False


def image_size(blob: bytes) -> tuple[int, int] | None:
    """PNG/GIF/JPEG/WebP 바이트에서 실제 (width, height) 를 읽는다(외부 라이브러리 없이)."""
    try:
        if blob[:8] == b"\x89PNG\r\n\x1a\n" and len(blob) >= 24:
            w, h = struct.unpack(">II", blob[16:24])
            return int(w), int(h)
        if blob[:4] == b"GIF8" and len(blob) >= 10:
            w, h = struct.unpack("<HH", blob[6:10])
            return int(w), int(h)
        if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP" and len(blob) >= 30:
            if blob[12:16] == b"VP8X":                       # 확장 포맷 — 24비트 (크기-1)
                w = int.from_bytes(blob[24:27], "little") + 1
                h = int.from_bytes(blob[27:30], "little") + 1
                return w, h
            if blob[12:16] == b"VP8L":                       # 무손실 — 14비트씩
                b = int.from_bytes(blob[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
            if blob[12:16] == b"VP8 " and blob[23:26] == b"\x9d\x01\x2a":   # 손실 — 시작코드 뒤 14비트
                w, h = struct.unpack("<HH", blob[26:30])
                return w & 0x3FFF, h & 0x3FFF
        if blob[:2] == b"\xff\xd8":
            i, n = 2, len(blob)
            while i + 9 < n:
                if blob[i] != 0xFF:
                    i += 1
                    continue
                marker = blob[i + 1]
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                    i += 2 if marker != 0xFF else 1
                    continue
                seg = struct.unpack(">H", blob[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", blob[i + 5:i + 9])
                    return int(w), int(h)
                i += 2 + seg
    except (struct.error, IndexError, ValueError):
        return None
    return None


def is_banner_or_icon(size: tuple[int, int] | None) -> bool:
    """실제 픽셀 크기가 회사 슬로건 배너·서명 배너·소형 아이콘인가.

    ★ 2026-11-24 회귀 수정: 본문의 **가로로 넓은 표 캡처**(1200x110 · 1217x135)가
      비율만 보고 잘려 나갔다. 슬로건 배너는 폭이 좁고(425 · 500 · 570) 납작하므로,
      가로 배너 판정은 `w < IMG_BANNER_MAX_W(700)` 일 때만 적용한다.
      폭 700 이상의 납작한 이미지는 표·현황 캡처로 보고 살린다.
    """
    if not size:
        return False                    # 크기를 못 읽으면 사진으로 본다(내용을 잃지 않게)
    w, h = size
    if w <= IMG_SMALL_W or h <= IMG_SMALL_H:
        return True                     # 작은 아이콘·구분선
    if w < IMG_BANNER_MAX_W and h <= IMG_BANNER_H and w >= IMG_BANNER_RATIO * h:
        return True                     # 가로형 슬로건·서명 배너(폭 700 미만)
    return False


def parse_read(html: str) -> dict:
    """편지읽기 페이지 → {date, from_raw, from_email, to[], cc[], subject, bodyText, attachments[], images[]}"""
    soup = BeautifulSoup(html, "html.parser")
    hdr = _header_map(soup)

    date = _norm_date(_clean(hdr["보낸날짜"].get_text(" ", strip=True))) if "보낸날짜" in hdr else ""
    from_raw = ""
    if "보낸사람" in hdr:
        a = hdr["보낸사람"].find("a", title=True)
        from_raw = _norm_addr(a["title"]) if a and a.get("title") else _clean(hdr["보낸사람"].get_text(" ", strip=True))
        from_raw = re.sub(r"\s*(주소록에 추가|수신허용|수신거부).*$", "", from_raw).strip()

    def people(key: str) -> list[str]:
        td = hdr.get(key)
        if td is None:
            return []
        anchors = td.find_all("a", title=True)
        if anchors:
            return [_norm_addr(a["title"]) for a in anchors]
        return [_norm_addr(x) for x in _clean(td.get_text(" ", strip=True)).split(",") if _clean(x)]

    subject = ""
    if "제 목" in hdr:
        b = hdr["제 목"].find("b")
        subject = _clean((b or hdr["제 목"]).get_text(" ", strip=True))

    body_el = soup.select_one("#mf_body_view")
    body_text = ""
    if body_el is not None:
        body_text = el_text(body_el)
    elif len(soup.select("table")) > 4:
        body_text = el_text(soup.select("table")[4])

    attachments = []
    for a in soup.select("td.mf_attach_names_wrap li a"):
        size_el = a.select_one("span.mf_attach_name_size")
        size = _clean(size_el.get_text(" ", strip=True)) if size_el else ""
        if size_el:
            size_el.extract()
        name = _clean(a.get_text(" ", strip=True))
        attachments.append({"name": name, "size": _pretty_size(size)})

    images = []
    if body_el is not None:
        for img in body_el.find_all("img"):
            src = (img.get("src") or "").strip()
            if not src or src.startswith("data:") or src.startswith("cid:"):
                continue
            if src.startswith("/"):
                src = BASE + src
            if not src.startswith("http"):
                continue
            if is_signature_image(img):
                continue            # 회사 로고·CI·슬로건·아이콘·서명 이미지는 본문 사진이 아니다
            images.append({"src": src, "alt": _clean(img.get("alt", ""))})

    return {
        "date": date,
        "from_raw": from_raw,
        "from_email": _addr_email(from_raw),
        "to": people("받는사람"),
        "cc": people("참조"),
        "subject": subject,
        "bodyText": body_text,
        "attachments": attachments,
        "images": images,
    }


def _pretty_size(raw: str) -> str:
    """`57.58K` → `57.58 KB` (표시 형식 통일)."""
    m = re.fullmatch(r"([\d.,]+)\s*([KMG]?)B?", (raw or "").strip(), re.I)
    if not m:
        return raw or ""
    num, unit = m.group(1), (m.group(2) or "").upper()
    return f"{num} {unit}B" if unit else num


# ────────────────────────────────────────────────────────────── 본문 정리
def strip_quoted(text: str) -> str:
    """인용 헤더 이전까지만 남긴다(내용 판단용)."""
    low = text.lower()
    cut = len(text)
    for marker in QUOTE_MARKERS:
        idx = low.find(marker, 60)
        if 0 < idx < cut:
            cut = idx
    return text[:cut].strip()


# 본문 하단의 슬로건·기밀 면책 문구 — 줄 머리에서만 인정한다(본문 중간의 'Tel.' 오인 방지).
#  ★ 직원 서명은 남긴다(2026-11-24 사용자 정정: "서명은 보여야대, 필요없는 회사 슬로건 같은게 보인다는거야").
#    — 서명 구분선 · 회사명 줄 · 소속/직급/이름 · 주소·TEL/Fax/Mobile/E-Mail 은 자르지 않는다.
#    걸러 내는 것은 회사 슬로건과 기밀·법적 면책 문구뿐이다.
SIGN_TEXT_PATTERNS = (
    r"(?:gm\s*)?confidential",                       # GM Confidential · CONFIDENTIAL
    r"본\s*메일은\s*지정된\s*수신인",                 # 국문 기밀 면책
    r"[\s(（\[·•\-]*본\s*메일.*(?:승인용도|사용할\s*수\s*없습니다)",   # 전자결재 면책(괄호·머리표 허용)
    r"이\s*메일은\s*.*(?:수신인|대상으로)",
    r"무단\s*전재",
    r"무단\s*배포",
    r"the\s*next\s*100",                             # 회사 슬로건
    r"hi\s*,?\s*chunil",                             # 회사 슬로건(Hi CHUNIL, High green)
    r"high\s*green",
    r"70\s*년의\s*경험",                             # 회사 슬로건
)
SIGN_TEXT_AFTER = 60            # 본문 앞 60자 안의 같은 낱말은 본문으로 본다


def is_sign_text(line: str) -> bool:
    low = re.sub(r"\s+", " ", str(line or "")).strip().lower()
    return any(re.match(p, low) for p in SIGN_TEXT_PATTERNS)


def strip_signature(text: str) -> str:
    """본문 하단의 슬로건·기밀 면책 문구를 잘라낸다(직원 서명·연락처는 남긴다).

    - 줄 머리에서 시작하는 줄만 인정한다.
    - 앞 SIGN_TEXT_AFTER 자 안에서는 건드리지 않는다(첫 줄이 면책 낱말로 시작해 통째로 날아가는 것 방지).
    - 같은 낱말이 본문 중간에 나와도 그 줄이 앞머리가 아니면 그대로 둔다.
    """
    text = str(text or "")
    pos = 0
    for line in text.splitlines(keepends=True):
        if pos >= SIGN_TEXT_AFTER and is_sign_text(line):
            return text[:pos].strip()
        pos += len(line)
    return text.strip()


# ────────────────────────────────────────────────────────────── 본문 텍스트 추출
#  웹메일 본문은 <div>/<span> 조각으로 나뉜다. get_text("\n") 은 요소 경계마다 줄을 바꿔
#  `<span>11</span>월 <span>24</span>일` 을 `11\n월\n24\n일` 로 찢는다.
#  그래서 **블록 태그 경계에서만** 줄을 바꾸고, 인라인 태그(span·b·font 등)는 붙여 쓴다.
BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "fieldset",
    "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "tfoot", "thead",
    "caption", "tr", "ul",
})
SKIP_TAGS = frozenset({"script", "style", "head", "noscript", "template"})
CELL_TAGS = frozenset({"td", "th"})              # 표 칸은 붙으면 안 되니 공백으로 나눈다
VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "wbr"})


#  v1.2.7 — 웹메일 편집기가 <p>/<div> 를 문장 조각마다 쓰는 경우가 많아
#  블록 경계는 '이어 붙일 수 있는 줄바꿈'(\n), <br> 는 '진짜 줄바꿈'(\x01),
#  표 <tr> 행은 '행 경계'(\x02) 로 표시한 뒤 join_body_text() 가 문장을 이어 붙인다.
HARD_BR = "\x01"
HARD_ROW = "\x02"
HARD_PARA = "\x03"           # 글자 없는 빈 블록 = 단락 구분

#  이어 붙이지 않는(줄바꿈을 유지하는) 조건 — index.html 의 formatBodyText() 와 같은 규칙
BODY_SENT_END = re.compile(r"(?:[.?!:;。…~]|다|요|니다|바랍니다|드립니다|세요|함|임|음|\))\s*$")
BODY_LIST_HEAD = re.compile(r"^\s*(?:[-*•·○◦▶▪※#>]|\(?\d{1,2}[).]\s|[①-⑳]|[가-힣]\.\s)")
BODY_LABEL_HEAD = re.compile(r"^\s*[가-힣A-Za-z][^:\n\d]{0,11}\s*[:：]\s*\S")   # `07:00` 같은 시각은 라벨이 아니다
BODY_QUOTE_HEAD = re.compile(
    r"^\s*(?:from|sent|to|cc|bcc|subject|date|보낸\s*사람|받는\s*사람|참조|제목|보낸\s*날짜)\s*[:：]", re.I)
BODY_URL_RE = re.compile(r"(?:https?://|www\.)\S", re.I)
BODY_ADDR_LINE = re.compile(r"^[^\n]{0,20}(?:대리|과장|차장|부장|사원|팀장|대표|님|제위)\s*[,.]?$")
BODY_MARK_LINE = re.compile(r"^\s*(?:—|–|-{2,}|={2,}|\*{3,})")
BODY_FENCE = re.compile(r"^\s*[*=_\-—–]{3,}\s*$")          # ***…*** 처럼 구분선만 있는 줄 = 서명 울타리
BODY_SIG_MARK = re.compile(r"^—\s*서명\s*—")
BODY_UNIT_TAIL = re.compile(r"^[월일년시분초대개건명원층호차%]")
BODY_CELL_GAP = "  "          # 표 행 — <td> 칸 사이는 두 칸 공백
BODY_JOIN_MAX = 30            # 두 줄이 모두 이보다 길면 서로 다른 문장으로 본다


def _body_keeps_break(prev: str, cur: str) -> bool:
    """앞 줄과 이어 붙이지 않고 줄바꿈을 유지해야 하는가."""
    if not prev:
        return True
    if BODY_CELL_GAP in prev or BODY_CELL_GAP in cur:              # 표 행
        return True
    if BODY_SENT_END.search(prev):                                 # 문장이 끝났다
        return True
    if BODY_LIST_HEAD.match(cur) or BODY_LABEL_HEAD.match(cur):    # 새 항목
        return True
    if BODY_LABEL_HEAD.match(prev):                                # `기한: 12/2` 같은 라벨 줄 다음은 새 항목
        return True
    if BODY_QUOTE_HEAD.match(cur) or BODY_MARK_LINE.match(cur) or BODY_MARK_LINE.match(prev):
        return True
    if BODY_FENCE.match(cur) or BODY_FENCE.match(prev):            # 서명 울타리(***…***)
        return True                                               # 인용 헤더·서명/인용 구분선
    if BODY_URL_RE.search(cur):                                    # 주소
        return True
    if BODY_ADDR_LINE.match(prev) or BODY_ADDR_LINE.match(cur):   # 인사·수신인 호칭
        return True
    if len(prev.strip()) > BODY_JOIN_MAX and len(cur.strip()) > BODY_JOIN_MAX:
        return True                                               # 둘 다 긴 줄이면 서로 다른 문장
    return False


def _body_join(prev: str, cur: str) -> str:
    if re.search(r"\d$", prev) and BODY_UNIT_TAIL.match(cur):
        return prev + cur                                          # 11 + 월 → 11월
    return prev + " " + cur


def join_body_text(text: str) -> str:
    """문장 중간에 끊긴 줄을 이어 붙인다 (\\x01 · \\x02 는 진짜 줄바꿈으로 살린다).

    `하기 품번 명일\\n07:00\\n기준 재고실사 요청드립니다` → `하기 품번 명일 07:00 기준 재고실사 요청드립니다`
    빈 줄(단락)·글머리표·라벨·URL·인용 헤더·서명·인사 호칭·표 행은 그대로 둔다.
    """
    out: list[str] = []
    in_sig = False
    hard = True
    for piece in re.split(r"([\n\x01\x02])", str(text or "").replace(HARD_PARA, "\n\n")):
        if piece in ("\n", HARD_BR, HARD_ROW):
            hard = piece != "\n"
            continue
        line = re.sub(r"[ \t\u200b\ufeff]+$", "", re.sub(r"^[ \t\u200b\ufeff]+", "", piece))
        line = re.sub(r"[ \t]{2,}", BODY_CELL_GAP, line).rstrip()
        if not line:
            in_sig = False
            if out and out[-1] != "":
                out.append("")                                     # 빈 줄은 한 번만
            continue
        if BODY_FENCE.match(line):                                # *** 울타리 안은 서명 블록
            in_sig = not in_sig
            out.append(line)
            continue
        prev = out[-1] if out else ""
        if prev and not hard and not in_sig and not _body_keeps_break(prev, cur=line):
            out[-1] = _body_join(prev.rstrip(), line)
        else:
            out.append(line)
        if BODY_SIG_MARK.match(line):
            in_sig = True                                          # 서명 블록은 줄을 그대로 둔다
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _one_break(m: "re.Match[str]") -> str:
    """붙어 있는 줄바꿈 표시를 하나로 줄인다 — <div> 닫힘 + 다음 <div> 열림의 이중 \\n 정리."""
    s = m.group(0)
    br = s.count(HARD_BR)
    if HARD_PARA in s or br >= 2:
        return "\n\n"                                              # 빈 블록·<br><br> 는 단락
    if HARD_ROW in s:
        return HARD_ROW
    if br == 1:
        return HARD_BR
    return "\n"


def el_text(el: Any) -> str:
    """본문 요소 → 글자 — 인라인 조각은 붙여 쓰고, 블록 경계는 이어 붙일 수 있는 줄바꿈으로 남긴다.

    `<div><span>11</span>월 <span>24</span>일</div>` → `11월 24일`
    `<p>하기 품번 명일</p><p>07:00</p><p>기준 재고실사 요청드립니다</p>`
        → `하기 품번 명일 07:00 기준 재고실사 요청드립니다`
    `<br>` 는 진짜 줄바꿈으로 남기고, 표 `<tr>` 행은 `\\n`, `<td>` 칸은 ` ` 공백으로 유지한다.
    """
    parts: list[str] = []

    def walk(node: Any) -> None:
        name = getattr(node, "name", None)
        if name is None:                                  # NavigableString
            parts.append(str(node))
            return
        name = name.lower()
        if name in SKIP_TAGS:
            return
        if name == "br":
            parts.append(HARD_BR)                         # <br> 은 진짜 줄바꿈
            return
        if name == "tr":
            parts.append(HARD_ROW)                        # 표 행 경계
            for child in node.children:
                walk(child)
            parts.append(HARD_ROW)
            return
        block = name in BLOCK_TAGS
        # 글자 없는 빈 블록(<div><br></div>)은 단락 구분으로 본다
        brk = (HARD_PARA if not node.get_text(strip=True) else "\n") if block else None
        if brk:
            parts.append(brk)
        elif name in CELL_TAGS:
            parts.append(" ")
        if name not in VOID_TAGS:
            for child in node.children:
                walk(child)
        if brk:
            parts.append(brk)
        elif name in CELL_TAGS:
            parts.append(" ")

    walk(el)
    raw = "".join(parts).replace("\u00a0", " ")
    raw = re.sub(r"[\n\x01\x02\x03]+", _one_break, raw)
    return join_body_text(raw).strip()


def clean_body(text: str) -> str:
    """분석·저장에 쓰는 본문 — 인용 헤더와 슬로건·면책 문구만 걷어낸다(직원 서명은 남긴다)."""
    return strip_signature(strip_quoted(text or ""))


def mentions_me(text: str) -> bool:
    return bool(MENTION_RE.search(strip_quoted(text or "")))


# ────────────────────────────────────────────────────────────── 본문 속 마감 기한
#  `제출 기한 : 12월 2일` · `마감 : 12/2` · `~ 12/2 까지` · `회신 기한 12/2` 처럼 본문에 적힌
#  기한을 직접 읽는다. AI 분석이 없거나 실패해 기본 모드로 돌아도 캘린더·마감 배지가 맞게 동작한다.
DEADLINE_BEFORE = re.compile(
    r"(?:제출\s*기한|회신\s*기한|보고\s*기한|제출\s*일|회신\s*일|마감\s*(?:일|기한|일자)?|납기(?:일)?|기한)"
    r"\s*(?:은|는|이|가|을|를|:)?\s*[:：~∼\-–]?\s*$")
DEADLINE_AFTER = re.compile(
    r"^\s*(?:\([^)]{1,6}\)|\[[^\]]{1,6}\])?\s*(?:까지|까지임|까지입니다|까지\s*(?:제출|회신|송부)|until|by\b)", re.I)
MONTH_DAY = re.compile(
    r"(?<!\d)(?:(20\d{2})\s*[년./\-]\s*)?"
    r"(1[0-2]|0?[1-9])\s*[월./\-]\s*(3[01]|[12]\d|0?[1-9])\s*일?(?!\d)")


def deadline_date(text: str, ref: str = "") -> str | None:
    """본문·제목에서 '기한·마감·~까지' 가 붙은 날짜를 찾는다 → YYYY-MM-DD (없으면 None)."""
    s = str(text or "")
    if not s:
        return None
    ref_d = None
    m_ref = re.match(r"(20\d{2})-(\d{2})-(\d{2})", str(ref or ""))
    if m_ref:
        try:
            ref_d = dt.date(int(m_ref.group(1)), int(m_ref.group(2)), int(m_ref.group(3)))
        except ValueError:
            ref_d = None
    for m in MONTH_DAY.finditer(s):
        before = s[max(0, m.start() - 24):m.start()]
        after = s[m.end():m.end() + 12]
        if not (DEADLINE_BEFORE.search(before) or DEADLINE_AFTER.match(after)):
            continue
        year = int(m.group(1)) if m.group(1) else (ref_d.year if ref_d else dt.date.today().year)
        try:
            got = dt.date(year, int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if ref_d and got < ref_d - dt.timedelta(days=180):
            try:
                got = got.replace(year=year + 1)      # `1월 5일` 을 12월에 받은 메일
            except ValueError:
                pass
        return got.isoformat()
    return None


def deadline_label(ymd: str) -> str:
    """2026-12-02 → `12월 2일`"""
    m = re.match(r"(20\d{2})-(\d{2})-(\d{2})", str(ymd or ""))
    return f"{int(m.group(2))}월 {int(m.group(3))}일" if m else (ymd or "")



# ────────────────────────────────────────────────────────────── 분류 (make_data.py 규칙 그대로)
def load_contacts(data: dict) -> dict[str, dict]:
    out = {}
    for c in data.get("contacts") or []:
        email = (c.get("email") or "").lower()
        if email:
            out[email] = c
    return out


def sender_label(raw: str, email: str, contacts: dict[str, dict]) -> str:
    """`"이방형" <banghyung.lee@gm.com>` → `이방형 부장 (GM)`"""
    name, title, org = _sender_parts(raw, email, contacts)
    label = f"{name} {title}".strip() if title and title not in name else name
    return f"{label} ({org})".strip() if org else label.strip()


def _sender_parts(raw: str, email: str, contacts: dict[str, dict]) -> tuple[str, str, str]:
    contacts = contacts or {}
    info = contacts.get((email or "").lower()) or {}
    name = (info.get("name") or "").strip()
    if not name:
        name = re.sub(r"<[^>]*>", "", raw or "").replace('"', "").strip()
        name = re.split(r"[,(]", name)[0].strip()
    title = (info.get("title") or "").strip()
    dept = (info.get("dept") or "").strip()
    if (email or "").lower().endswith("@gm.com"):
        org = "GM"
    else:
        org = "" if dept.startswith("납입1팀") else dept
    return name, title, org


def classify(item: dict) -> None:
    """priority·vipRank·vipLabel·isVip·priorityReason 를 in-place 로 채운다."""
    title = item.get("title") or ""
    body = item.get("bodyText") or ""
    sender = item.get("sender") or ""
    email = (item.get("senderEmail") or "").lower()
    name = re.sub(r"\s*\(.*?\)\s*", " ", sender).strip()
    to_me = item.get("recipientRole") == "To"

    item["mentionedUser"] = mentions_me(body)
    item["isVip"] = False
    item["vipLabel"] = ""
    item["vipRank"] = 0
    for key, addr, label, rank, need_to in VIPS:
        hit = key in sender or key in name or (addr and addr.lower() == email)
        if not hit:
            continue
        if need_to and not to_me:
            continue
        item["isVip"], item["vipLabel"], item["vipRank"] = True, label, rank
        break

    gm_sender = email.endswith("@gm.com")
    bulk_list = any(k in title for k in P3_TITLE_KEYWORDS)
    category = item.get("category") or ""

    if item["vipRank"] == 1:
        item["priority"], item["priorityReason"] = 1, f"{P1_LABEL} · {item['vipLabel']} 발신"
    elif item["vipRank"] == 2:
        item["priority"], item["priorityReason"] = 1, f"{P1_LABEL} · {item['vipLabel']} 발신 + 수신인(To)"
    elif item["mentionedUser"]:
        who = OWNER.get("name") or "내 이름"
        item["priority"], item["priorityReason"] = 1, f"{P1_LABEL} · 본문에서 '{who}' 직접 지목"
        if item["vipRank"] == 0:
            item["vipRank"], item["vipLabel"] = 3, "1-3 지목"
    elif bulk_list:
        item["priority"], item["priorityReason"] = 3, P3_TITLE_REASON
    elif (not gm_sender) and (not to_me) and category in P3_CATEGORIES:
        item["priority"], item["priorityReason"] = 3, P3_CATEGORY_REASON
    elif to_me and CFG.get("to_is_urgent", True):
        item["priority"], item["priorityReason"] = 1, f"{P1_LABEL} · 나를 수신인(To)으로 지정"
    else:
        item["priority"] = 2
        if gm_sender:
            item["priorityReason"] = f"{P2_LABEL} · 고객사(GM) 발신 · 내가 지목된 건 아님"
        elif category in EXTERNAL_CATS:
            item["priorityReason"] = f"{P2_LABEL} · 사내 주요 이슈 흐름"
        else:
            item["priorityReason"] = f"{P2_LABEL} · 모니터링(내가 지목된 건 아님)"

    # 해외물류/선적이 주된 내용이고 나를 지목하지 않았으면 즉시 확인으로 올리지 않는다
    blob = (title + " " + body).lower()
    ship_hits = sum(blob.count(k) for k in SHIP_KEYWORDS)
    if (item["priority"] == 1 and not item["mentionedUser"] and item["vipRank"] != 1
            and (gm_sender or ship_hits >= 2) and ship_hits >= 2):
        item["priority"] = 2
        item["priorityReason"] = f"{P2_LABEL} · 해외물류/선적 건 (내가 지목된 건 아님)"


# ────────────────────────────────────────────────────────────── Gemini
class GeminiError(RuntimeError):
    pass


ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "actionItems": {"type": "array", "items": {"type": "string"}},
        "dueDate": {"type": "string", "nullable": True},
        "deadline": {"type": "string"},
        "replyStatus": {"type": "string", "enum": ["needs_reply", "no_reply_needed", "replied"]},
        "category": {"type": "string", "enum": CATEGORIES},
        "urgency": {"type": "string", "enum": ["normal", "high", "urgent"]},
        "threadGist": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "replyDrafts": {
            "type": "object",
            "properties": {
                "polite": {"type": "string"},
                "factual": {"type": "string"},
                "concise": {"type": "string"},
            },
        },
    },
    "required": ["summary", "actionItems", "replyStatus", "category", "urgency", "threadGist"],
}

SYSTEM_PROMPT = """당신은 천일정기화물자동차(주) 납입1팀 김성규 과장의 업무 비서다.
웹메일 한 통을 읽고 대시보드에 표시할 분석 결과를 한국어 JSON 으로만 출력한다.

[대상자의 업무]
- 자재 납입·출하·재고 승인, GM(고객사) 대응, 결품/PTR/EOP/용기승인, 협력사 보급 지시.
- 해외물류부 영역(항공/해상 B/L, 선적 부킹, 포워더, 통관, 선복)은 담당이 아니므로 actionItems 에서 제외한다.

[summary 작성 규칙]
- **본문 첫 줄이나 인사말을 그대로 옮기지 않는다.** "○○님," · "요청드립니다" · "안녕하십니까" 같은 문장이
  summary 에 남아 있으면 잘못 쓴 것이다. (예: 본문이 "피세영 대리님, 하기 품번 명일 07:00 기준 재고실사
  요청드립니다"인데 summary 도 같은 문장이면 실패)
- **반드시 제3자 보고체 1~2문장으로 쓴다**: 누가(소속·이름·직급) → 누구에게 → 무슨 사유/품번으로 →
  언제까지 무엇을 요청·공유·제출하는지.
  예) "GM 염주희 차장이 납입·해외물류부에 MY26 EOP 자재 입항지연으로 11/24 07:00 기준 재고파악을 요청한 건"
  예) "사내 해외물류부 류광종 과장이 납입1팀에 11/24 07:00 기준 2품번(13524683·13540045) 재고실사 자료
  회신을 요청한 건"
- 소속 표기: 보낸 사람 주소가 @gm.com 이면 "GM", @chunil.co.kr 이면 "사내"(서명·본문에 부서가 있으면
  "해외물류부" 처럼 그 부서). 받는 사람은 [받는사람] 의 첫 사람·팀을 "누구에게"로 쓴다.
- 그 뒤에 필요한 사실(품번·수량·기한·진행 상태·남은 일)만 덧붙인다. "이슈 → 진행 → 남은 일"은
  필요할 때 2번째 문장부터 쓴다.
- 수신인·참조 설명("수신 A·B(To)", "김성규 과장은 참조", "내 조치 없음")은 쓰지 않는다. 수신 정보는 별도 필드로 저장된다.
- 날짜·품번·수량·기한 같은 사실은 원문 그대로 옮긴다. 추측은 "확인 필요"로 표시한다.
- 본문이 리스트·표 위주면 무엇이 몇 건인지, 회신이 필요한지만 한 줄로 압축한다.

[actionItems]
- 김성규 과장이 실제로 해야 하는 일만 0~4개. 없으면 빈 배열.
- 보고/RE/FW 성격이거나 참조(CC)로 받은 단순 공유면 비운다.

[dueDate / deadline]
- 본문에 명시된 요청 기한을 YYYY-MM-DD 로. 없으면 dueDate 는 null, deadline 은 "별도 회신 기한 없음" 같은 한 줄.

[replyStatus]
- needs_reply: 김성규 과장이 답을 보내야 하는 요청·질의·확인 건.
- no_reply_needed: 단순 공유·참고·이미 종결된 건.
- replied: 본문에 이미 회신한 흔적이 뚜렷한 건.

[replyDrafts]
- replyStatus 가 needs_reply 일 때만 3가지 톤으로 쓴다(그 외에는 생략).
- polite(정중·양해), factual(핵심·사실), concise(사내·간결). 서명은 "김성규 드림" 또는 "김성규".

[category] 승인/검토, PTR, 결품/보고, 불량/품질, 재고/실사, 현황/보고, 출하/보급, 납입요청, 일정/공유,
조직/업무, 공지/안전, 자료요청, 자재/확인, 기타 중 하나.
[urgency] normal/high/urgent. [threadGist] 스레드에 한 줄로 남길 이 메일의 핵심(40자 이내).
[tags] 품번·차종·키워드 0~5개."""


def model_hint(text: str, *, current: str = "") -> str:
    """API 오류문에서 '… models/gemini-X 를 쓰세요' 의 모델 이름을 뽑는다.

    오류문에는 쓰던 모델('models/gemini-2.5-flash is no longer available')과 대체 모델
    ('use models/gemini-3.8-flash')가 함께 나오므로, current 와 다른 첫 이름을 고른다.
    """
    names = re.findall(r"models/([a-zA-Z0-9._-]+)", text or "")
    for name in names:
        if name != current:
            return name
    return ""


class Gemini:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *, timeout: int = 90, retries: int = 3):
        self.key, self.model, self.timeout, self.retries = api_key, model, timeout, retries
        self.switched = False           # 모델이 종료돼 다른 모델로 갈아탄 적이 있는지
        self.url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent")

    def _use_model(self, model: str) -> None:
        """API 가 알려 준 대체 모델로 갈아탄다(설정을 고치지 않아도 계속 돌게)."""
        print(f"  ! Gemini 모델 '{self.model}' 사용 불가 — '{model}' 로 전환합니다")
        self.model, self.switched = model, True
        self.url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent")

    def _request(self, payload: dict) -> dict:
        last = None
        for attempt in range(1, self.retries + 1):
            try:
                r = requests.post(self.url, params={"key": self.key}, json=payload,
                                  timeout=self.timeout, headers={"Content-Type": "application/json"})
            except requests.RequestException as e:            # 네트워크
                last = str(e)
            else:
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}: {r.text[:400]}"
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(min(2 ** attempt, 20))
                    continue
                if r.status_code == 404 and not self.switched:
                    hint = model_hint(r.text, current=self.model)
                    if hint:
                        self._use_model(hint)
                        continue        # 새 모델로 바로 다시 시도
                raise GeminiError(last)
            time.sleep(min(2 ** attempt, 20))
        raise GeminiError(f"Gemini 요청 실패: {last}")

    def analyze(self, mail: dict, *, kind: str = "받은편지함", brief: bool = False) -> dict:
        body = (mail.get("bodyText") or "")[:7000]
        lines = [
            f"[편지함] {kind}",
            f"[보낸날짜] {mail.get('date') or mail.get('receivedAt')}",
            f"[보낸사람] {mail.get('from_raw') or mail.get('sender_raw')}",
            f"[받는사람] {', '.join(mail.get('to') or []) or '-'}",
            f"[참조] {', '.join(mail.get('cc') or []) or '-'}",
            f"[제목] {mail.get('subject')}",
        ]
        if mail.get("attachments"):
            lines.append("[첨부] " + ", ".join(f"{a['name']} ({a['size']})" for a in mail["attachments"]))
        lines.append("\n[본문]\n" + clean_body(body))
        if brief:
            lines.append("\n[이번 요청] 위 메일은 과거 기록 보강용이다. summary 는 1~2줄(200자 이내)로 "
                         "핵심만 쓰고, actionItems 는 빈 배열, replyDrafts 는 생략한다.")
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": "\n".join(lines)}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": ANALYSIS_SCHEMA,
                "maxOutputTokens": 3000,
            },
        }
        # flash 계열은 thinking 을 끄고 JSON 출력에 토큰을 몰아준다(pro 계열은 thinking 최소치가 있어 건드리지 않음)
        if "flash" in self.model:
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
        data = self._request(payload)
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise GeminiError(f"Gemini 응답 형식 오류: {json.dumps(data, ensure_ascii=False)[:400]}") from e
        return json.loads(text)


# ────────────────────────────────────────────────────────────── 분석 대체(키 없음/실패 시)
GREETING_RE = (
    r"^(?:안녕하십니까|안녕하세요|수고하십니다|수고하셨습니다|반갑습니다|감사합니다)\s*[?!.,]*\s*",
    r"^천일정기화물자동차\s*(?:\(주\)|㈜)?[^.!?]{0,60}?(?:입니다|드립니다)\s*[.!]?\s*",
    r"^(?:수신자|수신)\s*제위\s*[.,]?\s*",
    r"^(?:업무에\s*)?노고가\s*많으십니다\s*[.!]?\s*",
)


def strip_greeting(text: str) -> str:
    """앞머리의 인사말·소속 소개를 걷어낸다 — 휴리스틱 요약이 인사말로 채워지지 않게.

    웹메일 본문은 <div> 단위로 잘려 '안녕하십니까 ? 천일정기화물자동차(주) … 입니다 .' 처럼
    조각나 있으므로, 공백을 정리한 한 줄에서 앞에서부터 최대 4번 반복해 떼어 낸다.
    """
    s = _clean(text or "")
    for _ in range(4):
        before = s
        for pat in GREETING_RE:
            s = re.sub(pat, "", s, count=1)
        if s == before:
            break
    return s.strip()


# ── 3인칭 요약용 조각 — 휴리스틱도 "누가 → 누구에게 → 무엇을" 로 쓴다 (v1.2.7)
ACTION_PATTERNS = (
    ("회신", r"회신|답변|회답"),
    ("제출", r"제출"),
    ("송부", r"송부|발송|전달"),
    ("확인", r"확인\s*(?:요청|부탁|바랍니다|해\s*주)"),
    ("요청", r"요청|부탁|요망|협조"),
    ("공유", r"공유|안내|공지|알림"),
)
ACTION_PHRASE = {
    "요청": "{t}{o} 요청하는 내용임",
    "회신": "{t} 회신을 요청하는 내용임",
    "제출": "{t} 제출을 요청하는 내용임",
    "송부": "{t}{o} 송부한 내용임",
    "확인": "{t}{o} 확인 요청하는 내용임",
    "공유": "{t}{o} 공유한 내용임",
    "안내": "{t}{o} 안내한 내용임",
    "전달": "{t}{o} 전달한 내용임",
}
REL_DAYS = ((r"명일|내일|익일", 1), (r"금일|오늘|당일|금일중", 0), (r"전일|어제|작일", -1))
FILLER_HEAD = re.compile(r"^(?:하기|아래|다음|상기|위|첨부|동봉|하기와\s*같이|하기와\s*같은)\s*(?:품번|품목|건|사항|자료|내용|안)?\s*")
REASON_WORDS = re.compile(r"EOP|PTR|LLLL?P|결품|지연|부족|과다|사고|불량|변경|긴급|추가|회수|격리")


def _josa(word: str, with_batchim: str, without: str) -> str:
    w = (word or "").strip()
    if not w:
        return without
    code = ord(w[-1])
    if 0xAC00 <= code <= 0xD7A3:
        return with_batchim if (code - 0xAC00) % 28 else without
    return without


def _addr_name(raw: str) -> str:
    s = re.sub(r"<[^>]*>", "", raw or "").replace('"', "")
    s = re.sub(r"\s*\((?:C|T|B|CC|TO|To|Cc)\)\s*$", "", s.strip())
    return s.strip(" ,;")


def _sender_phrase(mail: dict, contacts: dict | None) -> str:
    email = (mail.get("from_email") or _addr_email(mail.get("from_raw") or "")).lower()
    raw = mail.get("from_raw") or ""
    name, title, org = _sender_parts(raw, email, contacts or {})
    if not org:
        gm = email.endswith("@gm.com") or "(gm)" in raw.lower()
        org = "GM" if gm else ("사내" if (email or name) else "")
    name = _clean(name)
    if title and title not in name:
        name = f"{name} {title}".strip()
    return " ".join(x for x in (org, name) if x) or "발신자"


def _recipient_phrase(mail: dict, contacts: dict | None, to_me: bool) -> str:
    """`피세영 대리에게` — 받는사람(To) 첫 사람. 나만 있으면 `나에게`."""
    people: list[tuple[str, str]] = []
    me = (ME or str(OWNER.get("email") or "")).lower()
    for raw in mail.get("to") or []:
        email = _addr_email(raw)
        if email and email == me:
            continue
        info = (contacts or {}).get(email) or {}
        name = _clean(info.get("name") or "") or _addr_name(raw)
        if not name:
            continue
        people.append((name, _clean(info.get("title") or "")))
    if not people:
        return "나에게" if to_me else "수신 담당자에게"
    name, title = people[0]
    who = f"{name} {title}".strip() if title and title not in name else name
    rest = len(people) - 1
    return f"{who}{_josa(who, '에게', '에게')}" + (f" 외 {rest}명에게" if rest else "")


def _rel_date(text: str, ref: str) -> str:
    """`명일`·`오늘` 같은 상대 날짜를 받은 날짜 기준 `11/24` 로."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(ref or ""))
    if not m:
        return ""
    try:
        base = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return ""
    for pat, delta in REL_DAYS:
        if re.search(pat, text):
            d = base + dt.timedelta(days=delta)
            return f"{d.month}/{d.day}"
    return ""


def _gist_clause(body: str, title: str, ref: str) -> tuple[str, str]:
    """요청 문장에서 (무엇을, 행동) 을 뽑는다 → ("11/24 07:00 기준 재고실사", "요청")."""
    text = join_body_text(clean_body(body or ""))
    for line in text.split("\n"):
        s = _clean(line.replace(HARD_BR, " ").replace(HARD_ROW, " "))
        if len(s) < 3 or BODY_CELL_GAP in s or BODY_ADDR_LINE.match(s):
            continue
        for verb, pat in ACTION_PATTERNS:
            hits = list(re.finditer(pat, s))
            if not hits:
                continue
            m = hits[-1]                   # 같은 줄에 동사가 두 번 나오면 뒤엣것(마지막 지시)을 기준으로
            head = s[: m.start()].strip(" ,.;:·-—–")
            head = re.sub(r"^(?:제출|회신|보고|마감)\s*(?:기한|일|일자)\s*[:：]?\s*", "", head).strip()
            head = re.sub(r"^[^\n]{0,14}?(?:대리|과장|차장|부장|사원|팀장|님)\s*[,.]?\s*", "", head)
            head = re.sub(r"^(?:수신자?|참조)\s*제위\s*[,.]?\s*", "", head)
            head = FILLER_HEAD.sub("", head).strip()
            day = _rel_date(s, ref)
            if day:
                head = re.sub(r"^(?:명일|내일|익일|금일|오늘|당일|전일|어제|작일)\s*", day + " ", head)
            head = re.sub(r"\s{2,}", " ", head).strip(" ,.;:·-—–")
            if len(head) >= 2:
                return head[:70], verb
            # 머리말이 없으면("확인 부탁드립니다") 동사 낱말만 주제로 — 일반적인 "요청/부탁" 은 건너뛴다
            word = re.sub(r"\s*(?:부탁|요청|요망|협조|드립니다|합니다|바랍니다)", "", _clean(m.group(0))).strip()
            if word and word not in ("요청", "부탁", "협조", "요망", "회신", "제출", "송부", "공유", "안내", "전달"):
                return word[:20], verb
            break
    # 요청 문장을 못 찾으면 제목으로
    t = re.sub(r"^\s*(?:re|fw|fwd|답장|전달)\s*[:：]\s*", "", _clean(title), flags=re.I)
    t = re.sub(r"^\[[^\]]{1,12}\]\s*", "", t).strip()
    verb = "전달"
    for v, pat in ACTION_PATTERNS:
        if re.search(pat, t):
            verb = v
            break
    return (t[:70] or ""), verb


def _reason_clause(body: str) -> str:
    """`(MY26 EOP 자재로 입항지연 예상 )` 같은 괄호 사유 한 조각."""
    for m in re.finditer(r"\(([^()\n]{6,60})\)", body or ""):
        s = _clean(m.group(1)).strip(" ,.;:·-—–")
        if REASON_WORDS.search(s):
            return s
    return ""


def third_person_summary(mail: dict, contacts: dict | None = None, to_me: bool = False) -> tuple[str, str]:
    """휴리스틱 3인칭 요약 → (summary, threadGist). 본문을 그대로 옮기지 않는다."""
    body = mail.get("bodyText") or ""
    title = mail.get("subject") or ""
    ref = mail.get("date") or mail.get("receivedAt") or ""
    who = _sender_phrase(mail, contacts)
    rcpt = _recipient_phrase(mail, contacts, to_me)
    topic, verb = _gist_clause(body, title, ref)
    if not topic:
        return "", ""
    obj = _josa(topic, "을", "를")
    if topic.rstrip().endswith(verb):          # 주제가 이미 동사로 끝나면("제출") 겹쳐 쓰지 않는다
        phrase = f"{topic}{obj} 요청하는 내용임" if verb in ("요청", "회신", "제출", "확인") else f"{topic}{obj} 한 내용임"
    else:
        phrase = ACTION_PHRASE.get(verb, ACTION_PHRASE["전달"]).format(t=topic, o=obj)
    summary = f"{who}{_josa(who, '이', '가')} {rcpt} {phrase}"
    reason = _reason_clause(body)
    if reason and reason not in summary:
        summary += f" — {reason}"
    if len(summary) > 220:
        summary = summary[:218].rstrip() + "…"
    gist = (topic if topic.rstrip().endswith(verb) else f"{topic} {verb}").strip()[:60]
    return summary, gist

def _looks_copied_summary(summary: str, body: str) -> bool:
    """예전 휴리스틱이 본문 앞부분을 그대로 잘라 넣은 요약인가."""
    s = re.sub(r"\s+", "", summary or "")
    if len(s) < 20:
        return False
    for cand in (body or "", strip_greeting(body or "")):
        b = re.sub(r"\s+", "", cand)
        if b and b.startswith(s):
            return True
    return False


def refresh_copied_summaries(items: list[dict], contacts: dict | None = None) -> int:
    """이미 쌓인 메일 중 '본문 복붙' 요약을 3인칭 요약으로 바꾼다 (v1.2.7).

    AI 가 쓴 요약처럼 본문 앞부분과 같지 않은 요약은 건드리지 않는다.
    """
    n = 0
    for it in items or []:
        body = it.get("bodyText") or ""
        cur = it.get("summary") or ""
        if not _looks_copied_summary(cur, body):
            continue
        mail = {"subject": it.get("title") or "", "bodyText": body,
                "date": it.get("receivedAt") or "",
                "from_raw": it.get("sender") or "", "from_email": it.get("senderEmail") or "",
                "to": it.get("to") or [], "cc": it.get("cc") or []}
        summary, _gist = third_person_summary(mail, contacts, it.get("recipientRole") == "To")
        if summary and summary != cur:
            it["summary"] = summary
            n += 1
    return n



def heuristic_analysis(mail: dict, to_me: bool, contacts: dict | None = None) -> dict:
    body = clean_body(mail.get("bodyText") or "")
    core = strip_greeting(body) or body          # 인사말·소속 소개는 요약에서 뺀다
    first = _clean(core)[:280]
    title = mail.get("subject") or ""
    # 본문에 `제출 기한 : 12월 2일` 처럼 적힌 기한은 AI 없이도 그대로 읽어 dueDate 를 채운다
    due = deadline_date(title + "\n" + body, mail.get("date") or mail.get("receivedAt") or "")
    cat = "기타"
    for key, c in (("출하리스트", "출하/보급"), ("납입요청", "납입요청"), ("재고", "재고/실사"),
                   ("PTR", "PTR"), ("용기", "승인/검토"), ("승인", "승인/검토"), ("결품", "결품/보고")):
        if key.lower() in (title + body).lower():
            cat = c
            break
    summary, gist = third_person_summary(mail, contacts, to_me)
    return {
        "summary": summary or first or title,
        "actionItems": [],
        "dueDate": due,
        "deadline": (deadline_label(due) if due
                     else ("별도 회신 기한 없음" if not to_me else "확인 필요")),
        "replyStatus": "needs_reply" if to_me else "no_reply_needed",
        "category": cat,
        "urgency": "normal",
        "threadGist": gist or (first or title)[:60],
        "tags": [],
        "replyDrafts": None,
    }


# ────────────────────────────────────────────────────────────── 항목 만들기
def _norm_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title or "").lower()
    t = re.sub(r"\[\s*external\s*\]", " ", t)
    for _ in range(3):
        t = re.sub(r"^\s*(re|fw|fwd|답장|전달)\s*[:：]\s*", "", t)
    t = re.sub(r"\(수정본?[^)]*\)", " ", t)
    t = re.sub(r"[\[\](){}<>\"'“”‘’·ㆍ,.;:!?\-_/\\|]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _is_to(mail: dict, me: str | None = None) -> bool:
    """내가 받는사람(To)인지. me 를 주지 않으면 로그인 사용자(OWNER) 기준."""
    target = (me or ME or "").strip().lower()
    if not target:
        return False
    return any((_addr_email(x) == target) for x in (mail.get("to") or []))


def build_item(mail: dict, analysis: dict, contacts: dict[str, dict], now: dt.datetime) -> dict:
    to_me = _is_to(mail)
    name, title, _org = _sender_parts(mail.get("from_raw", ""), mail.get("from_email", ""), contacts)
    label = sender_label(mail.get("from_raw", ""), mail.get("from_email", ""), contacts)
    sender_name = f"{name} {title}".strip() if title and title not in name else name
    item = {
        "id": f"m-{mail['mf_no']}",
        "title": mail["subject"],
        "sender": label,
        "senderEmail": mail.get("from_email", ""),
        "receivedAt": mail.get("date") or "",
        "deadline": analysis.get("deadline") or "",
        "urgency": analysis.get("urgency") or "normal",
        "category": analysis.get("category") or "기타",
        "recipientRole": "To" if to_me else "CC",
        "mentionedUser": False,
        "dueDate": analysis.get("dueDate") or None,
        "replyStatus": analysis.get("replyStatus") or ("needs_reply" if to_me else "no_reply_needed"),
        "summary": analysis.get("summary") or "",
        "actionItems": analysis.get("actionItems") or [],
        "attachmentsUnverified": False,
        "replyDraft": None,
        "mailLink": mail["mailLink"],
        "to": mail.get("to") or [],
        "cc": mail.get("cc") or [],
        "thread": [{
            "id": f"m-{mail['mf_no']}",
            "date": mail.get("date") or "",
            "from": label,
            "gist": analysis.get("threadGist") or "",
            "link": mail["mailLink"],
        }],
        "senderName": sender_name,
        "priority": 2,
        "priorityReason": "",
        "mailbox": mail.get("box", "inbox"),
        "archived": False,
    }
    if mail.get("attachments"):
        item["attachments"] = mail["attachments"]
    if analysis.get("tags"):
        item["tags"] = analysis["tags"]
    body = clean_body(mail.get("bodyText") or "")
    if body:
        item["bodyText"] = body[:2500]
    if analysis.get("replyDrafts") and item["replyStatus"] == "needs_reply":
        item["replyDrafts"] = analysis["replyDrafts"]
    if mail.get("imagesData"):
        item["images"] = mail["imagesData"]
        item["imageCount"] = len(mail["imagesData"])
    classify(item)
    return item


def find_thread_target(items: list[dict], title: str, received: str, *, days: int = 14) -> dict | None:
    """같은 스레드(정규화 제목 동일) 항목을 찾는다. 최근 것만, 보관(archived) 제외."""
    key = _norm_title(title)
    if not key:
        return None
    try:
        new_at = dt.datetime.strptime(received[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        new_at = dt.datetime.now(KST).replace(tzinfo=None)
    best, best_at = None, None
    for it in items:
        if it.get("archived"):
            continue
        if _norm_title(it.get("title", "")) != key:
            continue
        stamp = str(it.get("lastAt") or it.get("receivedAt") or "")[:16]
        try:
            at = dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if abs((new_at - at).days) > days:
            continue
        if best_at is None or at > best_at:
            best, best_at = it, at
    return best


def merge_into_thread(item: dict, new_item: dict) -> None:
    """새 메일을 기존 항목의 스레드에 덧붙이고, 최신 메일 기준으로 앞면을 갱신한다."""
    entry = dict(new_item["thread"][0])
    item.setdefault("thread", []).append(entry)
    item["thread"].sort(key=lambda m: str(m.get("date") or ""))
    item["lastAt"] = max([str(m.get("date") or "") for m in item["thread"]] + [item.get("receivedAt") or ""])[:16]
    # 최신 메일이 내가 봐야 하는 등급이면 항목 등급을 올린다(내리는 경우는 유지 = 사용자가 확인한 것 보호)
    if new_item["priority"] < (item.get("priority") or 3):
        item["priority"] = new_item["priority"]
        item["priorityReason"] = new_item["priorityReason"]
    if new_item.get("dueDate") and not item.get("dueDate"):
        item["dueDate"] = new_item["dueDate"]
    if new_item["replyStatus"] == "needs_reply":
        item["replyStatus"] = "needs_reply"
        if new_item.get("replyDrafts"):
            item["replyDrafts"] = new_item["replyDrafts"]
    item["summary"] = new_item["summary"] or item.get("summary", "")
    if new_item.get("actionItems"):
        item["actionItems"] = new_item["actionItems"]
    if new_item.get("attachments"):
        item["attachments"] = new_item["attachments"]
    if new_item.get("bodyText"):
        item["bodyText"] = new_item["bodyText"]
    if new_item.get("images"):
        item["images"] = new_item["images"]
        item["imageCount"] = len(new_item["images"])


def apply_sent_reply(items: list[dict], sent: dict, analysis: dict, contacts: dict[str, dict]) -> dict | None:
    """보낸편지함 신규 발신 → 같은 제목의 미회신 항목을 replied 로 전환."""
    target = find_thread_target(items, sent.get("subject", ""), sent.get("date") or sent.get("receivedAt") or "", days=21)
    if target is None:
        return None
    target["replyStatus"] = "replied"
    note = f"{sent.get('date', '')} 회신 발송" + (f" — {analysis.get('threadGist')}" if analysis.get("threadGist") else "")
    target["repliedNote"] = note
    target["deadline"] = target.get("deadline") or "회신 완료"
    target.setdefault("thread", []).append({
        "id": f"s-{sent['mf_no']}",
        "date": sent.get("date") or "",
        "from": sender_label(sent.get("from_raw", ""), sent.get("from_email", ""), contacts) or "김성규 과장",
        "gist": "회신 발송" + (f" — {analysis.get('threadGist')}" if analysis.get("threadGist") else ""),
        "link": sent["mailLink"],
    })
    target["thread"].sort(key=lambda m: str(m.get("date") or ""))
    target["lastAt"] = max([str(m.get("date") or "") for m in target["thread"]] + [target.get("receivedAt") or ""])[:16]
    return target


# ────────────────────────────────────────────────────────────── 동기화 본체
def max_mf_no(data: dict) -> int:
    best = 0
    for it in data.get("items") or []:
        m = re.fullmatch(r"[msa]-(\d+)", str(it.get("id") or ""))
        if m:
            best = max(best, int(m.group(1)))
    return best


def kst_now() -> dt.datetime:
    return dt.datetime.now(KST)


def sync(args: argparse.Namespace) -> dict:
    pin = os.environ.get("MAILBOARD_PIN", "").strip()
    if not pin:
        raise SystemExit("MAILBOARD_PIN 환경변수가 필요합니다.")
    seed_days = int(getattr(args, "seed_days", 14) or 14)
    seed_max = int(getattr(args, "seed_max", 30) or 30)
    enc_path = args.data_enc
    data, is_new = load_or_init(enc_path, pin, seed_days=seed_days)
    items = data.setdefault("items", [])
    base = max_mf_no(data)
    now = kst_now()
    report: dict[str, Any] = {"base_mf_no": base, "new_inbox": [], "new_sent": [], "replied": [],
                              "warnings": [], "gemini_calls": 0, "init": is_new,
                              "mode": "gemini" if args.gemini_key else "heuristic"}

    nm = Nmail(args.chunil_id, args.chunil_pw, ip_check=args.ip_check, debug_dir=args.dump_dir)
    nm.login()

    # 사내 주소록(조직도 + 개인주소록) — 비어 있거나 --refresh-contacts 면 새로 받아 둔다.
    #  실패해도 메일 동기화는 그대로 진행한다(발신자 이름·부서 보완만 못 할 뿐).
    contacts_added = 0
    if getattr(args, "refresh_contacts", False) or not data.get("contacts"):
        try:
            fetched = nm.fetch_contacts(report=report)
        except Exception as e:  # noqa: BLE001 - 주소록 실패가 동기화를 막지 않는다
            fetched = []
            report["warnings"].append(f"사내 주소록 수집 실패: {type(e).__name__}: {str(e)[:200]}")
        if fetched:
            data["contacts"] = fetched
            contacts_added = len(fetched)
    contacts = load_contacts(data)

    # 로그인 사용자를 먼저 읽고 분류 규칙을 세운다(이름·메일·부서·VIP).
    owner = detect_owner(nm, args.chunil_id, contacts=data.get("contacts"), data=data)
    configure(owner, load_board_config(getattr(args, "board_config", None)))
    data["owner"] = dict(OWNER)
    data["account"] = OWNER.get("email") or data.get("account") or ""
    report["owner"] = {k: OWNER.get(k) for k in ("name", "title", "dept", "email")}
    report["vips"] = [v[0] + " " + str(v[2]) for v in VIPS]
    # 조직도 직위 칸이 비어 있으면 지난 기록에서 직급을 추론해 채운다(신규 메일이 없어도)
    inferred = ensure_owner_title(data)
    if inferred:
        report["owner"]["title"] = inferred

    if is_new:
        # 첫 실행: 최근 N일 메일로 보드를 새로 채운다(기존 기록을 지울 일이 없다).
        report["mode"] = "init"
        report["seed_days"] = seed_days
        inbox = nm.seed_mailbox("inbox", days=seed_days, limit=seed_max)
        sent = nm.seed_mailbox("sent", days=seed_days, limit=max(5, seed_max // 3))
        data["source"] = f"mail.chunil.co.kr 받은편지함 (최근 {seed_days}일 · 이전 기록은 검색)"
        data["rangeFrom"] = (now - dt.timedelta(days=seed_days)).strftime("%Y-%m-%d")
    else:
        inbox = [m for m in nm.list_mailbox("inbox", pages=args.max_pages, stop_below=base) if m["mf_no"] > base]
        sent = [m for m in nm.list_mailbox("sent", pages=args.max_pages, stop_below=base) if m["mf_no"] > base]
        inbox = inbox[: args.max_new]
        sent = sent[: args.max_new]
    report["new_inbox"] = [{"mf_no": m["mf_no"], "subject": m["subject"], "receivedAt": m["receivedAt"]} for m in inbox]
    report["new_sent"] = [{"mf_no": m["mf_no"], "subject": m["subject"], "receivedAt": m["receivedAt"]} for m in sent]

    if not inbox and not sent and not is_new and not contacts_added and not inferred:
        report["changed"] = False
        return report

    gem = Gemini(args.gemini_key, args.gemini_model) if args.gemini_key else None

    def analyze(mail: dict, kind: str) -> dict:
        if gem is None:
            return heuristic_analysis(mail, _is_to(mail), contacts)
        try:
            report["gemini_calls"] += 1
            return gem.analyze(mail, kind=kind)
        except Exception as e:  # noqa: BLE001 - 한 건 실패가 전체를 멈추지 않게
            # 로그가 공개될 수 있으므로 메일 제목·본문은 오류 문구에 넣지 않는다
            report["warnings"].append(f"Gemini 실패(mf_no={mail.get('mf_no')}): {type(e).__name__}: {str(e)[:200]}")
            return heuristic_analysis(mail, _is_to(mail), contacts)

    added = 0
    title_sent: list[str] = []
    title_inbox: list[str] = []
    for row in inbox:
        detail = nm.read_mail("inbox", row["mf_no"])
        if not detail.get("subject"):
            detail["subject"] = row["subject"]
        title_inbox.append(str(detail.get("subject") or "") + "\n" + str(detail.get("bodyText") or ""))
        if args.images and detail.get("images"):
            detail["imagesData"] = collect_images(nm, detail["images"], report)
        analysis = analyze(detail, "받은편지함")
        new_item = build_item(detail, analysis, contacts, now)
        target = find_thread_target(items, detail["subject"], detail["date"]) if args.thread_merge else None
        if target is not None:
            merge_into_thread(target, new_item)
        else:
            items.append(new_item)
            added += 1
        report["new_inbox"][-1].update({"priority": new_item["priority"], "sender": new_item["sender"],
                                        "summary": new_item["summary"][:120], "merged_into": target.get("id") if target else None})

    for row in sent:
        if not args.track_replies:
            break
        detail = nm.read_mail("sent", row["mf_no"])
        if not detail.get("subject"):
            detail["subject"] = row["subject"]
        title_sent.append(str(detail.get("subject") or "") + "\n" + str(detail.get("bodyText") or ""))
        analysis = analyze(detail, "보낸편지함")
        hit = apply_sent_reply(items, detail, analysis, contacts)
        if hit:
            report["replied"].append({"sent_mf_no": row["mf_no"], "target": hit["id"], "subject": detail["subject"][:60]})

    refreshed = refresh_copied_summaries(items, contacts)   # 예전 '본문 복붙' 요약 → 3인칭 (v1.2.7)
    if refreshed:
        report["summariesRefreshed"] = refreshed
    changed = bool(refreshed or added or report["replied"] or contacts_added or inferred
                   or any(m.get("merged_into") for m in report["new_inbox"]))
    if is_new:
        changed = True      # 첫 실행이면 메일이 0건이어도 보드를 만든다(페이지가 열리도록)
    report["changed"] = changed
    if not changed:
        return report

    # meta
    stamp = now.strftime("%Y-%m-%d %H:%M")
    data["updatedAt"] = f"{stamp} (KST)"
    crit = data.setdefault("criteria", {})
    for older, newer in (("prev4VerifiedAt", "prev3VerifiedAt"), ("prev3VerifiedAt", "prev2VerifiedAt"),
                         ("prev2VerifiedAt", "prevVerifiedAt"), ("prevVerifiedAt", "verifiedAt")):
        if crit.get(newer):
            crit[older] = crit[newer]
    if is_new:
        crit["verifiedAt"] = (
            f"{stamp} (KST) 최초 구축 — 최근 {seed_days}일 받은편지함 {len(inbox)}건을 읽어 {added}건 등록"
            f" · 보낸편지함 {len(sent)}건 대조(회신 반영 {len(report['replied'])}건)"
            f" · Gemini {report['gemini_calls']}회 호출"
            + (f" · 사내 주소록 {contacts_added}명 수집" if contacts_added else "")
        )
    else:
        crit["verifiedAt"] = (
            f"{stamp} (KST) GitHub Actions 자동 갱신 — 받은편지함 신규 {len(inbox)}건 중 {added}건 추가"
            f"{'·스레드 병합 ' + str(len(inbox) - added) + '건' if len(inbox) > added else ''}"
            f" · 보낸편지함 신규 {len(sent)}건(회신 반영 {len(report['replied'])}건) · Gemini {report['gemini_calls']}회 호출"
            + (f" · 사내 주소록 {contacts_added}명 재수집" if contacts_added else "")
        )
    # data.json 은 최신순 정렬(make_data.py 와 동일) — 대시보드가 위에서부터 최신으로 그린다
    items.sort(key=lambda it: (str(it.get("lastAt") or it.get("receivedAt") or ""), str(it.get("id"))),
               reverse=True)
    data["items"] = items
    # 첫 실행이라 위에서 못 채웠으면, 방금 모은 메일로 한 번 더 직급을 추론한다
    infer_after = ensure_owner_title(data, sent_texts=title_sent, inbox_texts=title_inbox)
    if infer_after:
        report["owner"]["title"] = infer_after
        data["owner"] = dict(OWNER)

    if args.dry_run:
        report["written"] = None
        return report

    compress = page_supports_gzip(args.data_enc)
    out = encrypt_payload(data, pin, compress=compress)
    tmp = args.out or (args.data_enc + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    if args.out and args.out != args.data_enc:
        pass
    else:
        os.replace(tmp, args.data_enc)
    report["written"] = tmp if args.out else args.data_enc
    report["size"] = os.path.getsize(report["written"])
    report["plainBytes"] = out["plainBytes"]
    return report


def collect_images(nm: Nmail, imgs: list[dict], report: dict, *, cap_each: int = 300_000,
                   max_n: int = 3, min_bytes: int = IMG_MIN_BYTES) -> list[dict]:
    """본문 인라인 이미지를 data URI 로 (최대 max_n장).

    실패·로고·아이콘은 조용히 건너뛴다.
    · 내려받은 바이트가 min_bytes(기본 12KB) 미만 → 로고·SNS 아이콘·추적 픽셀
    · 실제 픽셀이 소형 아이콘(width<=130·height<=70)이거나 가로형 배너
      (height<=140 이고 width>=2.5*height) → 'Hi CHUNIL, High green' 같은 슬로건 배너
    · 같은 메일에 같은 이미지가 여러 번 붙으면(해시 동일) 한 번만 남긴다
    """
    out: list[dict] = []
    seen: set[str] = set()
    for im in imgs:
        if len(out) >= max_n:
            break
        blob = nm.fetch_bytes(im["src"], cap=cap_each)
        if not blob:
            continue
        if len(blob) < min_bytes:
            continue                # 12KB 미만 — 로고·아이콘·추적 픽셀
        if is_banner_or_icon(image_size(blob)):
            continue                # 슬로건·서명 배너 · 소형 아이콘
        digest = hashlib.sha256(blob).hexdigest()
        if digest in seen:
            continue                # 같은 배너가 서명에 3번 붙어도 사진은 1장
        seen.add(digest)
        ctype = "image/jpeg"
        head = blob[:12]
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            ctype = "image/png"
        elif head[:4] == b"GIF8":
            ctype = "image/gif"
        elif head[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            ctype = "image/webp"
        out.append({"name": im.get("alt") or f"본문 이미지 {len(out) + 1}",
                    "dataUrl": f"data:{ctype};base64,{base64.b64encode(blob).decode()}"})
    return out


def page_supports_gzip(data_enc_path: str) -> bool:
    """같은 저장소의 index.html 이 gzip(v2) 해제를 지원하는지 확인 (build_data.py 와 같은 판단)."""
    for cand in (os.path.join(os.path.dirname(os.path.abspath(data_enc_path)), "index.html"),
                 os.path.join(os.getcwd(), "index.html"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.html")):
        try:
            with open(cand, encoding="utf-8") as f:
                src = f.read()
            return "gunzipBytes" in src and "DecompressionStream" in src
        except OSError:
            continue
    return False


# ────────────────────────────────────────────────────────────── 자체 검증
def selftest_roundtrip(path: str, pin: str) -> int:
    with open(path, encoding="utf-8") as f:
        enc = json.load(f)
    print(f"· 원본 {path}: v={enc.get('v')} comp={enc.get('comp')} iterations={enc.get('iterations')} "
          f"plainBytes={enc.get('plainBytes')} size={os.path.getsize(path):,}")
    payload = decrypt_payload(enc, pin)
    items = payload.get("items", [])
    print(f"· 복호화 OK — 항목 {len(items):,}건, updatedAt={payload.get('updatedAt')}, owner={payload.get('owner', {}).get('name')}")
    print(f"· 최신 mf_no 기준점: {max_mf_no(payload)}")
    again = encrypt_payload(payload, pin, compress=(enc.get("comp") == "gzip"))
    back = decrypt_payload(again, pin)
    same = json.dumps(payload, sort_keys=True, ensure_ascii=False) == json.dumps(back, sort_keys=True, ensure_ascii=False)
    print(f"· 재암호화 라운드트립: {'일치' if same else '불일치'} (aes-gcm {again['cipher']}, comp={again['comp']}, {again['plainBytes']:,} bytes)")
    keys_ok = set(again) == set(enc)
    print(f"· 파일 키 구조: {'원본과 동일' if keys_ok else '다름 ' + str(set(again) ^ set(enc))}")
    # build_data.py 와의 상호 호환 (build 가 만든 암호문을 우리가 읽을 수 있는지)
    import subprocess, tempfile
    here = os.path.dirname(os.path.abspath(path))
    with tempfile.TemporaryDirectory() as td:
        js = os.path.join(td, "data.json")
        with open(js, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        env = dict(os.environ, MAILBOARD_PIN=pin)
        res = subprocess.run([sys.executable, os.path.join(here, "build_data.py"), "--in", js,
                              "--out", os.path.join(td, "d.enc.json")], capture_output=True, text=True, env=env)
        ok_build = False
        if res.returncode == 0:
            with open(os.path.join(td, "d.enc.json"), encoding="utf-8") as f:
                built = json.load(f)
            ok_build = (json.dumps(decrypt_payload(built, pin), sort_keys=True) ==
                        json.dumps(payload, sort_keys=True))
        print(f"· build_data.py 호환: {'OK (암복호 왕복 일치)' if ok_build else '확인 실패 — ' + (res.stderr or res.stdout)[:300]}")
    return 0 if (same and keys_ok) else 1


def fixture_check(list_path: str | None, read_path: str | None) -> int:
    rc = 0
    if list_path:
        with open(list_path, encoding="utf-8") as f:
            rows = parse_list(f.read(), "inbox")
        print(f"· 목록 파서: {len(rows)}행")
        for r in rows[:6]:
            print(f"   - {r['mf_no']} | {r['receivedAt']} | {r['size']} | unread={r['unread']} "
                  f"attach={r['attach']} imp={r['important']} | {r['sender_raw']} | {r['subject'][:44]}")
        if not rows:
            print("   ! 파싱된 행이 없습니다")
            rc = 1
    if read_path:
        with open(read_path, encoding="utf-8") as f:
            d = parse_read(f.read())
        print(f"· 본문 파서: 제목={d['subject'][:50]!r} 날짜={d['date']} 발신={d['from_raw'][:50]!r}")
        print(f"   to={len(d['to'])} cc={len(d['cc'])} 첨부={[a['name'] + ' ' + a['size'] for a in d['attachments']]}")
        print(f"   본문 {len(d['bodyText'])}자 / 인용·서명 제거 후 {len(clean_body(d['bodyText']))}자 / "
              f"지목={mentions_me(d['bodyText'])}")
        if not d["subject"] or not d["date"]:
            print("   ! 제목/날짜 파싱 실패")
            rc = 1
    return rc


def _show_subjects() -> bool:
    """공개 저장소의 Actions 로그에는 메일 제목이 남지 않도록 기본은 숨긴다.
    내 PC에서 직접 돌릴 때만 SYNC_SHOW_SUBJECTS=1 로 켠다."""
    return os.environ.get("SYNC_SHOW_SUBJECTS") == "1" and not os.environ.get("GITHUB_ACTIONS")


def mask_report(report: dict) -> dict:
    """--json 출력용 — 제목을 기본으로 마스킹한다."""
    if _show_subjects():
        return report
    out = json.loads(json.dumps(report, ensure_ascii=False))
    for key in ("new_inbox", "new_sent", "replied"):
        for row in out.get(key) or []:
            if row.get("subject"):
                row["subject"] = f"mf-{row.get('mf_no', '?')}"
    return out


def write_github_output(report: dict) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"changed={'true' if report.get('changed') else 'false'}\n")
        f.write(f"new_count={len(report.get('new_inbox') or []) + len(report.get('new_sent') or [])}\n")
        f.write(f"replied_count={len(report.get('replied') or [])}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Nmail → Gemini → data.enc.json 동기화")
    ap.add_argument("--data-enc", default="data.enc.json", help="복호화·재암호화할 data.enc.json")
    ap.add_argument("--out", default=None, help="출력 경로(기본: --data-enc 를 덮어씀)")
    ap.add_argument("--dry-run", action="store_true", help="수집·분석만 하고 파일은 쓰지 않음")
    ap.add_argument("--max-pages", type=int, default=3, help="편지함당 읽을 목록 페이지 수(100건/페이지)")
    ap.add_argument("--max-new", type=int, default=40, help="한 번에 처리할 신규 메일 상한")
    ap.add_argument("--no-thread-merge", dest="thread_merge", action="store_false", default=True,
                    help="같은 제목 스레드 병합을 끄고 메일마다 항목 생성")
    ap.add_argument("--no-track-replies", dest="track_replies", action="store_false", default=True,
                    help="보낸편지함 대조(회신 완료 자동 전환)를 끔")
    ap.add_argument("--images", dest="images", action="store_true", default=True,
                    help="본문 인라인 이미지를 data URI 로 함께 저장(기본 켜짐)")
    ap.add_argument("--no-images", dest="images", action="store_false")
    ap.add_argument("--dump-dir", default=None, help="받은 HTML 을 저장할 디버그 폴더")
    ap.add_argument("--selftest-roundtrip", action="store_true", help="현재 data.enc.json 복호화·재암호화 검증")
    ap.add_argument("--fixture-list", default=None, help="목록 HTML 파일로 파서만 검증")
    ap.add_argument("--fixture-read", default=None, help="편지읽기 HTML 파일로 파서만 검증")
    ap.add_argument("--gemini-payload-test", action="store_true",
                    help="Gemini 요청 본문(JSON) 구조만 확인 — 실제 호출은 하지 않는다")
    ap.add_argument("--seed-days", type=int, default=14,
                    help="첫 실행(빈 보드)에서 거슬러 올라갈 일수 — 기본 14일")
    ap.add_argument("--seed-max", type=int, default=30,
                    help="첫 실행에서 한 번에 가져올 받은편지함 건수 상한 — 기본 30건")
    ap.add_argument("--board-config", default=None,
                    help="분류 설정 파일 경로(기본: 저장소 루트 board_config.json)")
    ap.add_argument("--owner-probe", action="store_true",
                    help="로그인해 **사용자 인식(이름·메일·부서·VIP)** 결과만 출력하고 끝낸다")
    ap.add_argument("--refresh-contacts", action="store_true",
                    help="사내 주소록(조직도+개인주소록)을 기존 내용과 관계없이 새로 받아 갱신한다")
    ap.add_argument("--contacts-probe", action="store_true",
                    help="로그인해 **사내 주소록 수집**만 실행해 몇 명을 읽었는지 확인하고 끝낸다")
    ap.add_argument("--json", action="store_true", help="결과 요약을 JSON 으로 출력")
    args = ap.parse_args(argv)
    if args.contacts_probe or args.owner_probe:
        args.chunil_id = os.environ.get("CHUNIL_ID", "").strip()
        args.chunil_pw = os.environ.get("CHUNIL_PW", "").strip()
        if not (args.chunil_id and args.chunil_pw):
            print("CHUNIL_ID / CHUNIL_PW 환경변수가 필요합니다.", file=sys.stderr)
            return 2
        nm = Nmail(args.chunil_id, args.chunil_pw,
                   ip_check=os.environ.get("NMAIL_IP_CHECK", "1").strip())
        nm.login()
        book: list[dict] = []
        rep: dict = {"warnings": []}
        try:
            book = nm.fetch_contacts(report=rep)
        except Exception as e:  # noqa: BLE001 - 사용자 인식만 확인할 때도 있으니 계속
            print(f"주소록 수집 실패: {type(e).__name__}: {e}", file=sys.stderr)
        owner = detect_owner(nm, args.chunil_id, contacts=book)
        configure(owner, load_board_config(args.board_config))
        print(json.dumps({"owner": OWNER, "contacts": rep.get("contacts") or {"total": len(book)},
                          "vips": [list(v) for v in VIPS]}, ensure_ascii=False, indent=2))
        for w in rep.get("warnings") or []:
            print("  ! " + w, file=sys.stderr)
        return 0

    pin = os.environ.get("MAILBOARD_PIN", "").strip()
    if args.selftest_roundtrip:
        if not pin:
            print("MAILBOARD_PIN 환경변수가 필요합니다.", file=sys.stderr)
            return 2
        return selftest_roundtrip(args.data_enc, pin)
    if args.fixture_list or args.fixture_read:
        return fixture_check(args.fixture_list, args.fixture_read)
    if args.gemini_payload_test:
        sample = {"date": "2026-11-23 10:05", "from_raw": '"최수철" <csc7797@chunil.co.kr>',
                  "to": ["김성규 <kimpro@chunil.co.kr>"], "cc": [], "subject": "공정이관 검토 요청",
                  "bodyText": "검토 후 의견 부탁드립니다.", "attachments": [{"name": "검토.xlsx", "size": "12.3 KB"}]}
        g = Gemini("DUMMY", os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL)
        captured = {}
        g._request = lambda p: captured.update(payload=p) or {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
        g.analyze(sample)
        print(json.dumps(captured["payload"], ensure_ascii=False, indent=2)[:4000])
        return 0

    args.chunil_id = os.environ.get("CHUNIL_ID", "").strip()
    args.chunil_pw = os.environ.get("CHUNIL_PW", "").strip()
    args.gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    args.gemini_model = (os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL).strip()
    args.ip_check = os.environ.get("NMAIL_IP_CHECK", "1").strip()
    missing = [k for k, v in (("CHUNIL_ID", args.chunil_id), ("CHUNIL_PW", args.chunil_pw),
                              ("MAILBOARD_PIN", pin)) if not v]
    if missing:
        print(f"환경변수 누락: {', '.join(missing)}", file=sys.stderr)
        return 2
    if not args.gemini_key:
        msg = "GEMINI_API_KEY 가 없어 규칙 기반 대체 분석으로 실행합니다(요약 품질 저하)."
        if os.environ.get("GITHUB_ACTIONS"):
            print("오류: GitHub Actions 실행에서는 GEMINI_API_KEY 가 필요합니다. " + msg, file=sys.stderr)
            return 2
        print("경고: " + msg, file=sys.stderr)

    try:
        report = sync(args)
    except NmailError as e:
        print(f"웹메일 오류: {e}", file=sys.stderr)
        return 3
    except (GeminiError, ValueError, OSError) as e:
        print(f"실행 오류: {e}", file=sys.stderr)
        return 4

    write_github_output(report)
    if args.json:
        print(json.dumps(mask_report(report), ensure_ascii=False, indent=2))
    else:
        print(f"[동기화] 기준 mf_no={report['base_mf_no']} · 신규 받은 {len(report['new_inbox'])}건"
              f" · 신규 보낸 {len(report['new_sent'])}건 · 회신 반영 {len(report['replied'])}건"
              f" · 변경={report.get('changed')} · Gemini {report['gemini_calls']}회"
              + (" · 최초 구축" if report.get("init") else ""))
        ow = report.get("owner") or {}
        print(f"  · 로그인 사용자: {ow.get('name') or '?'} {ow.get('title') or ''} "
              f"({ow.get('dept') or '부서 미확인'}) {ow.get('email') or ''}".rstrip())
        if report.get("vips"):
            print("  · VIP 등급: " + ", ".join(report["vips"]))
        ct = report.get("contacts") or {}
        if ct:
            print(f"  · 사내 주소록: {ct.get('total', 0)}명 (조직도 {ct.get('org', 0)} · 개인 {ct.get('personal', 0)})"
                  f" · 그룹 {ct.get('groups', 0)}개")
        show = _show_subjects()
        for row in report["new_inbox"]:
            extra = f" {row.get('sender', '')} — {row['subject'][:60]}" if show else ""
            print(f"  · [{row.get('priority', '?')}] mf_no={row['mf_no']} {row['receivedAt']}{extra}")
        if not show and report["new_inbox"]:
            print("  (제목은 공개 로그 노출 방지를 위해 표시하지 않습니다 · 로컬에서는 SYNC_SHOW_SUBJECTS=1)")
        for w in report["warnings"]:
            print("  ! " + w)
        if report.get("written"):
            print(f"  → {report['written']} ({report.get('size', 0):,} bytes)")
    return 0 if not report["warnings"] else 0


if __name__ == "__main__":
    sys.exit(main())
