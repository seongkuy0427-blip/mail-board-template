#!/usr/bin/env python3
"""천일 웹메일(Nmail) → Gemini API → data.enc.json 동기화 (GitHub Actions 무인 실행용 · 사내 배포판).

GitHub Actions 러너에서 30분마다 돌아,
   1) 기존 data.enc.json 을 MAILBOARD_PIN 으로 복호화해 지금 상태를 읽고,
   2) mail.chunil.co.kr 에 로그인해 **사내 주소록(조직도+개인주소록)** 이 비어 있으면 받아 두고,
   3) **로그인 사용자(이름·메일·부서·직급)** 를 먼저 읽고(주소록의 본인 항목으로 보완),
   4) 받은편지함·보낸편지함의 새 메일만 가져와,
   5) Gemini API 로 요약·할 일·기한·회신 필요 여부·실무형 답장 3안(자료 송부·조치 결과·담당자 전달)을 만들고,
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
import io
import json
import os
import re
import struct
import sys
import time
import unicodedata
from typing import Any

import requests
from bs4 import BeautifulSoup, Comment
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
P1_LABEL, P2_LABEL, P3_LABEL = "🔴 즉시 확인", "🟡 흐름 참고", "⚪ 기록 보관"
P3_TITLE_KEYWORDS: tuple[str, ...] = ("출하리스트", "납입요청", "미상차현황")

CFG: dict = {
    "company": DEFAULT_COMPANY,
    "to_is_urgent": True,      # 내가 To 로 직접 받은 메일도 즉시 확인으로 (새 사용자 기본)
    "p3_title_keywords": list(P3_TITLE_KEYWORDS),
    "vips": [],                # [{"name": "", "email": "", "label": "1-1 최우선", "rank": 1, "toOnly": false}]
}
P3_TITLE_REASON = P3_LABEL + " · 출하/납입 리스트"

# ── 내 분류 규칙(커스텀) — 화면(⚙️ mb_rules_v1 · 포털 /api/state rules)과 같은 저장소 (v1.2.12)
#    포털은 state.json 의 rules 를, GitHub Actions 는 board_config.json 의 rules 를 넘겨준다.
#    적용 순서도 화면과 같다: ① 발신자 규칙 → ② 제목 키워드 규칙 → ③ 기본 분류.
CUSTOM_RULES: dict = {"senders": [], "keywords": []}
_CR_EMAIL: dict = {}
_CR_NAME: dict = {}
_CR_KW: list = []


_RULE_TITLE_RE = re.compile("|".join((
    "과장보", "부사장", "본부장", "상무", "전무", "이사", "부장", "차장", "과장", "대리",
    "사원", "주임", "계장", "팀장", "실장", "소장", "대표", "사장", "매니저")))


def _rule_name(name: str) -> str:
    """`김세유 (납입1팀)` · `최수철 부장` → 규칙 키로 쓰는 이름(괄호·직급 제거, 공백 제거, 소문자)."""
    t = re.sub(r"\s*\([^)]*\)\s*", " ", str(name or "")).strip()
    m = re.match(r"^(.{1,20}?)\s*(?:" + _RULE_TITLE_RE.pattern + r")$", t)
    n = m.group(1).strip() if m else ""
    return re.sub(r"\s+", "", n or t).lower()


def set_custom_rules(rules) -> None:
    """규칙 저장소(dict) → 발신자·키워드 색인. 형식이 어긋난 항목은 조용히 건너뛴다."""
    global CUSTOM_RULES, _CR_EMAIL, _CR_NAME, _CR_KW
    out: dict = {"senders": [], "keywords": []}
    if isinstance(rules, dict):
        for r in rules.get("senders") or []:
            if not isinstance(r, dict):
                continue
            try:
                p = int(r.get("priority"))
            except (TypeError, ValueError):
                continue
            if p not in (1, 2, 3):
                continue
            email = str(r.get("email") or "").strip().lower()
            name = str(r.get("name") or "").strip()
            key = str(r.get("key") or email or name).strip()
            if not key:
                continue
            out["senders"].append({"key": key, "name": name, "email": email,
                                   "priority": p, "toOnly": bool(r.get("toOnly"))})
        for k in rules.get("keywords") or []:
            if not isinstance(k, dict):
                continue
            text = str(k.get("text") or "").strip()
            try:
                p = int(k.get("priority"))
            except (TypeError, ValueError):
                continue
            if text and p in (1, 2, 3):
                out["keywords"].append({"text": text, "priority": p})
    CUSTOM_RULES = out
    _CR_EMAIL, _CR_NAME, _CR_KW = {}, {}, []
    for s in out["senders"]:
        if s["email"]:
            _CR_EMAIL[s["email"]] = s
        if s["name"]:
            _CR_NAME[_rule_name(s["name"])] = s
    for k in out["keywords"]:
        _CR_KW.append((k["text"].lower(), k["priority"], k["text"]))


def custom_rule_for(item: dict, protected: bool = False):
    """이 메일에 걸리는 내 규칙 → (priority, 설명) 또는 None.

    화면(index.html ruleForItem)과 같은 순서: 발신자 규칙을 먼저 보고, 없으면 제목 키워드.
    ⚪ 기록 보관(3) 규칙은 보호 메일(수신 To·이름 지목)에는 걸지 않는다(예외 안전장치).
    """
    email = (item.get("senderEmail") or "").strip().lower()
    sender = item.get("senderName") or item.get("sender") or ""
    rule = _CR_EMAIL.get(email) if email else None
    if rule is None and _CR_NAME:
        rule = _CR_NAME.get(_rule_name(sender))
    if rule is not None:
        if rule.get("toOnly") and item.get("recipientRole") != "To":
            rule = None
        elif not (protected and rule["priority"] == 3):
            who = rule.get("name") or rule.get("email") or rule.get("key")
            return rule["priority"], f"내 규칙(발신자 '{who}')"
    if _CR_KW:
        title = (item.get("title") or "").lower()
        for text, prio, raw in _CR_KW:
            if text not in title:
                continue
            if protected and prio == 3:
                continue
            return prio, f"내 규칙(키워드 '{raw}')"
    return None
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
    set_custom_rules(CFG.get("rules"))       # 내 규칙(발신자·키워드) — 화면과 같은 저장소 (v1.2.12)
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


PARSE_MARK_RE = re.compile(r"\[\s*(?:BEGIN|END)\s*\]\s*", re.I)


def strip_parse_marks(text: str) -> str:
    """웹메일 HTML 주석이 본문으로 새어 들어온 `[Begin]`·`[End]` 파싱 마커를 걷어낸다 (v1.2.18).

    Nmail 편지읽기 화면은 `<!-- [Begin]상단 디자인 -->` 같은 주석을 본문 요소 안에 둔다.
    예전 파서는 주석까지 글자로 읽어 본문·제목·요약 앞에 `[Begin]` 이 그대로 붙었다.
    이미 저장된 본문도 동기화할 때 이 함수로 정리된다.
    """
    s = str(text or "")
    if not s:
        return s
    return PARSE_MARK_RE.sub("", s)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", strip_parse_marks(text).replace("\xa0", " ")).strip()


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

    attachments = _parse_attachments(soup)

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


_ATT_IMG_RE = re.compile(r"\.(jpe?g|png|gif|webp|bmp)$", re.I)


def _att_image_name(name: str) -> bool:
    """첨부 파일명이 사진인지(.jpg .jpeg .png .gif .webp .bmp) — 썸네일 대상 판정."""
    return bool(_ATT_IMG_RE.search((name or "").strip()))


# 웹메일 화면의 사이드바·상단 메뉴 — 첨부파일이 아니다 (v1.2.18 사용자 신고: 첨부 0건인데 26건으로 표시)
WEBMAIL_UI_NAMES = {
    "메일", "일정관리", "주소록", "환경설정", "개인정보", "로그보기", "로그아웃", "편지쓰기", "내게쓰기",
    "안읽음", "중요", "수신확인", "대용량첨부", "전체메일", "관리", "받은편지함", "보낸편지함",
    "임시보관함", "지운편지함", "비움", "스팸편지함", "스팸메일설정", "pop3이용안내",
    "smtp/pop3문제해결", "리턴메일분석", "pop3", "smtp", "리턴메일", "로그인", "사용자정보",
}
# 실제 첨부 내려받기 주소 — 편지읽기·다운로드 링크만 첨부로 본다
ATT_DOWNLOAD_RE = re.compile(r"(mail_attach_down|include/download\.php|file_down|attach_down|mail_down\.php)", re.I)


def is_webmail_ui_name(name: str) -> bool:
    """`안읽음 1`·`받은편지함 4` 처럼 건수만 붙은 웹메일 메뉴 이름인가 (v1.2.18)."""
    raw = _clean(name)
    if not raw:
        return False
    key = re.sub(r"\s*\d+\s*$", "", raw)
    key = re.sub(r"[\s·ㆍ()\[\]]", "", key).lower()
    if not key:
        return False
    if key in WEBMAIL_UI_NAMES:
        return True
    if re.match(r"^(?:pop3|smtp|리턴메일)", key):
        return True
    if re.search(r"(?:이용안내|문제해결)$", key):
        return True
    me = _clean(OWNER.get("name") or "")
    if me and key in {me.lower(), re.sub(r"[\s·ㆍ()\[\]]", "", me).lower()}:
        return True                     # 로그인 사용자 이름만 달랑 있는 메뉴 줄
    return False


def _att_name_ok(name: str, size: str = "", url: str = "") -> bool:
    """첨부로 볼 만한 이름인가 — 메뉴 이름·빈 이름·확장자도 용량도 주소도 없는 줄은 버린다."""
    raw = _clean(name)
    if not raw:
        return False
    if is_webmail_ui_name(raw):
        return False
    if not re.search(r"\.[A-Za-z0-9]{1,6}$", raw) and not _clean(size) and not str(url or "").strip():
        return False
    return True


def clean_attachment_list(atts: list) -> list[dict]:
    """저장된 첨부 목록을 같은 규칙으로 정리한다(웹메일 메뉴·빈 칩·같은 파일명 중복 제거)."""
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for a in atts or []:
        if not isinstance(a, dict):
            continue
        name = _clean(a.get("name") or "")
        size = _clean(a.get("size") or "")
        url = str(a.get("url") or "").strip()
        if not _att_name_ok(name, size, url):
            continue
        key = re.sub(r"\s+", " ", name).strip().lower()
        prev = seen.get(key)
        if prev is not None:
            if not prev.get("size") and size:
                prev["size"] = size
            if not prev.get("url") and url:
                prev["url"] = url
            continue
        rec: dict = {"name": name, "size": size}
        if url:
            rec["url"] = url
        if a.get("dataUrl"):
            rec["dataUrl"] = a["dataUrl"]
        seen[key] = rec
        out.append(rec)
    return out


def clean_attachments(items: list[dict], *, limit: int = 0) -> int:
    """이미 저장된 첨부 목록에서 웹메일 메뉴 이름·빈 칩을 걷어낸다 (v1.2.18).

    네트워크 없이 저장분만 정리하므로, 첨부가 없는데 메뉴 26종이 붙어 있던 메일도
    다음 동기화 한 번에 `첨부파일 · 0건`으로 바로잡힌다.
    """
    fixed = 0
    for it in items or []:
        atts = it.get("attachments")
        if not isinstance(atts, list) or not atts:
            continue
        kept = clean_attachment_list(atts)
        if len(kept) < len(atts):
            fixed += len(atts) - len(kept)
            if kept:
                it["attachments"] = kept
            else:
                it.pop("attachments", None)
        if limit and fixed >= limit:
            break
    return fixed


def _parse_attachments(soup) -> list[dict]:
    """편지읽기 페이지의 첨부 목록 → [{name, size, url?}]

    · 첨부 영역(`.mf_attach_names_wrap`)이 없으면 첨부 0건이다. 예전에는 페이지 전체에서 `li a` 를
      훑어, 첨부가 없는 메일이면 웹메일 사이드바 메뉴(메일·일정관리·주소록 … 26종)를 첨부로 읽었다
      (v1.2.18 사용자 신고 — `첨부파일 · 26건`).
    · 파일명이 빈 <a>(아이콘 전용 다운로드 버튼)·웹메일 메뉴 이름은 첨부가 아니라 버튼·메뉴라 버린다.
    · 같은 파일명이 두 번 나오면 한 번만 남기고, 비어 있던 크기·주소는 채워 넣는다.
    · url 은 첨부 내려받기 주소(mail.chunil.co.kr) — 포털이 사진 썸네일을 중계할 때 쓴다.
    """
    out: list[dict] = []
    seen: dict[str, dict] = {}
    wrap = soup.select_one(".mf_attach_names_wrap")
    if wrap is None:
        return out
    for a in wrap.select("a"):
        size_el = a.select_one("span.mf_attach_name_size")
        size = _clean(size_el.get_text(" ", strip=True)) if size_el else ""
        if size_el:
            size_el.extract()
        name = _clean(a.get_text(" ", strip=True))
        if not name:
            # 글자가 없는 <a> — title 에 파일명이 들어 있는 경우만 살린다
            title = _clean(a.get("title") or "")
            title = re.sub(r"^(다운로드|첨부\s*다운로드|내려받기|미리보기)\s*", "", title).strip()
            name = title if title and not re.search(r"(다운로드|내려받기|미리보기)$", title) else ""
        href = (a.get("href") or "").strip()
        if href.startswith("/"):
            href = BASE + href
        if not href.startswith("http") or "javascript:" in href:
            href = ""
        if not _att_name_ok(name, size, href):
            continue
        key = re.sub(r"\s+", " ", name).strip().lower()
        prev = seen.get(key)
        if prev is not None:
            if not prev.get("size") and size:
                prev["size"] = _pretty_size(size)
            if not prev.get("url") and href:
                prev["url"] = href
            continue
        rec = {"name": name, "size": _pretty_size(size)}
        if href:
            rec["url"] = href
        seen[key] = rec
        out.append(rec)
    return out


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
        if isinstance(node, Comment):
            return                                        # <!-- [Begin] … --> 주석은 본문 글자가 아니다 (v1.2.18)
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
    return strip_parse_marks(join_body_text(raw)).strip()


def clean_body(text: str) -> str:
    """분석·저장에 쓰는 본문 — 인용 헤더와 슬로건·면책 문구만 걷어낸다(직원 서명은 남긴다)."""
    return strip_signature(strip_quoted(text or ""))


# ══════════════════════════════════════════════════════════════ 메일 분석 입력 · 3단 분석 (v1.2.11)
#  GM 처럼 여러 부서를 한꺼번에 수신/참조에 넣는 메일에서 "실제로 요청받은 주담당 부서"와
#  "우리 부서(팀) 및 내가 할 일"을 가려내려면 입력이 달라져야 한다.
#   ① 인용 구분선(-----Original Message-----·보낸사람: 등) 아래의 이전 회신을 자르지 않는다.
#   ② 회사 슬로건·기밀 면책·주소/TEL/Fax 같은 서명 잡음만 걷어 내고 **부서·직급 각인 1줄은 남긴다**.
#   ③ 사내 조직(우리 부서 인원 + To/CC 인원의 부서)을 프롬프트 컨텍스트로 함께 준다.
#   ④ 같은 스레드가 이어지면 전체를 다시 보내지 않고 '누적 타임라인 + 새 메일 1통'만 보낸다(증분).
#   ⑤ 🔴 즉시 확인·🟡 흐름 참고는 처음부터 3단 정밀 분석한다(v1.2.21 — 경량 요약 분기 제거).
#      ⚪ 기록 보관만 AI 를 부르지 않고, 필요할 때 화면의 [✨ AI 요약 실행] 으로 정밀 분석한다(포털).
OUR_DIVISION = ""                   # (개인판은 '납입관리부') CFG["our_dept"] 가 있으면 그것이 우선
ANALYSIS_BODY_CHARS = 10000         # AI 에 보내는 본문 상한(전체 스레드 포함) — v1.2.22 배경·인용 스레드 여유
THREAD_COMPRESS_AT = 10             # 이 통수 이상이면 중간 회신을 1줄로 압축
THREAD_KEEP_ORIGIN = 2              # 압축할 때 원문을 그대로 두는 최초 메일 수
THREAD_KEEP_RECENT = 4              # 압축할 때 원문을 그대로 두는 최근 메일 수
THREAD_LINE_CHARS = 90              # 압축된 중간 메일 1줄 길이
CACHE_TIMELINE_MAX = 16             # 스레드 캐시에 남기는 타임라인 줄 수
THREAD_CACHE_MAX = 200              # 스레드 캐시 키 수 상한(data 파일 크기 보호)
SKIP_SIMPLE_SUMMARY = "기록 보관 메일로 분류되어 AI 분석을 생략했습니다."
LIGHT_TAG = "[타 부서 주담당 · 단순 참고]"

THREAD_START_RE = re.compile(
    r"^\s*(?:-{2,}\s*(?:original message|원본\s*메일|메일\s*원본|forwarded message|전달된 메일)\s*-{2,}.*"
    r"|(?:보낸\s*사람|보낸사람|from)\s*[:：].*"
    r"|on\s+.{0,80}wrote\s*:?\s*)$", re.I)
#  1줄 압축할 때 '내용' 줄을 고르기 위해 건너뛰는 머리표 줄
HEADER_ONLY_RE = re.compile(
    r"^\s*(?:보낸\s*사람|보낸사람|보낸\s*날짜|받는\s*사람|참조|제목|from|sent|to|cc|bcc|subject|date)\s*[:：]",
    re.I)

#  서명 블록의 잡음 줄 — 줄 머리에서만 인정한다(본문 중간의 'Tel.' 오인 방지).
SIG_NOISE_PATTERNS = (
    r"^주소\s*[:：]", r"^우편번호", r"^(?:tel|t\.)\s*[:：]?", r"^전화\s*[:：]", r"^대표전화",
    r"^fax\s*[:：]?", r"^팩스\s*[:：]?", r"^mobile\s*[:：]?", r"^hp\s*[:：]", r"^휴대폰\s*[:：]?",
    r"^e-?mail\s*[:：]", r"^이메일\s*[:：]", r"^(?:https?://|www\.)",
    r"^(?:copyright|ⓒ|©)", r"^무단\s*(?:전재|배포)", r"^본\s*메일", r"^이\s*메일은",
    r"^(?:gm\s*)?confidential", r"^the\s*next\s*100", r"^hi\s*,?\s*chunil", r"^high\s*green",
    r"^70\s*년의\s*경험",
)
DEPT_SIG_RE = re.compile(r"[가-힣A-Za-z0-9]{2,14}(?:사업본부|사업부|본부|관리부|물류부|영업부|팀|실|부)")
RANK_SIG_RE = re.compile(r"(?:상무|전무|이사|부장|차장|과장|대리|사원|주임|계장|팀장|실장|소장|매니저)")


def _clip(text: str, n: int) -> str:
    s = _clean(text or "")
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def internal_domain() -> str:
    """사내 메일 도메인 — 설정이 있으면 그것을, 없으면 chunil.co.kr."""
    return str(globals().get("DOMAIN") or "chunil.co.kr").lstrip("@")


def is_sig_noise_line(line: str) -> bool:
    low = re.sub(r"\s+", " ", str(line or "")).strip().lower()
    if not low:
        return False
    return any(re.match(p, low) for p in SIG_NOISE_PATTERNS)


def is_dept_signature(line: str) -> bool:
    """`자동차물류사업부 납입1팀 과장 김성규` 처럼 부서·직급이 함께 있는 각인 줄.

    주담당 부서 판별의 근거이므로 서명 블록을 걷어 낼 때도 이 한 줄은 남긴다.
    """
    s = str(line or "").strip()
    if not (6 <= len(s) <= 90):
        return False
    if not DEPT_SIG_RE.search(s):
        return False
    return bool(RANK_SIG_RE.search(s)) or bool(re.search(r"(?:팀|부|실)\s*$", s))


def strip_slogan_lines(text: str) -> str:
    """회사 슬로건·기밀 면책 줄만 걷어 낸다(직원 서명·이전 회신은 그대로)."""
    return "\n".join(ln for ln in str(text or "").splitlines() if not is_sign_text(ln))


SEP_ONLY_RE = re.compile(r"^[\s\-—–_=·•]+$")
THREAD_SEP_ONLY_RE = re.compile(r"^\s*-{2,}\s*(?:original message|원본\s*메일|메일\s*원본|forwarded message|전달된 메일)\s*-{2,}\s*$", re.I)


def squeeze_signatures(text: str) -> str:
    """반복되는 하단 서명 블록(주소·TEL/Fax/Mobile·면책)을 걷어 내고 부서·직급 각인 1줄은 남긴다."""
    out: list[str] = []
    seen_sig: set[str] = set()
    sig_tails: list[str] = []
    for line in str(text or "").splitlines():
        s = line.strip()
        if not s:
            out.append("")
            continue
        if SEP_ONLY_RE.match(s) or THREAD_SEP_ONLY_RE.match(s):
            continue                                     # 서명 울타리(—, ---) · 인용 구분선만 있는 줄
        if is_sig_noise_line(s):
            continue
        if is_dept_signature(s):
            key = re.sub(r"\s+", " ", s)
            tail = key[-20:]                              # 회사명이 앞에 붙어도 같은 사람으로 본다
            if key in seen_sig or any(t and t in key for t in sig_tails):
                continue                                  # 같은 서명이 회신마다 반복되면 한 번만
            seen_sig.add(key)
            sig_tails.append(tail)
            out.append("· " + key)                        # 부서 판별용 각인 표시
            continue
        out.append(line)
    txt = "\n".join(out)
    txt = re.sub(r"[ \t]{2,}", " ", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def split_thread(text: str) -> list[str]:
    """본문을 인용 헤더 기준으로 '메일 단위'로 나눈다(최신 메일이 앞)."""
    text = str(text or "")
    parts: list[str] = []
    cur: list[str] = []
    for line in text.splitlines():
        if THREAD_START_RE.match(line) and sum(len(x) for x in cur) >= 20:
            parts.append("\n".join(cur))
            cur = [line]
            continue
        cur.append(line)
    if cur:
        parts.append("\n".join(cur))
    merged: list[str] = []
    for p in parts:                      # 헤더만 남은 조각은 다음 조각에 붙인다
        if merged and len(re.sub(r"\s", "", p)) < 40:
            merged[-1] = merged[-1] + "\n" + p
        else:
            merged.append(p)
    return [p for p in merged if p.strip()]


def compress_long_thread(text: str) -> str:
    """왕복이 길면(기본 10통↑) 최초 2통·최근 4통만 원문, 중간은 1줄씩 남긴다(토큰 절감)."""
    parts = [p for p in split_thread(text) if p.strip()]
    if len(parts) < THREAD_COMPRESS_AT:
        return "\n\n".join(reversed(parts)) if len(parts) > 1 else str(text or "").strip()
    parts = list(reversed(parts))                     # 최초 → 최신
    n = len(parts)
    parts = [p.strip() for p in parts]
    origin = parts[:THREAD_KEEP_ORIGIN]
    recent = parts[n - THREAD_KEEP_RECENT:] if THREAD_KEEP_RECENT else []
    middle = parts[THREAD_KEEP_ORIGIN: n - len(recent)]
    lines = []
    for p in middle:
        head = next((ln for ln in p.splitlines()
                     if ln.strip() and not THREAD_START_RE.match(ln)
                     and not HEADER_ONLY_RE.match(ln)), "")
        lines.append("… " + _clip(head, THREAD_LINE_CHARS))
    block = "\n".join([f"[중간 {len(middle)}건 — 1줄 요약]"] + lines)
    return "\n\n".join(origin + [block] + recent)


def analysis_body(mail: dict) -> str:
    """AI 분석 입력용 본문 — 이전 회신·전달 스레드를 자르지 않고 잡음만 걷어 낸다."""
    raw = str(mail.get("bodyText") or "")
    if not raw.strip():
        return ""
    text = squeeze_signatures(strip_slogan_lines(raw))
    text = compress_long_thread(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text[:ANALYSIS_BODY_CHARS]


# ── 사내 조직 컨텍스트 ────────────────────────────────────────────
def our_dept_label() -> str:
    cfg = globals().get("CFG") or {}
    label = str(cfg.get("our_dept") or "").strip()
    if label:
        return label
    if OUR_DIVISION:
        return OUR_DIVISION
    return (OWNER.get("dept") or "").strip() or "우리 부서"


def _dept_key(dept: str) -> str:
    return re.sub(r"[\s\d]+", "", str(dept or ""))


def is_our_team(dept: str) -> bool:
    """`납입1팀`·`납입2팀` 처럼 숫자만 다른 같은 팀 계열인가."""
    key = _dept_key(dept)
    mine = _dept_key(OWNER.get("dept") or "")
    return bool(key) and bool(mine) and key == mine


def our_dept_tokens(contacts: dict | None = None) -> list[str]:
    out: list[str] = []
    for tok in (our_dept_label(), OWNER.get("dept") or ""):
        tok = str(tok or "").strip()
        if tok and tok not in out:
            out.append(tok)
    for c in (contacts or {}).values():
        d = str(c.get("dept") or "").strip()
        if d and is_our_team(d) and d not in out:
            out.append(d)
    return out


def our_members(contacts: dict | None = None) -> list[dict]:
    me = (OWNER.get("email") or ME or "").lower()
    out: list[dict] = []
    for email, c in (contacts or {}).items():
        if not is_our_team(c.get("dept")):
            continue
        if str(email).lower() == me:
            continue
        out.append({"email": str(email), "name": (c.get("name") or "").strip(),
                    "title": (c.get("title") or "").strip(), "dept": (c.get("dept") or "").strip()})
    out.sort(key=lambda c: (c["dept"], c["name"]))
    return out


def person_label(raw: str, contacts: dict | None = None) -> str:
    """주소 → `부서 이름 직급 (외부처)` 한 줄(부서 판별 근거)."""
    email = _addr_email(raw)
    info = (contacts or {}).get(email) or {}
    name = (info.get("name") or "").strip() or _addr_name(raw) or email or str(raw)
    title = (info.get("title") or "").strip()
    dept = (info.get("dept") or "").strip()
    if email.endswith("@gm.com"):
        return f"{name} {title} (GM)".replace("  ", " ").strip()
    if email.endswith(internal_domain()):
        return (f"{dept} {name} {title}" if dept else f"{name} {title}").strip() or str(raw)
    domain = email.split("@")[-1] if "@" in email else "외부"
    return f"{name} {title} ({domain})".replace("  ", " ").strip()


def org_context(mail: dict, contacts: dict | None = None) -> str:
    """AI 프롬프트에 넣을 사내 조직 컨텍스트 — 우리 부서 인원과 이 메일의 To/CC 인원."""
    contacts = contacts or {}
    label = our_dept_label()
    team = (OWNER.get("dept") or "").strip()
    me = f"{OWNER.get('name') or ''} {OWNER.get('title') or ''}".strip() or "나"
    lines = ["[사내 조직 컨텍스트]",
             f"- 나: {me} · {label}" + (f" ({OWNER.get('email')})" if OWNER.get("email") else "")]
    members = our_members(contacts)
    if members:
        shown = members[:14]
        body = ", ".join(f"{m['name']} {m['title']}".strip() +
                         (f"({m['dept']})" if m["dept"] and m["dept"] != team else "")
                         for m in shown)
        lines.append(f"- 우리 부서({label}) 인원 {len(members)}명: {body}" + (" 외" if len(members) > len(shown) else ""))
    else:
        lines.append(f"- 우리 부서({label}) 인원: 주소록 매핑 없음")
    rows: list[str] = []
    for kind, addrs in (("수신(To)", mail.get("to") or []), ("참조(CC)", mail.get("cc") or [])):
        for raw in addrs:
            email = _addr_email(raw)
            if email and email == (OWNER.get("email") or ME or "").lower():
                rows.append(f"  · {kind} 나({me}) — 우리 부서")
                continue
            rows.append(f"  · {kind} {person_label(raw, contacts)}")
    lines.append("- 이 메일의 수신·참조 인원(주담당 부서 판별 근거):")
    lines += rows[:18] or ["  · 없음"]
    return "\n".join(lines)


# ── 스레드 판별·캐시 ──────────────────────────────────────────────
def short_when(when: str) -> str:
    """`2026-11-24 07:00` → `11/24 07:00`"""
    s = str(when or "")
    m = re.search(r"(\d{2,4})-(\d{2})-(\d{2})[ T]?(\d{2}):(\d{2})", s)
    if m:
        return f"{int(m.group(2))}/{int(m.group(3))} {m.group(4)}:{m.group(5)}"
    return s[:16]


def timeline_line(mail: dict, contacts: dict | None, gist: str) -> str:
    """`[11/24 07:00] GM 염주희 차장 → 납입1팀 : …` 한 줄."""
    when = short_when(mail.get("date") or mail.get("receivedAt") or "")
    who = _sender_phrase(mail, contacts) or (mail.get("from_raw") or "발신자")
    rcpt = _recipient_phrase(mail, contacts, _is_to(mail))
    rcpt = re.sub(r"(?:에게|께|한테)$", "", rcpt or "").strip()      # "[날짜] A → B : …" 형태로 읽히게
    return f"[{when}] {who} → {rcpt or '-'} : {_clip(gist, 80)}"


def guess_category(title: str, body: str) -> str:
    blob = f"{title or ''} {body or ''}".lower()
    for key, cat in (("출하리스트", "출하/보급"), ("납입요청", "납입요청"), ("재고", "재고/실사"),
                     ("ptr", "PTR"), ("용기", "승인/검토"), ("승인", "승인/검토"), ("결품", "결품/보고")):
        if key in blob:
            return cat
    return "기타"


def prefilter(mail: dict, contacts: dict | None = None) -> dict:
    """AI 를 부르기 전에 classify() 규칙으로 등급을 미리 본다(본문·제목·수신 위치만 사용)."""
    item = {
        "title": mail.get("subject") or mail.get("title") or "",
        "bodyText": mail.get("bodyText") or "",
        "sender": sender_label(mail.get("from_raw") or mail.get("sender") or "",
                               mail.get("from_email") or mail.get("senderEmail") or "", contacts or {}),
        "senderEmail": mail.get("from_email") or mail.get("senderEmail") or "",
        "recipientRole": "To" if _is_to(mail) else "CC",
        "category": guess_category(mail.get("subject") or mail.get("title"),
                                   clean_body(mail.get("bodyText") or "")),
    }
    classify(item)
    return item


def text_mentions_us(mail: dict) -> bool:
    """제목·본문(스레드 전체)에 우리 부서/내 이름이 직접 나오는가."""
    text = f"{mail.get('subject') or mail.get('title') or ''}\n{mail.get('bodyText') or ''}"
    if MENTION_RE.search(text):
        return True
    return any(tok and tok in text for tok in our_dept_tokens())


def to_includes_us(mail: dict, contacts: dict | None = None) -> bool:
    me = (OWNER.get("email") or ME or "").lower()
    for raw in mail.get("to") or []:
        email = _addr_email(raw)
        if email and email == me:
            return True
        if is_our_team(((contacts or {}).get(email) or {}).get("dept")):
            return True
    return False


# ── 결론 문구 · 주담당 교차 판별 (v1.2.13) ────────────────────────────
#  사용자 확정: "본문 확인 후 회신" 같은 상투어 금지 · 일이 없다는 식의 짧은 마무리도 금지.
#  결론은 [구체적 행위 + 대상/품번·기준 + 회신 대상자·형태] 로 쓴다.
ACTION_BANNED = (
    "본문 요청 내용 확인 후 회신", "본문 확인 후 회신", "본문 확인 후 조치",
    "본문 요청 확인", "내용 확인 후 회신",
)
#  "할 일이 없다"는 식의 짧은 마무리도 금지 — 이유와 다음 행동을 적는다.
ACTION_BANNED_RE = re.compile(r"할\s*일\s*없|볼\s*일\s*없")
#  v1.2.22 — AI 가 이 스레드의 실제 관건을 써 냈는데 고정 상투어로 덮어쓰면 맥락이 사라진다.
#  '상투어(무엇을 볼지 없는 문구)' 와 '이 메일에만 해당하는 구체 문장' 을 갈라, 후자는 살린다.
GENERIC_ACTION_RE = re.compile(
    r"직접\s*조치\s*없음[^\n]*(?:참조\s*흐름\s*파악\s*필요|담당)"
    r"|타\s*부서\s*주담당\s*건\s*·\s*참조\s*모니터링"
    r"|^\s*\[?\s*(?:확인\s*필요|검토\s*필요)\s*\]?\s*$")
DIRECT_TAG_RE = re.compile(r"^\[?\s*직접\s*(?:회신|조치)\s*필요\s*\]?\s*")
ACTION_MARK_RE = re.compile(r"관건|모니터링|영향|GM|업체|협력사|현장|라인|품번|재고|납입|출하|"
                            r"\d{1,2}\s*[/월]\s*\d{1,2}|\d{6,}", re.I)


def _generic_action(text: str) -> bool:
    """무엇을 볼지 없는 상투어·금지어인가."""
    s = str(text or "").strip()
    if not s or any(b in s for b in ACTION_BANNED) or ACTION_BANNED_RE.search(s):
        return True
    return bool(GENERIC_ACTION_RE.search(s))


def _concrete_action(text: str) -> bool:
    """이 메일에만 해당하는 관건·품번·기한·주체가 들어간 문장인가 (v1.2.22)."""
    s = str(text or "").strip()
    if _generic_action(s) or len(re.sub(r"\s", "", s)) < 12:
        return False
    return bool(ACTION_MARK_RE.search(s))


#  v1.2.22 — AI 는 주담당을 `GM 염주희 차장(용기 사양 승인) · 우리 부서(납입1팀)는 참조` 처럼 쓰라고 지시받는다.
#  그 꼬리(`· 우리 부서(...)는 참조`)에 우리 팀 이름이 있다고 주담당을 우리 팀으로 되돌리면 안 된다.
MAIN_TAIL_RE = re.compile(r"·\s*우리\s*부서|\(\s*주담당\s*\)|주담당\s*[:：]")


def main_is_ours(main: str, mine: list[str]) -> bool:
    """mainDept 의 '머리(실제 주체)' 가 우리 부서인가 — 뒤에 붙은 참조 표기는 보지 않는다."""
    head = MAIN_TAIL_RE.split(str(main or "").strip())[0]
    return any(tok and tok in head for tok in mine)


def _as_reference(text: str) -> str:
    """타 주체 주담당 건에서 '[직접 회신 필요] …' 를 '[참조 모니터링] …' 로 바꾼다."""
    s = str(text or "").strip()
    m = DIRECT_TAG_RE.match(s)
    if not m:
        return s
    rest = s[m.end():].strip()
    return ("[참조 모니터링] " + rest) if rest else ""

# 타 부서 업무 신호 — 본문이 이쪽 중심이면 주담당을 우리 팀으로 올리지 않는다
OTHER_DEPT_RULES: tuple[tuple[str, str], ...] = (
    ("해외물류부", r"해외물류|선적|부킹|booking|B/L|\bBL\b|통관|포워더|선복|항공|해상|컨테이너"),
    ("조달물류팀", r"조달물류|도입자재|도입\s*요청|발주|구매"),
    ("영업부", r"영업|수주|판매|딜러"),
    ("제조부", r"제조|생산라인|라인\s*가동|공정\s*이관"),
    ("안전보건팀", r"안전보건|안전점검|위험성|산업안전|규정\s*위반"),
    ("경영지원팀", r"경영지원|인사|총무|회계|급여|근태|특근"),
    ("품질팀", r"품질|불량|클레임|재작업"),
)
OUR_WORK_RE = re.compile(r"납입|출하|재고|실사|보급|서열|PTR|EOP|용기|결품|미상차|입고", re.I)


def _mail_text(mail: dict) -> str:
    return f"{mail.get('subject') or mail.get('title') or ''}\n{mail.get('bodyText') or ''}"


def _other_dept_hit(mail: dict, contacts: dict | None = None) -> tuple[str, int]:
    """본문·제목에서 가장 강하게 드러나는 타 부서와 그 신호 수."""
    text = _mail_text(mail)
    addrs = [*(mail.get("to") or []), *(mail.get("cc") or [])]
    depts = [str(((contacts or {}).get(_addr_email(a)) or {}).get("dept") or "") for a in addrs]
    best, best_score = "", 0
    for label, pat in OTHER_DEPT_RULES:
        hits = re.findall(pat, text, re.I)
        if not hits:
            continue
        score = len(hits)
        stem = re.sub(r"(부|팀|실|본부)$", "", label)
        if stem and any(stem in d for d in depts):
            score += 2
        if score > best_score:
            best, best_score = label, score
    return best, best_score


REQUEST_VERB_RE = re.compile(r"(요청|회신|송부|확인|검토|정리|조치|제출|보고|부탁|전달|작성|반영|처리|진행)")


def _fresh_body(mail: dict) -> str:
    """발신자가 이번에 새로 쓴 본문 — 인용 머리말(──── Original Message ────)과 서명 뒤는 뺀다.

    `strip_quoted` 는 앞 60자 안의 인용 머리말을 건너뛰므로(본문 첫 줄의 'From:' 오인 방지),
    짧은 전달 메일은 첫 줄부터 인용이 시작되는 경우가 많다. 여기서는 인용 머리말이 나오면
    그 앞까지만 새 본문으로 본다 (v1.2.18).
    """
    text = str(mail.get("bodyText") or "")
    low = text.lower()
    cut = len(text)
    for marker in QUOTE_MARKERS:
        idx = low.find(marker)
        if idx > 0:
            cut = min(cut, idx)
    return strip_signature(text[:cut].strip())


def _directed_at_us(text: str, contacts: dict | None = None) -> bool:
    """새 본문에서 우리 부서·팀원을 '요청' 문맥으로 직접 불렀는가 (v1.2.18).

    전달·참조 메일의 인용문이나 서명에 우리 팀 이름·팀원 이름이 스쳐 나오는 것과 구분하려고,
    이름 앞뒤 45자 안에 요청 동사(요청·회신·확인·부탁 …)가 함께 있어야 인정한다.
    """
    body = str(text or "")
    if not body:
        return False
    needles: set[str] = set()
    for tok in our_dept_tokens(contacts or {}):
        if tok and len(tok) >= 2:
            needles.add(tok)
    label = our_dept_label()
    if label:
        needles.add(label)
    my_name = str(OWNER.get("name") or "").strip()
    if len(my_name) >= 2:
        needles.add(my_name)
    for m in our_members(contacts or {}):
        nm = str(m.get("name") or "").strip()
        if len(nm) >= 2:
            needles.add(nm)
    for needle in needles:
        for hit in re.finditer(re.escape(needle), body):
            window = body[max(0, hit.start() - 45): hit.end() + 45]
            if REQUEST_VERB_RE.search(window):
                return True
    return False


def _first_to_label(mail: dict, contacts: dict | None = None) -> str:
    """받는사람(To) 첫 사람 — `해외물류부 류광종 과장 (chunil.co.kr)` 처럼 부서까지 붙인 한 줄."""
    me = (OWNER.get("email") or ME or "").lower()
    for raw in mail.get("to") or []:
        email = _addr_email(raw)
        if email and email == me:
            continue
        label = person_label(raw, contacts or {})
        if label:
            return label
    return ""


# ── 발신자가 우리 부서(팀원) 또는 나인가 (v1.2.21) ───────────────────────────────
#  납입1팀 강한 사원 → 경영지원팀 정하건 처럼 **우리 팀원이 보낸 메일**을 "강한에게 결과 회신" 으로
#  거꾸로 결론짓던 버그. 발신자가 우리 쪽이면 그 메일은 이미 우리가 회신·송부한 것이므로
#  다시 회신하라는 결론을 내지 않는다.
REPLY_SUBJECT_RE = re.compile(r"^\s*(?:re|fw|fwd|답장|전달)\s*[:：]", re.I)
REPLY_DONE_RE = re.compile(
    r"(?:해당자\s*(?:가|는|도)?\s*없|해당\s*없|송부\s*(?:드립|드렸|완료|하였|했)|회신\s*(?:드립|드렸|완료|하였|했)|"
    r"전달\s*(?:드립|드렸|완료|하였|했)|확인\s*완료|처리\s*(?:완료|하였|했)|조치\s*(?:완료|하였|했)|"
    r"보고\s*(?:드립|드렸|완료|하였|했)|제출\s*(?:하였|했|완료)|발송\s*(?:하였|했|완료)|"
    r"자료\s*(?:송부|전달|첨부)|첨부\s*(?:드립|하였|했))"
)


def sender_is_ours(mail: dict, contacts: dict | None = None) -> bool:
    """발신자가 나 또는 우리 부서(납입1팀) 팀원인가 — 이미 우리가 보낸 메일인지 가르는 기준."""
    contacts = contacts or {}
    email = (mail.get("from_email") or _addr_email(mail.get("from_raw") or "") or "").lower()
    me = (OWNER.get("email") or ME or "").lower()
    if email and me and email == me:
        return True
    info = contacts.get(email) or {}
    if info and is_our_team(str(info.get("dept") or "")):
        return True
    raw = mail.get("from_raw") or ""
    name = re.sub(r"\s+", "", _clean(_sender_parts(raw, email, contacts)[0] or ""))
    if len(name) >= 2:
        for member in our_members(contacts):
            if re.sub(r"\s+", "", member.get("name") or "") == name:
                return True
    return False


def _ours_reply_conclusion(mail: dict, contacts: dict | None, done: bool) -> tuple[str, str]:
    """우리 부서가 보낸 메일의 결론 — (우리 부서 할 일, 내 할 일)."""
    to_label = _first_to_label(mail, contacts) or "수신자"
    if done:
        return (f"[회신·송부 완료 · 우리 부서(발신자) → {to_label}에게 처리 완료]",
                "[추가 회신 없음 · 처리 이력 확인용]")
    return (f"[우리 부서 발신 · {to_label}의 회신 대기 (내가 회신할 건 아님)]",
            "[추가 회신 없음 · 상대 회신만 지켜보면 됩니다]")


def dept_ruling(mail: dict, contacts: dict | None = None) -> dict:
    """수신(To)/참조(CC) 위치 · 본문 지목 · 회신 요청 대상 · 업무 키워드를 교차해 주담당을 정한다.

    v1.2.18 — 발신자가 To 를 다른 담당자·부서로 보내고 우리 팀은 참조(CC)에만 있는 전달·공유 건은,
    인용문·서명에 우리 팀 이름이 스쳐 나온다는 이유만으로 `주담당: 납입1팀`·`[직접 회신 필요]` 로
    결론내지 않는다. 새로 쓴 본문에서 우리 팀·팀원을 직접 요청한 경우에만 우리 건으로 본다.
    """
    contacts = contacts or {}
    text = _mail_text(mail)
    fresh = _fresh_body(mail)
    to_me = _is_to(mail)
    in_to = to_includes_us(mail, contacts)
    mentioned = _directed_at_us(fresh, contacts)
    if not mentioned:                       # 본문이 우리 팀원을 직접 부른 경우도 우리 건이다
        for m in our_members(contacts):
            nm = str(m.get("name") or "").strip()
            if len(nm) >= 2 and nm in fresh and REQUEST_VERB_RE.search(fresh):
                mentioned = True
                break
    quoted_mention = text_mentions_us(mail) or mentioned
    our_hits = len(OUR_WORK_RE.findall(text))
    other, other_score = _other_dept_hit(mail, contacts)
    label = our_dept_label()
    # 본문이 타 부서 업무 중심이고 우리 팀 지목도, 우리 업무 신호도 없으면 주담당을 넘긴다
    other_main = bool(other) and other_score >= 2 and not mentioned and our_hits == 0
    sender_ours = sender_is_ours(mail, contacts)      # v1.2.21 — 발신자가 우리 부서(팀)인가
    ours = (to_me or in_to or mentioned) and not other_main and not sender_ours
    to_label = "" if ours else _first_to_label(mail, contacts)
    if ours:
        main = label
    elif other:
        main = f"{other}(주담당) · 우리 부서({label})는 참조"
    elif to_label:
        main = f"{to_label}(주담당) · 우리 부서({label})는 참조"
    else:
        main = "확인 필요(주담당 부서 미상)"
    if ours and mentioned:
        basis = "본문에서 우리 부서/이름이 직접 요청 대상으로 지목됨"
    elif ours and to_me:
        basis = "우리 팀이 수신(To) · 본문 요청 대상도 우리 업무 범위"
    elif ours:
        basis = "우리 팀이 수신(To)에 포함 · 본문 요청도 우리 업무 범위"
    elif other:
        where = "수신(To)에 우리 팀 없이 참조(CC)만" if not in_to else "수신(To)에 있으나"
        basis = f"{where} · 본문 요청·업무 키워드가 {other} 중심"
    elif to_label:
        basis = ("발신자가 수신(To)을 다른 담당자에게 보내고 우리 팀은 참조(CC)만"
                 + (" · 인용·서명에 우리 팀 이름이 보이지만 새 본문에 직접 요청은 없음" if quoted_mention else ""))
    else:
        basis = "참조(CC)만 있고 본문 지목이 없어 주담당 판별 어려움 — 원문 확인 권장"
    reply_done = False
    if sender_ours:
        # v1.2.21 — 우리가 이미 보낸 메일(RE: 답장·회신/송부 완료 표현)은 발신자에게 다시 회신하지 않는다
        reply_done = bool(REPLY_SUBJECT_RE.match(str(mail.get("subject") or ""))) \
            or bool(REPLY_DONE_RE.search(fresh or ""))
        main = (f"{to_label}(수신) · 우리 부서({label})는 발신" if to_label
                else f"우리 부서({label}) 발신")
        basis = ("발신자가 우리 부서(팀) — 우리가 이미 보낸 메일"
                 + (" · 본문에 회신·송부 완료 표현" if reply_done else ""))
    return {"to_me": to_me, "in_to": in_to, "mentioned": mentioned,
            "ours": ours, "other": (other or (to_label if to_label else "")) if not ours else "",
            "other_kind": ("dept" if other else ("person" if (to_label and not ours) else "")),
            "main": main, "basis": basis,
            "sender_ours": sender_ours, "reply_done": reply_done}


_DUE_WEEK = "월화수목금토일"


def _due_phrase(due: str, text: str) -> str:
    """2026-11-25 + 본문의 `07:00`/`07시` → `11/25(수) 07:00`."""
    m = re.match(r"(20\d{2})-(\d{2})-(\d{2})", str(due or ""))
    if not m:
        return ""
    try:
        wd = _DUE_WEEK[dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).weekday()]
    except ValueError:
        return ""
    when = f"{int(m.group(2))}/{int(m.group(3))}({wd})"
    t = re.search(r"(?<!\d)(\d{1,2})\s*:\s*(\d{2})(?!\d)", str(text or ""))
    if t:
        when += f" {int(t.group(1)):02d}:{t.group(2)}"
    else:
        t2 = re.search(r"(?<!\d)(\d{1,2})\s*시", str(text or ""))
        if t2:
            when += f" {int(t2.group(1)):02d}시"
    return when


def _date_phrase(text: str) -> str:
    """`11/24(화) 07:00 기준` 처럼 '기준·까지'가 붙은 날짜를 결론 문구용으로 뽑는다."""
    s = str(text or "")
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s*[/.\-]\s*(\d{1,2})(?:\s*\(([월화수목금토일])\))?", s):
        around = s[max(0, m.start() - 26):m.end() + 26]
        tail0 = s[m.end():m.end() + 12]
        near_clock = bool(re.search(r"^\s*(?:\d{1,2}\s*[:시])", tail0))
        if "기준" not in around and "까지" not in around and not near_clock:
            continue
        mm, dd = int(m.group(1)), int(m.group(2))
        if not (1 <= mm <= 12 and 1 <= dd <= 31):
            continue
        wd = m.group(3) or _DUE_WEEK[dt.date(dt.date.today().year, mm, dd).weekday()]
        tail = s[m.end():m.end() + 12]
        t = re.search(r"(?<!\d)(\d{1,2})\s*:\s*(\d{2})(?!\d)", tail)
        clock = f" {int(t.group(1)):02d}:{t.group(2)}" if t else ""
        if not clock:
            t2 = re.search(r"(?<!\d)(\d{1,2})\s*시", tail)
            if t2:
                clock = f" {int(t2.group(1)):02d}시"
        return f"{mm}/{dd}({wd}){clock}"
    return ""


def _request_target(mail: dict) -> str:
    """무엇을 요청받았는지 — 제목에서 날짜 꼬리를 떼고 품번을 붙인다."""
    title = str(mail.get("subject") or mail.get("title") or "")
    body = clean_body(mail.get("bodyText") or "")
    t = re.sub(r"^(?:re|fw|fwd|답장|전달)\s*[:：]\s*", "", title.strip(), flags=re.I)
    t = re.sub(r"[\s_\-]*\d{1,2}\s*[/.\-]\s*\d{1,2}\s*(?:\([월화수목금토일]\))?\s*(?:\d{1,2}\s*[:시].*)?$", "", t)
    t = re.sub(r"\s*\[[^\]]{0,20}\]\s*$", "", t).strip(" _-·")
    codes: list[str] = []
    for m in re.finditer(r"(?<!\d)(\d{6,10})(?!\d)", f"{title}\n{body}"):
        c = m.group(1)
        if re.match(r"20\d{6}", c) or c in codes:
            continue
        codes.append(c)
        if len(codes) >= 2:
            break
    target = _clip(t or title, 46) or "요청 건"
    if codes:
        target = f"{target}({', '.join(codes)})"
    return target


def concrete_actions(mail: dict, contacts: dict | None = None, *, ruling: dict | None = None) -> tuple[str, str]:
    """[구체적 행위 + 대상/품번 + 회신 대상자] 형태의 (우리 부서 할 일, 내 할 일)."""
    ruling = ruling or dept_ruling(mail, contacts)
    if ruling.get("sender_ours"):     # v1.2.21 — 우리가 보낸 메일: 발신자에게 회신하라는 결론을 내지 않는다
        return _ours_reply_conclusion(mail, contacts, bool(ruling.get("reply_done")))
    target = _request_target(mail)
    who = _sender_phrase(mail, contacts) or "요청자"
    text = _mail_text(mail)
    due = _due_phrase(deadline_date(text, mail.get("date") or mail.get("receivedAt") or ""), text) or _date_phrase(text)
    when = f"{due}까지 " if due else ""
    if ruling.get("ours"):
        our = f"[직접 회신 필요] {target} 확인·정리 → {when}{who}에게 결과 회신"
        if ruling.get("to_me"):
            my = f"[직접 회신 필요] {target} 담당 확인 후 {when}{who}에게 회신"
        elif ruling.get("mentioned") or ruling.get("in_to"):
            my = f"[참조 · {target} 건 업무 흐름 파악 (팀 회신 여부 확인)]"
        else:
            my = "[참조 · 업무 흐름 파악 (별도 회신 불필요)]"
    else:
        dept = ruling.get("other") or "주담당 부서"
        if ruling.get("other_kind") == "person":
            # 수신(To) 사람만 알 때 — 부서가 같은 팀일 수도 있으니 '타 부서'로 단정하지 않는다 (v1.2.18)
            our = f"[직접 조치 없음 · 수신(To) {dept} 담당 · 참조 흐름 파악 필요]"
        else:
            our = f"[직접 조치 없음 · 타 부서({dept}) 주담당 · 참조 흐름 파악 필요]"
        my = "[타 부서 주담당 건 · 참조 모니터링 (확인 처리 권장)]"
    return our, my


def _share_actions(mail: dict) -> tuple[str, str]:
    """⚪ 기록 보관 — 일이 없더라도 이유와 참조 범위를 업무적인 표현으로 남긴다."""
    target = _request_target(mail)
    return (f"[직접 조치 없음 · {target} 일상 공유 · 참조 흐름 파악]",
            "[참조 · 업무 흐름 파악 (별도 회신 불필요)]")


def polish_actions(result: dict, mail: dict, contacts: dict | None = None) -> dict:
    """AI 가 상투어·일이 없다는 식의 마무리를 냈거나 주담당을 잘못 올렸으면 바로잡는다."""
    ruling = dept_ruling(mail, contacts)
    fb_our, fb_my = concrete_actions(mail, contacts, ruling=ruling)
    if ruling.get("sender_ours"):
        # v1.2.21 — AI 가 ‘[직접 회신 필요] … → 발신자에게 회신’ 으로 써도 우리가 보낸 메일이면 바로잡는다
        result["ourDeptAction"] = fb_our
        result["myAction"] = fb_my
        result["mainDept"] = ruling["main"]
        result["deptBasis"] = str(result.get("deptBasis") or "").strip() or ruling["basis"]
        result["replyStatus"] = "no_reply_needed"
        result["actionItems"] = []
        return result
    our = str(result.get("ourDeptAction") or "").strip()
    my = str(result.get("myAction") or "").strip()
    bad = lambda t: (not t) or any(b in t for b in ACTION_BANNED) or bool(ACTION_BANNED_RE.search(t))
    if ruling["ours"]:
        if bad(our):
            our = fb_our
        if bad(my):
            my = fb_my
    else:
        # v1.2.18 — 우리 부서 일이 아니면 AI 가 ‘[직접 회신 필요]’로 써도 결론·할 일을 우리 팀 기준으로 바로잡는다
        # v1.2.22 — 단, AI 가 이 스레드의 실제 관건(예: 용기 개선 변경 여부 vs 지속 커팅 필요 여부)을
        #   구체적으로 써 냈으면 상투어로 덮어쓰지 않는다. 회신 요구 표현만 참조로 바꾼다.
        our = (_as_reference(our) or fb_our) if _concrete_action(our) else fb_our
        my = (_as_reference(my) or fb_my) if _concrete_action(my) else fb_my
        if result.get("actionItems"):
            result["actionItems"] = []
    main = str(result.get("mainDept") or "").strip()
    mine = our_dept_tokens(contacts) or [our_dept_label()]
    if not ruling["ours"] and (not main or main_is_ours(main, mine)):
        main = ruling["main"]
        result["deptBasis"] = ruling["basis"]
    result["ourDeptAction"] = our
    result["myAction"] = my
    result["mainDept"] = main or ruling["main"]
    if not str(result.get("deptBasis") or "").strip():
        result["deptBasis"] = ruling["basis"]
    # v1.2.18 — 우리 부서 일이 아니면(참조만·타 담당자 주담당) 회신 필요를 남기지 않는다
    if result.get("replyStatus") == "needs_reply" and not ruling["ours"]:
        result["replyStatus"] = "no_reply_needed"
    return result


def thread_cache_key(title: str) -> str:
    key = _norm_title(title or "")
    return key or str(title or "").strip().lower()


def thread_cache_get(store, mail: dict) -> dict | None:
    if not isinstance(store, dict):
        return None
    rec = store.get(thread_cache_key(mail.get("subject") or mail.get("title") or ""))
    return rec if isinstance(rec, dict) and rec.get("timeline") else None


def thread_cache_put(store, mail: dict, analysis: dict, *, used_cache: bool = False) -> dict | None:
    """이 스레드의 누적 타임라인·결론을 남긴다 — 다음 회신은 이 줄만 보고 이어 붙인다."""
    if not isinstance(store, dict) or not isinstance(analysis, dict):
        return None
    key = thread_cache_key(mail.get("subject") or mail.get("title") or "")
    if not key:
        return None
    rec = dict(store.get(key) or {})
    timeline = [str(x).strip() for x in (analysis.get("timeline") or []) if str(x).strip()]
    if not timeline:                       # 모델이 타임라인을 비웠으면 캐시 줄 + 이번 메일 1줄로 보완
        timeline = list(rec.get("timeline") or [])
        line = timeline_line(mail, None, analysis.get("headline") or analysis.get("threadGist") or "")
        if line and not any(line.split(" : ")[0] in t for t in timeline):
            timeline.append(line)
    rec.update({"timeline": timeline[-CACHE_TIMELINE_MAX:],
                "count": len(timeline),
                "background": analysis.get("background") or rec.get("background") or "",
                "mainDept": analysis.get("mainDept") or rec.get("mainDept") or "",
                "deptBasis": analysis.get("deptBasis") or rec.get("deptBasis") or "",
                "ourDeptAction": analysis.get("ourDeptAction") or rec.get("ourDeptAction") or "",
                "myAction": analysis.get("myAction") or rec.get("myAction") or "",
                "headline": analysis.get("headline") or rec.get("headline") or "",
                "summary": analysis.get("summary") or rec.get("summary") or "",
                "usedCache": bool(used_cache),
                "updatedAt": kst_now().strftime("%Y-%m-%d %H:%M")})
    store[key] = rec
    if len(store) > THREAD_CACHE_MAX:            # 오래된 스레드부터 버린다(데이터 크기 보호)
        old = sorted(store.items(), key=lambda kv: str((kv[1] or {}).get("updatedAt") or ""))
        for k, _v in old[: len(store) - THREAD_CACHE_MAX]:
            store.pop(k, None)
    return rec


# ── 3단 조립 · 건너뛰기 ──────────────────────────────────────────
def compose_analysis(timeline, main_dept: str = "", dept_basis: str = "",
                     our_action: str = "", my_action: str = "", background: str = "") -> str:
    """AI 가 준 재료를 화면용 3단 텍스트로 고정 조립한다(형식이 흔들리지 않게).

    v1.2.22 — 1단을 '배경 → 흐름'으로 바꿨다. 원본을 열지 않아도 무슨 일이 왜 생겼고
    각 주체가 무엇을 걸고 있는지 먼저 읽히도록 background 를 맨 앞 줄에 둔다.
    """
    label = our_dept_label()
    who = f"{OWNER.get('name') or ''} {OWNER.get('title') or ''}".strip() or "나"
    lines = ["1) 🔄 업무 진행 경과 (배경 → 흐름)"]
    bg = re.sub(r"\s*\n+\s*", " ", str(background or "")).strip()
    if bg:
        lines.append("- 배경: " + bg)
    tl = [str(t).strip() for t in (timeline or []) if str(t).strip()]
    lines += ["- " + t for t in tl] or ["- (본문 1통 — 이전 회신 없음)"]
    lines += ["", "2) 🎯 요청 대상 및 주담당 부서 판별",
              f"- 주담당: {main_dept or '확인 필요'}",
              "- 판단 근거: " + (dept_basis or "수신(To)·참조(CC) 위치, 본문 지목, 이전 회신자 서명 기준"),
              "", "3) ✅ 결론: 내가 할 일 / 우리 부서 할 일",
              f"- 우리 부서({label}): {our_action or '[확인 필요]'}",
              f"- 나({who}): {my_action or '[확인 필요]'}"]
    return "\n".join(lines)


def is_detailed_summary(text: str) -> bool:
    s = str(text or "")
    return all(k in s for k in ("🔄", "🎯", "✅"))


# ══════════════════════════════════════════════════════════════ 본문 인용 스레드 (v1.2.21)
#  메일함에 1통만 들어와도 본문 아래에는 `-----Original Message-----` / `From:` · `Sent:` /
#  `보낸사람:` · `보낸 날짜:` 같은 인용 블록이 붙어 있다. 히스토리가
#  "이 메일이 스레드의 첫 메일입니다"로 끝나지 않도록 인용 블록을 시간순 스레드에 합친다.
Q_FIELD_KEYS = {
    "from": "from", "보낸사람": "from",
    "sent": "date", "date": "date", "보낸날짜": "date", "보낸때": "date", "일시": "date",
    "subject": "subject", "제목": "subject",
    "to": "to", "받는사람": "to", "cc": "cc", "참조": "cc",
}
Q_FIELD_RE = re.compile(r"^\s*(?:>\s*)?([A-Za-z가-힣][A-Za-z가-힣 ]{0,9}?)\s*[:：]\s*(.*)$")
Q_SEP_RE = re.compile(r"^\s*[>\s]*(?:-{2,}\s*)?(?:original\s*message|원본\s*메일|메일\s*원본|"
                      r"forwarded\s*message|전달된\s*메일|전달\s*메일)(?:\s*-{2,})?\s*$", re.I)
Q_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def quoted_field(line: str) -> tuple[str | None, str]:
    """`From:` · `보낸 사람:` 같은 인용 머리표 한 줄 → (field, value). 머리표가 아니면 (None, "")."""
    m = Q_FIELD_RE.match(str(line or ""))
    if not m:
        return None, ""
    key = re.sub(r"\s+", "", m.group(1)).lower()
    return Q_FIELD_KEYS.get(key), _clean(m.group(2) or "")


def _q_clock(text: str) -> tuple[int, int, bool]:
    """인용 날짜 문자열에서 시:분 — (시, 분, 시각이 있었나). `오후`/`PM` 이면 24시간으로 올린다."""
    m = re.search(r"(?<!\d)(\d{1,2})\s*[:시]\s*(\d{2})?(?!\d)", text or "")
    if not m:
        return 0, 0, False
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    around = (text or "")[:m.end() + 3]
    if hh < 12 and (re.search(r"오후", around) or re.search(r"\bpm\b", text or "", re.I)):
        hh += 12
    if hh == 12 and (re.search(r"오전", around) or re.search(r"\bam\b", text or "", re.I)):
        hh = 0
    return hh, mm, True


def _q_date(raw: str) -> str:
    """인용 날짜 → `YYYY-MM-DD HH:MM` (한국식·영문식 모두). 알아보지 못하면 원문을 짧게 남긴다."""
    s = _clean(raw or "").strip()
    if not s:
        return ""
    y = mo = d = 0
    rest = s
    m = re.search(r"(20\d{2})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        rest = s[m.end():m.end() + 20]
    if not y:
        m2 = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s*(20\d{2})", s)      # Nov 24, 2026
        if m2 and m2.group(1)[:3].lower() in Q_MONTHS:
            y, mo, d = int(m2.group(3)), Q_MONTHS[m2.group(1)[:3].lower()], int(m2.group(2))
            rest = s[m2.end():m2.end() + 20]
    if not y:
        m3 = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(20\d{2})", s)        # 24 Nov 2026
        if m3 and m3.group(2)[:3].lower() in Q_MONTHS:
            y, mo, d = int(m3.group(3)), Q_MONTHS[m3.group(2)[:3].lower()], int(m3.group(1))
            rest = s[m3.end():m3.end() + 20]
    if not y or not (1 <= mo <= 12 and 1 <= d <= 31):
        return _clip(s, 24)
    hh, mm, has = _q_clock(rest or s)
    return f"{y:04d}-{mo:02d}-{d:02d}" + (f" {hh:02d}:{mm:02d}" if has else "")


def _q_from(raw: str) -> str:
    """`"염주희" <johhee.yeom@gm.com>` → `염주희 (GM)` — 화면 스레드 줄에 쓰는 발신자 표기."""
    s = _clean(raw or "").strip().strip(";").strip()
    if not s:
        return ""
    email = ""
    m = re.search(r"<([^<>@\s]+@[^<>\s]+)>", s)
    if m:
        email = m.group(1)
    name = re.sub(r"<[^>]*>", "", s).strip().strip('"\'').strip()
    if not name and email:
        name = email.split("@")[0]
    org = ""
    if email:
        dom = email.split("@")[-1].lower()
        if dom == "gm.com":
            org = "GM"
        elif dom and dom != internal_domain().lower():
            org = dom.split(".")[0].upper()
    label = _clip(name, 20)
    return f"{label} ({org})" if label and org else label


def _q_body_line(lines: list[str]) -> str:
    """인용 블록의 첫 알맹이 줄 — 인사·서명·슬로건·빈 줄은 건너뛴다."""
    cands: list[str] = []
    for ln in lines:
        t = _clean(ln).strip()
        if not t or t.startswith(">"):
            continue
        if is_sign_text(t) or is_sig_noise_line(t) or Q_FIELD_RE.match(t):
            continue
        if re.match(r"^\s*(?:-{2,}|_{2,}|={2,})\s*$", t):
            continue
        if re.match(r"^(?:안녕하십니까|안녕하세요|수고하십니다|수고\s*많으십니다|반갑습니다)", t):
            continue
        if re.fullmatch(r"[A-Za-z가-힣 ,.]{1,24},", t):          # `Seong Gyu,` 같은 호칭 인사
            continue
        cands.append(t)
    if not cands:
        return ""
    for t in cands:                                            # 짧은 인사·이름 줄은 건너뛰고 첫 알맹이 줄
        if len(t) >= 12:
            return t
    return cands[0]


def _q_entry(head: dict, body_lines: list[str]) -> dict | None:
    frm = _q_from(head.get("from") or "")
    date = _q_date(head.get("date") or "")
    subject = re.sub(r"^\s*(?:re|fw|fwd|답장|전달)\s*[:：]\s*", "", _clean(head.get("subject") or ""),
                     flags=re.I).strip()
    gist_body = _q_body_line(body_lines)
    gist = " · ".join(x for x in (_clip(subject, 70), _clip(gist_body, 90)) if x)
    if not (frm or date or subject):
        return None
    return {"date": date, "from": frm or "이전 메일", "gist": gist or "(내용 없음)"}


def quoted_thread_entries(body: str) -> list[dict]:
    """본문 아래 인용 블록(`From:`+`Sent:`+`Subject:` / `보낸사람:`…) → 시간순 스레드 줄."""
    text = str(body or "")
    if not text:
        return []
    entries: list[dict] = []
    cur: dict | None = None
    body_lines: list[str] = []

    def flush() -> None:
        nonlocal cur, body_lines
        if cur is not None:
            entry = _q_entry(cur, body_lines)
            if entry:
                entries.append(entry)
        cur, body_lines = None, []

    for raw in text.splitlines():
        line = raw.rstrip()
        if Q_SEP_RE.match(line) or THREAD_SEP_ONLY_RE.match(line):
            flush()
            cur = {"from": "", "date": "", "subject": ""}
            continue
        field, val = quoted_field(line)
        if field:
            if field == "from" and cur is not None and cur.get("from"):
                flush()                                  # 구분선 없이 다음 메일이 시작된 경우
            if cur is None:
                if field != "from":
                    continue                             # 머리표 없이 날짜만 있는 줄은 인용 시작으로 보지 않는다
                cur = {"from": "", "date": "", "subject": ""}
            if field in ("to", "cc"):
                continue                                 # 수신·참조는 히스토리 줄에 쓰지 않는다
            cur[field] = val
            continue
        if cur is not None and len(body_lines) < 8:
            body_lines.append(line)
    flush()
    entries.sort(key=lambda e: str(e.get("date") or ""))
    return entries


def merge_quoted_thread(thread: list, body: str) -> list:
    """기존 스레드 + 본문 인용 블록 — 같은 날짜·발신자는 이미 있는 줄로 보고 건너뛴다."""
    entries = quoted_thread_entries(body)
    if not entries:
        return list(thread or [])
    have: set[tuple[str, str]] = set()
    for t in thread or []:
        if not isinstance(t, dict):
            continue
        have.add((str(t.get("date") or "")[:10], _clip(_clean(t.get("from") or ""), 12)))
    out = list(thread or [])
    for e in entries:
        key = (str(e.get("date") or "")[:10], _clip(_clean(e.get("from") or ""), 12))
        if key in have:
            continue
        have.add(key)
        out.append(e)
    return dedup_thread(out)


def enrich_threads_from_body(items: list[dict]) -> int:
    """이미 저장된 항목도 본문 인용 블록을 히스토리에 채운다(새 메일이 없어도 1회 정리)."""
    fixed = 0
    for it in items or []:
        if not isinstance(it, dict):
            continue
        body = str(it.get("bodyText") or "")
        if not body:
            continue
        thr = it.get("thread") if isinstance(it.get("thread"), list) else []
        if len(thr) >= 2 and not quoted_thread_entries(body):
            continue
        merged = merge_quoted_thread(thr, body)
        if len(merged) > len(thr) or (thr and merged != thr):
            if merged != thr:
                it["thread"] = merged
                fixed += 1
                last = max([str(m.get("date") or "") for m in merged] + [str(it.get("receivedAt") or "")])[:16]
                if last:
                    it["lastAt"] = last
    return fixed


# ══════════════════════════════════════════════════════════════ 첨부파일 핵심 내용 (v1.2.21)
#  본문이 "첨부 파일 참조 바랍니다" 뿐이면 AI 가 아무것도 모른 채 분석한다.
#  3MB 이하 .xlsx/.xls/.csv/.pdf/.docx/.txt 만 메모리에서 열어 핵심 텍스트를 뽑고,
#  [첨부파일 핵심 요약] 으로 분석 프롬프트에 함께 보낸다. 실패·대용량은 조용히 건너뛴다.
ATT_TEXT_MAX = 3 * 1024 * 1024          # 이 크기까지만 연다(바이트)
ATT_PROMPT_CHARS = 1500                 # 프롬프트에 넣는 최대 글자
ATT_STORE_CHARS = 1200                  # 보드에 저장하는 최대 글자(item 크기 보호)
ATT_TEXT_EXTS = (".xlsx", ".xlsm", ".xls", ".csv", ".pdf", ".docx", ".txt")


def attachment_supported(name: str) -> bool:
    """내용을 열어 볼 수 있는 첨부인가(.xlsx .xls .csv .pdf .docx .txt)."""
    return str(name or "").strip().lower().endswith(ATT_TEXT_EXTS)


def _decode_text(blob: bytes) -> str:
    for enc in ("utf-8-sig", "cp949", "euc-kr", "latin-1"):
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            continue
    return blob.decode("utf-8", "replace")


def _norm_att_text(text: str, limit: int = ATT_PROMPT_CHARS) -> str:
    rows = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in str(text or "").replace("\r", "").splitlines()]
    rows = [r for r in rows if r]
    out: list[str] = []
    size = 0
    for r in rows:
        if size + len(r) + 1 > limit:
            break
        out.append(r)
        size += len(r) + 1
    return "\n".join(out)


def _xlsx_rows_ok(cells: list[str]) -> bool:
    return any(c.strip() for c in cells)


def _xlsx_text_openpyxl(blob: bytes) -> str:
    try:
        from openpyxl import load_workbook          # 외부 라이브러리가 있으면 이쪽이 정확하다
    except Exception:  # noqa: BLE001
        return ""
    try:
        wb = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001
        return ""
    out: list[str] = []
    try:
        for idx, ws in enumerate(wb.worksheets[:2]):
            rows: list[list[str]] = []
            for row in ws.iter_rows(values_only=True, max_row=26):
                cells = [("" if v is None else str(v)).strip() for v in (row or ())]
                if _xlsx_rows_ok(cells):
                    rows.append(cells)
            if not rows:
                continue
            out.append(f"[시트] {ws.title} · {len(rows)}행")
            out += [" | ".join(c for c in r if c) for r in rows[:25]]
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass
    return _norm_att_text("\n".join(out))


def _xlsx_text_stdlib(blob: bytes) -> str:
    """openpyxl 이 없어도 zipfile + xml.etree 로 .xlsx 를 읽는다(표준 라이브러리 폴백)."""
    import zipfile
    import xml.etree.ElementTree as ET
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except Exception:  # noqa: BLE001
        return ""
    shared: list[str] = []
    try:
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.iter(ns + "si"):
                shared.append("".join((t.text or "") for t in si.iter(ns + "t")))
    except Exception:  # noqa: BLE001
        shared = []
    names: list[str] = []
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        names = [str(s.get("name") or "") for s in wb.iter(ns + "sheet")]
    except Exception:  # noqa: BLE001
        names = []
    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    out: list[str] = []
    for idx, path in enumerate(sheets[:2]):
        try:
            root = ET.fromstring(z.read(path))
        except Exception:  # noqa: BLE001
            continue
        rows: list[list[str]] = []
        for r in root.iter(ns + "row"):
            cells: list[str] = []
            for c in r.iter(ns + "c"):
                t = c.get("t")
                v = c.find(ns + "v")
                val = ""
                if t == "s" and v is not None and (v.text or "").strip().isdigit():
                    i = int(v.text.strip())
                    val = shared[i] if i < len(shared) else ""
                elif t == "inlineStr":
                    val = "".join((x.text or "") for x in c.iter(ns + "t"))
                elif v is not None:
                    val = v.text or ""
                cells.append(str(val).strip())
            if _xlsx_rows_ok(cells):
                rows.append(cells)
        if not rows:
            continue
        title = names[idx] if idx < len(names) else f"시트{idx + 1}"
        out.append(f"[시트] {title} · {len(rows)}행")
        out += [" | ".join(c for c in r if c) for r in rows[:25]]
    try:
        z.close()
    except Exception:  # noqa: BLE001
        pass
    return _norm_att_text("\n".join(out))


def _xls_text(blob: bytes) -> str:
    try:
        import xlrd                                 # 옛 .xls 는 xlrd 가 있을 때만
    except Exception:  # noqa: BLE001
        return ""
    try:
        book = xlrd.open_workbook(file_contents=blob)
    except Exception:  # noqa: BLE001
        return ""
    out: list[str] = []
    for sh in book.sheets()[:2]:
        rows = []
        for r in range(min(sh.nrows, 25)):
            cells = [str(sh.cell_value(r, c)).strip() for c in range(sh.ncols)]
            if _xlsx_rows_ok(cells):
                rows.append(" | ".join(c for c in cells if c))
        if rows:
            out.append(f"[시트] {sh.name} · {sh.nrows}행")
            out += rows
    return _norm_att_text("\n".join(out))


def _docx_text(blob: bytes) -> str:
    """.docx 는 zipfile + 정규식으로 문단 텍스트만 뽑는다(외부 라이브러리 불필요)."""
    import html as _html
    import zipfile
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
        z.close()
    except Exception:  # noqa: BLE001
        return ""
    out: list[str] = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)[:80]:
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S))
        text = _html.unescape(text).strip()
        if text:
            out.append(text)
    return _norm_att_text("\n".join(out))


def _pdf_text(blob: bytes) -> str:
    """상위 2페이지 1,500자 — pypdf → PyMuPDF(fitz) 순서로 시도."""
    text = ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(blob))
        text = "\n".join((p.extract_text() or "") for p in reader.pages[:2])
    except Exception:  # noqa: BLE001
        text = ""
    if not text.strip():
        try:
            import fitz                            # PyMuPDF
            doc = fitz.open(stream=blob, filetype="pdf")
            text = "\n".join(doc[i].get_text() for i in range(min(2, doc.page_count)))
            doc.close()
        except Exception:  # noqa: BLE001
            text = ""
    return _norm_att_text(text)


def extract_attachment_text(name: str, blob: bytes) -> str:
    """첨부 1건 → 핵심 텍스트. 지원하지 않거나 읽지 못하면 빈 문자열(조용히 건너뜀)."""
    if not blob or not attachment_supported(name):
        return ""
    ext = str(name).strip().lower().rsplit(".", 1)[-1]
    try:
        if ext in ("xlsx", "xlsm"):
            return _xlsx_text_openpyxl(blob) or _xlsx_text_stdlib(blob)
        if ext == "xls":
            return _xls_text(blob) or _xlsx_text_stdlib(blob)
        if ext == "docx":
            return _docx_text(blob)
        if ext == "pdf":
            return _pdf_text(blob)
        if ext in ("csv", "txt"):
            return _norm_att_text(_decode_text(blob))
    except Exception:  # noqa: BLE001 - 첨부 하나가 분석을 막지 않는다
        return ""
    return ""


def attachment_summary_text(fetcher, mail: dict, *, max_files: int = 2,
                            stats: dict | None = None) -> str:
    """메일 첨부(3MB 이하) 핵심 텍스트 → AI 프롬프트용 `[첨부파일 핵심 요약]` 본문.

    fetcher 는 웹메일 로그인 세션을 쥔 Nmail(fetch_bytes 사용) — 없으면 아무것도 하지 않는다.
    """
    if fetcher is None:
        return ""
    parts: list[str] = []
    for a in (mail.get("attachments") or [])[:8]:
        if len(parts) >= max_files:
            break
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "").strip()
        url = str(a.get("url") or "").strip()
        if not name or not url or not attachment_supported(name):
            continue
        try:
            blob = fetcher.fetch_bytes(url, cap=ATT_TEXT_MAX)
        except TypeError:
            try:
                blob = fetcher.fetch_bytes(url)
            except Exception:  # noqa: BLE001
                blob = None
        except Exception:  # noqa: BLE001
            blob = None
        if not blob:
            continue
        text = extract_attachment_text(name, blob)
        if not text.strip():
            continue
        parts.append(f"· {name} — {text}")
    return "\n".join(parts)


def _att_one_line(mail: dict) -> str:
    """첨부 요약을 화면 한 줄로 — `파일명 · 시트 4행 · 머리글`."""
    txt = str(mail.get("attachmentText") or "").strip()
    txt = re.sub(r"^·\s*[^\n—]{0,90}—\s*", "", txt)
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    sheet = ""
    if lines and lines[0].startswith("[시트]"):
        sheet = re.sub(r"^\[시트\]\s*", "", lines[0]).strip()
        lines = lines[1:]
    first = lines[0] if lines else ""
    name = next((str(a.get("name") or "") for a in mail.get("attachments") or []
                 if isinstance(a, dict) and attachment_supported(a.get("name") or "")), "")
    parts = [p for p in (name, _clip(sheet, 30), first) if p]
    return _clip(" · ".join(parts), 90)


def skip_analysis(mail: dict, reason: str = "simple_share") -> dict:
    """⚪ 기록 보관 — AI 를 전혀 부르지 않는다(Zero-Token). 나중에 화면에서 다시 부를 수 있게 입력만 남긴다."""
    body = clean_body(mail.get("bodyText") or "")
    title = mail.get("subject") or mail.get("title") or ""
    ref = mail.get("date") or mail.get("receivedAt") or ""
    due = deadline_date(f"{title}\n{body}", ref)
    gist, _verb = _gist_clause(body, title, ref)
    head = _clip(gist or title, 60)
    return {
        "summary": SKIP_SIMPLE_SUMMARY,
        "headline": head,
        "actionItems": [],
        "dueDate": due,
        "deadline": (deadline_label(due) if due else "별도 회신 기한 없음"),
        "replyStatus": "no_reply_needed",
        "category": guess_category(title, body),
        "urgency": "normal",
        "threadGist": head,
        "tags": [],
        "replyDrafts": {},
        "aiSkipped": True,
        "aiSkipReason": reason,
        "mainDept": "확인 필요(기록 보관)",
        "deptBasis": "출하리스트·납입요청 등 현장 일상 공유로 분류(수신·참조 위치만)",
        "ourDeptAction": _share_actions(mail)[0],
        "myAction": _share_actions(mail)[1],
        "timeline": [timeline_line(mail, None, head)],
        "analysisBody": analysis_body(mail)[:4000],
    }


def light_analysis(mail: dict, contacts: dict | None = None) -> dict:
    """타 부서 주담당 참조(CC) 메일 — 무거운 스레드 분석 대신 한 줄 경량 요약."""
    body = clean_body(mail.get("bodyText") or "")
    title = mail.get("subject") or mail.get("title") or ""
    ref = mail.get("date") or mail.get("receivedAt") or ""
    due = deadline_date(f"{title}\n{body}", ref)
    gist, _verb = _gist_clause(body, title, ref)
    head = _clip(gist or title, 60)
    who = _sender_phrase(mail, contacts) or (mail.get("from_raw") or "발신자")
    rcpt = _recipient_phrase(mail, contacts, _is_to(mail))
    summary = f"{LIGHT_TAG} {who} → {rcpt}" + (f" : {head}" if head else "")
    _light_ruling = dept_ruling(mail, contacts)
    _light_actions = concrete_actions(mail, contacts, ruling=_light_ruling)
    return {
        "summary": summary,
        "headline": head,
        "actionItems": [],
        "dueDate": due,
        "deadline": (deadline_label(due) if due else "별도 회신 기한 없음"),
        "replyStatus": "no_reply_needed",
        "category": guess_category(title, body),
        "urgency": "normal",
        "threadGist": head,
        "tags": [],
        "replyDrafts": {},
        "aiSkipped": True,
        "aiSkipReason": "other_dept",
        "mainDept": _light_ruling["main"],
        "deptBasis": _light_ruling["basis"],
        "ourDeptAction": _light_actions[0],
        "myAction": _light_actions[1],
        "timeline": [timeline_line(mail, contacts, head)],
        "analysisBody": analysis_body(mail)[:4000],
    }


# ── AI 입력 조립 (네트워크 없음 — 테스트가 이 문자열을 직접 검사한다) ──────────
def analysis_input_text(mail: dict, kind: str = "받은편지함", *, contacts: dict | None = None,
                        cache: dict | None = None, brief: bool = False) -> str:
    lines = [
        f"[편지함] {kind}",
        f"[보낸날짜] {mail.get('date') or mail.get('receivedAt') or ''}",
        f"[보낸사람] {mail.get('from_raw') or mail.get('sender_raw') or mail.get('sender') or ''}",
        f"[받는사람] {', '.join(mail.get('to') or []) or '-'}",
        f"[참조] {', '.join(mail.get('cc') or []) or '-'}",
        f"[제목] {mail.get('subject') or mail.get('title') or ''}",
    ]
    if mail.get("attachments"):
        lines.append("[첨부] " + ", ".join(f"{a.get('name')}({a.get('size')})" for a in mail["attachments"]))
    if mail.get("attachmentText"):
        lines += ["", "[첨부파일 핵심 요약] — 첨부를 열어 뽑은 실제 값. 메일 분석·결론·할 일에 반드시 반영한다",
                  str(mail["attachmentText"])[:ATT_PROMPT_CHARS]]
    lines += ["", org_context(mail, contacts)]
    body = analysis_body(mail)
    if cache and cache.get("timeline"):
        lines += ["", "[기존 누적 타임라인] — 이미 분석한 이 스레드의 시간순 경과(줄은 그대로 두고 끝에 이어 붙인다)"]
        lines += [f"- {t}" for t in cache["timeline"]]
        if cache.get("mainDept") or cache.get("ourDeptAction"):
            lines.append(f"[지난 결론] 주담당 {cache.get('mainDept') or '-'} / "
                         f"우리 부서 {cache.get('ourDeptAction') or '-'} / 나 {cache.get('myAction') or '-'}")
        if cache.get("background"):
            lines.append(f"[지난 배경] {cache['background']}")
        lines += ["[이번 요청] 아래 '새로 추가된 메일 1통'만 읽어 위 타임라인 끝에 이어 붙이고, "
                  "3단 결론(주담당·우리 부서 할 일·내 할 일)을 최신 기준으로 갱신한다.",
                  "배경(background)은 지난 배경을 이어 쓰되, 새 메일로 원인·쟁점이 바뀌었으면 고쳐 쓴다.",
                  "", "[새로 추가된 메일 1통]", body]
    else:
        lines += ["", "[본문 — 최초 → 최신 순 (이전 회신·전달 포함)]", body]
    if brief:
        lines.append("\n[이번 요청] 위 메일은 과거 기록 보강용이다. timeline 은 1~2줄, "
                     "ourDeptAction·myAction 은 한 줄로 짧게 쓰고, actionItems 는 빈 배열, replyDrafts 는 생략한다.")
    return "\n".join(lines)


def normalize_analysis(result: dict, mail: dict, cache: dict | None = None,
                        contacts: dict | None = None) -> dict:
    """AI 응답을 화면 형식으로 정리한다 — 3단 고정 조립, 타임라인 보존, 빠진 필드 보완."""
    out = dict(result or {})
    out = polish_actions(out, mail, contacts)          # 상투어·일이 없다는 마무리·주담당 오판정 교정 (v1.2.13)
    timeline = [str(x).strip() for x in (out.get("timeline") or []) if str(x).strip()]
    if cache and cache.get("timeline"):
        old = [str(t) for t in cache["timeline"] if str(t).strip()]
        if not timeline or len(timeline) < len(old):
            timeline = old + [t for t in timeline if t not in old]
    fields = (out.get("mainDept") or "", out.get("deptBasis") or "",
              out.get("ourDeptAction") or "", out.get("myAction") or "")
    summary = str(out.get("summary") or "").strip()
    # v1.2.22 — 배경은 AI 응답 → 스레드 캐시 순으로 살린다(증분 분석에서도 맥락이 남게)
    background = (str(out.get("background") or "").strip()
                  or str((cache or {}).get("background") or "").strip())
    if timeline or any(fields):
        composed = compose_analysis(timeline, *fields, background=background)
        if not is_detailed_summary(summary) or (background and background[:24] not in summary):
            summary = composed
    out["summary"] = summary
    if background:
        out["background"] = background
    if timeline:
        out["timeline"] = timeline[-CACHE_TIMELINE_MAX:]
    if not out.get("headline"):
        out["headline"] = _clip(out.get("threadGist") or (timeline[0] if timeline else "")
                                or (mail.get("subject") or ""), 60)
    if out.get("replyDrafts") is None:
        out["replyDrafts"] = {}
    return out


def _run_ai(gem, mail: dict, kind: str, contacts: dict | None, cache: dict | None,
            stats: dict | None, brief: bool) -> dict:
    if gem is not None:
        try:
            if isinstance(stats, dict):
                stats["gemini_calls"] = int(stats.get("gemini_calls") or 0) + 1
            res = gem.analyze(mail, kind=kind, contacts=contacts, cache=cache, brief=brief)
            return normalize_analysis(res, mail, cache, contacts)
        except Exception as e:  # noqa: BLE001 - 한 건 실패가 전체를 멈추지 않게
            # 로그가 공개될 수 있으므로 메일 제목·본문은 오류 문구에 넣지 않는다
            msg = f"Gemini 실패(mf_no={mail.get('mf_no')}): {type(e).__name__}: {str(e)[:160]}"
            if isinstance(stats, dict):
                stats.setdefault("warnings", []).append(msg)
            print(f"  ! {msg}", file=sys.stderr)
    return normalize_analysis(heuristic_analysis(mail, _is_to(mail), contacts, cache=cache), mail, cache, contacts)


def analyze_mail(gem, mail: dict, kind: str = "받은편지함", contacts: dict | None = None,
                 *, thread_cache: dict | None = None, stats: dict | None = None,
                 brief: bool = False, force_deep: bool = False, fetcher=None,
                 fresh: bool = False) -> dict:
    """메일 1통 분석 — ⚪ 기록 보관만 AI 생략, 🔴 즉시 확인·🟡 흐름 참고는 3단 정밀 분석 (v1.2.21).

    정밀 분석은 스레드 캐시가 있으면 '누적 타임라인 + 새 메일 1통'만 보낸다(증분).
    fetcher(웹메일 로그인 세션)를 주면 첨부(.xlsx/.pdf/.docx…) 핵심 내용을 뽑아 프롬프트에 함께 보낸다.
    """
    contacts = contacts or {}
    inbox = str(kind or "").startswith("받은")
    if inbox and not brief and not force_deep:
        priority = int(prefilter(mail, contacts).get("priority") or 2)
        mentioned = text_mentions_us(mail)
        to_me = _is_to(mail)          # 내가 직접 수신(To)인가 — 팀원이 받은 것과 구분한다
        # ⚪ 기록 보관 — AI 0회. 단, 내가 수신(To)이거나 본문·제목에 지목된 건은 예외로 정상 분석한다(v1.2.12).
        if priority == 3 and not mentioned and not to_me:
            return skip_analysis(mail, "simple_share")
        # v1.2.21 — 🟡 흐름 참고(참조 CC·타 부서 주담당)도 경량 1줄 대신 처음부터 3단 정밀 분석.
        #   타 부서 주담당이면 분석 안에서 [직접 조치 없음 · 타 부서(OOO) 주담당 · 참조 흐름 파악 필요]로 판정한다.
        #   (v1.2.11 의 `if not mentioned and not to_includes_us(...): return light_analysis(...)` 분기를 걷어냈다)
    # v1.2.21 — 첨부 내용(3MB 이하 .xlsx/.pdf/.docx/.csv/.txt)을 열어 프롬프트에 함께 보낸다
    if fetcher is not None and not mail.get("attachmentText"):
        att = attachment_summary_text(fetcher, mail, stats=stats)
        if att:
            mail["attachmentText"] = att
    # v1.2.22 — fresh: 스레드 캐시를 무시하고 본문 전체를 다시 읽는다([✨ AI 다시 분석] 버튼)
    cache = None if fresh else thread_cache_get(thread_cache, mail)
    res = _run_ai(gem, mail, kind, contacts, cache, stats, brief)
    if inbox and not brief:
        thread_cache_put(thread_cache, mail, res, used_cache=bool(cache))
    return res


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
    # v1.2.14 — 수신 구분(recipientRole)이 낡았거나 비어 있어도(옛 수집분·스레드 병합분),
    #           실제 수신(To) 명단에 내 주소가 있으면 수신으로 보고 🔴 즉시로 분류한다.
    to_me = item.get("recipientRole") == "To" or _item_to_me(item)

    item["mentionedUser"] = mentions_me(body) or mentions_me(title)   # 제목 지목도 포함(v1.2.12)
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

    # 보호 대상 — 내가 수신(To)이거나 제목·본문에 내 이름이 지목됨. ‘기록 보관’ 규칙이 걸려도
    # 이 메일은 흐름 참고 아래로 내려가지 않는다(화면 isProtectedItem 과 같은 기준).
    protected = bool(to_me or item["mentionedUser"])
    labels = {1: P1_LABEL, 2: P2_LABEL, 3: P3_LABEL}
    custom = custom_rule_for(item, protected)
    if custom:
        prio, why = custom
        item["priority"], item["priorityReason"] = prio, f"{labels[prio]} · {why}"
        if prio == 1 and item["vipRank"] == 0 and item["mentionedUser"]:
            item["vipRank"], item["vipLabel"] = 3, "1-3 지목"
    elif item["vipRank"] == 1:
        item["priority"], item["priorityReason"] = 1, f"{P1_LABEL} · {item['vipLabel']} 발신"
    elif item["vipRank"] == 2:
        item["priority"], item["priorityReason"] = 1, f"{P1_LABEL} · {item['vipLabel']} 발신 + 수신인(To)"
    elif item["mentionedUser"]:
        who = OWNER.get("name") or "내 이름"
        item["priority"], item["priorityReason"] = 1, f"{P1_LABEL} · 제목·본문에서 '{who}' 직접 지목"
        if item["vipRank"] == 0:
            item["vipRank"], item["vipLabel"] = 3, "1-3 지목"
    elif to_me and CFG.get("to_is_urgent", True):
        item["priority"], item["priorityReason"] = 1, f"{P1_LABEL} · 나를 수신인(To)으로 지정"
    elif bulk_list:
        # 수신(To)·지목 건은 위에서 이미 1순위라, 여기 오는 건 참조(CC)로만 받은 리스트다.
        item["priority"], item["priorityReason"] = 3, P3_TITLE_REASON
    else:
        item["priority"] = 2
        if gm_sender:
            item["priorityReason"] = f"{P2_LABEL} · 고객사(GM) 발신 · 내가 지목된 건 아님"
        elif category in EXTERNAL_CATS:
            item["priorityReason"] = f"{P2_LABEL} · 사내 주요 이슈 흐름"
        else:
            item["priorityReason"] = f"{P2_LABEL} · 참조(CC) 수신 · 내가 지목된 건 아님"

    # 해외물류/선적이 주된 내용이고 나를 지목하지 않았으면 즉시 확인으로 올리지 않는다
    blob = (title + " " + body).lower()
    ship_hits = sum(blob.count(k) for k in SHIP_KEYWORDS)
    if (item["priority"] == 1 and not item["mentionedUser"] and not to_me and item["vipRank"] != 1
            and (gm_sender or ship_hits >= 2) and ship_hits >= 2):
        item["priority"] = 2
        item["priorityReason"] = f"{P2_LABEL} · 해외물류/선적 건 (내가 지목된 건 아님)"


def repair_to_priority(items: list[dict]) -> int:
    """수신(To)인데 🔴이 아닌 항목을 다시 분류한다 (v1.2.14).

    v1.2.12 이전에 만들어진 항목은 데이터의 priority 가 2 로 남아 있어, 화면이나 다른 기기가
    그 값을 그대로 쓰면 수신(To)인데 🟡 로 보인다(사용자 신고: ‘수신인(To)’ + 🟡 흐름 참고).
    AI 호출 없이 classify() 규칙만 다시 적용하고, 실제로 값이 바뀐 건수만 돌려준다.
    """
    fixed = 0
    for it in items:
        if not isinstance(it, dict) or it.get("archived"):
            continue
        if not (_item_to_me(it) or it.get("recipientRole") == "To"):
            continue
        before = (it.get("recipientRole"), it.get("priority"))
        if it.get("recipientRole") != "To":
            it["recipientRole"] = "To"
        if (it.get("priority") or 2) != 1:
            classify(it)
        if (it.get("recipientRole"), it.get("priority")) != before:
            fixed += 1
    return fixed


# ────────────────────────────────────────────────────────────── Gemini
class GeminiError(RuntimeError):
    pass


ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        # 3단 분석 재료 (v1.2.11) — 화면은 이 값들로 1) 진행 경과 / 2) 주담당 / 3) 결론을 조립한다
        # v1.2.22 — background: 1)의 '배경'(왜 이슈가 생겼고 각 주체가 무엇을 걸고 있는지)
        "background": {"type": "string"},
        "timeline": {"type": "array", "items": {"type": "string"}},
        "mainDept": {"type": "string"},
        "deptBasis": {"type": "string"},
        "ourDeptAction": {"type": "string"},
        "myAction": {"type": "string"},
        "headline": {"type": "string"},
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
                "attach": {"type": "string"},
                "result": {"type": "string"},
                "forward": {"type": "string"},
            },
        },
    },
    "required": ["background", "timeline", "mainDept", "deptBasis", "ourDeptAction", "myAction",
                 "summary", "actionItems", "replyStatus", "category", "urgency", "threadGist"],
}

SYSTEM_PROMPT = """당신은 천일정기화물자동차(주) 납입1팀 김성규 과장의 업무 비서다.
웹메일 한 통을 읽고 대시보드에 표시할 분석 결과를 한국어 JSON 으로만 출력한다.

[대상자의 업무]
- 자재 납입·출하·재고 승인, GM(고객사) 대응, 결품/PTR/EOP/용기승인, 협력사 보급 지시.
- 해외물류부 영역(항공/해상 B/L, 선적 부킹, 포워더, 통관, 선복)은 담당이 아니므로 actionItems 에서 제외한다.

[이 분석의 목적 — 3단 고정]
이 화면은 **웹메일 원본을 열지 않고 이 3단만 읽고 업무를 이해하는 대시보드**다. 지금 이 3단만으로는
"무슨 일이 왜 생겼고 누구 입장이 어떻게 다른지"가 보이지 않아 원본을 다시 열게 만드는 것이 문제다.
그래서 단순한 진행사항 나열이 아니라 **맥락(배경·원인·쟁점)과 실질 주체**를 남긴다.
GM 처럼 여러 부서를 수신(To)/참조(CC)에 한꺼번에 넣어 보내는 메일에서
① 왜 이 이슈가 생겼고 지금 어디까지 왔는지 ② 실제로 결정·개선·조치할 주체가 누구인지
③ 납입관리부(우리 팀)와 김성규 과장이 무엇을 해야 하는지(또는 무엇을 지켜봐야 하는지)를 분명히 가려낸다.
수신/참조 위치, 본문 지목, 인용된 이전 회신의 서명·각인, 업무 성격을 함께 보고 판단한다.
화면에는 이 분석이 아래 3단으로 표시된다 — 너는 각 단에 들어갈 재료를 정확히 채운다.
  1) 🔄 업무 진행 경과 (배경 → 흐름)   → background · timeline
  2) 🎯 요청 대상 및 주담당 부서 판별 → mainDept · deptBasis
  3) ✅ 결론: 내가 할 일 / 우리 부서 할 일 → ourDeptAction · myAction

[background — 1)의 '배경' · 이번 분석에서 가장 중요한 재료]
- 이 스레드를 처음 보는 사람이 "무슨 일이 왜 생겼고, 누가 무엇을 걸고 있는지"를 원본 없이 이해하도록 2~4문장으로 쓴다.
  ① **왜 이슈가 생겼는지** — 사양·조건과 현장의 차이, 사고·클레임·지연의 원인.
     예) "승인된 용기는 HSC 타입인데 현장에는 RSC 로 입고되어 상단을 잘라 보급해 왔고, 그 절단 작업이 작업자 열상 위험을 만들고 있다."
  ② **각 주체의 입장과 어려움** — 고객사(GM)·협력사·우리 현장(조립·생산 라인)이 각각 무엇을 요구하고 무엇을 어려워하는지.
     예) "GM 은 승인 용기로 공급되도록 개선 여부를 검토 중이고, 협력사는 승인서대로 공급해야 한다는 입장이며, 현장은 절단 없이 보급되기를 원한다."
  ③ **현재 상태** — 지금 어디까지 진행됐고 무엇이 아직 정해지지 않았는지.
- 영어 원문·인용 스레드에 원인과 쟁점이 적혀 있으면 그 표현을 살려 쓴다(HSC/RSC, 열상 위험, 승인서 등). 제목만 보고 짐작하지 않는다.
- 근거가 없는 추측은 "(확인 필요)" 로 표시한다. 원래 단순 공유·일정 안내인 메일은 1~2문장으로 줄인다.

[timeline — 1)의 '흐름']
- 최초 발신부터 최신 메일까지 시간순으로 한 줄에 하나씩 `[날짜/시간] 발신자 → 수신자 : 그 메일이 남긴 핵심` 형태로 쓴다.
  예) "[11/18 16:12] GM 염주희 차장 → 납입1팀·협력사 : 승인 용기(HSC)와 달리 RSC 로 입고돼 현장에서 상단 절단 중, 열상 위험 — 승인 용기로 긴급 납품 요청"
- **"진행사항 공유" · "논의" · "확인 요청" · "전달" 처럼 내용 없는 한 줄은 쓰지 않는다.** 그 줄에서 무엇이 결정·요청·우려됐는지 반드시 적는다.
- 일정·품번·수량·조건이 바뀌었으면 그 줄에 바뀐 사실을 적는다(예: "기한 11/24 → 11/25 변경").
- `[기존 누적 타임라인]` 이 함께 오면 **그 줄들은 지우거나 고치지 말고 그대로 두고**, 새로 온 메일 1통만
  끝에 이어 붙여 전체 타임라인을 다시 낸다. 20줄을 넘으면 중간을 "… (중간 N건 생략)" 한 줄로 줄여 18줄 이내로.
- **본문 첫 줄이나 인사말을 그대로 옮기지 않는다.** "○○님," · "요청드립니다" · "안녕하십니까" 같은 문장이
  타임라인에 그대로 남아 있으면 잘못 쓴 것이다.
- 누가(소속·이름·직급) → 누구에게 → 무슨 사유/품번으로 → 언제까지 무엇을, 의 **제3자 보고체**로 쓴다.
- 소속 표기: 보낸 사람 주소가 @gm.com 이면 "GM", 사내면 서명·조직도에 나온 부서명("해외물류부" 등)을 쓴다.

[mainDept / deptBasis — 2) 요청 대상 및 주담당 부서 판별]
- **전달자·협조자가 아니라 이 건을 실제로 결정·개선·조치할 주체**를 찾는다. 수신(To)에 적힌 사람은 메일을 받아
  넘긴 전달자일 수 있다. 원 발신자(인용문의 From)와 최종 조치자를 본다.
  · 사양·용기·포장 승인 변경 → GM(제품·포장 승인 담당)
  · 금형·부품 사양·품질 → 설계·품질 담당
  · 선적·통관·부킹 → 해외물류부 / 발주·도입 → 조달물류팀 / 인사·급여·근태 → 경영지원팀
  · 원인을 제공한 협력사가 조치해야 하면 → 협력사 OO(개선 조치)
- mainDept 표기: `주체(역할)` 로 쓰고, 우리 팀이 참조면 뒤에 붙인다.
  예) "GM 염주희 차장(용기 사양 승인·결정) · 우리 부서(납입1팀)는 참조"
  예) "협력사 ILLINOIS TOOL WORKS(승인 용기 공급) · GM(사양 승인) · 우리 부서(납입1팀)는 참조"
- deptBasis: 그 판단 근거 1줄 — 누가 무엇을 결정·조치해야 하는지가 본문·인용 어디에 근거가 있는지.
- **수신(To)에 우리 팀이 들어 있다는 사실만으로 우리 팀 주담당이라고 쓰지 않는다.** 본문에서 실제 요청·회신을 받는
  부서/사람이 따로 있으면(예: 선적·통관→해외물류부, 도입·발주→조달물류팀, 생산·공정→제조부, 인사·급여→경영지원팀)
  mainDept 는 그 부서로 쓴다. 우리 팀이 참조(CC)로만 있고 본문 지목도 없으면 replyStatus 는 no_reply_needed.
- **발신자가 To 를 다른 담당자·다른 부서로 지정하고 우리 팀은 참조(CC)에만 있으면, 우리 팀 이름·팀원 이름이
  인용문·서명·이전 회신에 스쳐 나오더라도 주담당을 우리 팀으로 올리지 않는다.** 새로 쓴 본문에서 우리 팀·팀원에게
  직접 요청한 문장(예: "납입1팀 확인 부탁드립니다", "김성규 과장님 회신 부탁드립니다")이 있을 때만 직접 회신으로 본다.
- **발신자가 나 또는 우리 부서(팀) 팀원이면 우리가 이미 보낸 메일이다** (예: 납입1팀 강한 사원 → 경영지원팀 정하건).
  발신자에게 다시 회신하라는 결론을 절대 쓰지 않는다. `RE:` 답장이거나 본문에 `해당자 없습니다`·`송부드립니다`·
  `회신드립니다`·`전달드립니다`·`확인 완료` 같은 회신·송부 완료 표현이 있으면 ourDeptAction 을
  `[회신·송부 완료 · 우리 부서(발신자) → 수신자(OOO)에게 처리 완료]`, myAction 을 `[추가 회신 없음 · 처리 이력 확인용]`
  으로 쓰고 replyStatus 는 no_reply_needed, actionItems 는 빈 배열로 둔다. 본문의 핵심 결론(예: "해당자 없음",
  "자료 송부 완료")은 headline·timeline 에 원문 표현 그대로 남긴다.
- [사내 조직 컨텍스트] 의 부서 매핑을 적극적으로 쓴다. "나"가 속한 부서가 우리 부서다.

[ourDeptAction / myAction — 3) 결론]
- **금지 문구**: "본문 요청 내용 확인 후 회신", "본문 확인 후 회신", "본문 확인 후 조치", "본문 요청 확인",
  "내용 확인 후 회신" 같은 두루뭉술한 상투어. **맡은 일이 없다는 뜻의 짧은 마무리도 절대 쓰지 않는다.**
  할 일이 없으면 왜 없는지(타 주체 주담당·기록 보관)와 무엇을 계속 볼지(핵심 관건·모니터링)를 적는다.
- **타 부서·고객사·협력사가 주담당인 건도 빈칸이나 상투어로 끝내지 않는다.** 이 스레드에서 우리 팀이 지켜볼
  **실제 관건**을 한 줄로 쓴다.
  · 형식: `[참조 모니터링 · 핵심 관건: 무엇이 결정되면 이 건이 끝나는지 + 우리 현장·납입에 미치는 영향]`
  · 예) `[참조 모니터링 · 핵심 관건: 용기 사양 개선(HSC 승인 용기로 변경) 여부 vs 현장 지속 절단 필요 여부 — 입고 지연 시 조립 라인 결품 위험]`
  · "무엇이 결정되면 끝나는가"(사양 변경 승인 여부, 회신 마감, 재고 소진 시점, 공급 재개일)가 드러나야 한다.
  · `[직접 조치 없음 · 참조 흐름 파악 필요]` 처럼 무엇을 볼지 없는 문구만 쓰지 않는다.
- ourDeptAction: [구체적 행위 + 대상/품번·기준 + 회신 대상자·형태] 가 드러나게 한 줄로 쓴다.
  · 직접 할 일이 있으면 `[직접 회신 필요] 11/25(수) 07시 기준 2품번(13524683·13540045) 재고조사 결과 정리 → 요청자(유광승 과장)에게 회신`
    처럼 날짜·품번·수량·회신 대상을 원문 값으로 넣는다.
  · 우리 부서 일이 아니면 위 관건 형식으로 쓴다(또는 `[타 부서(해외물류부) 진행 건 · 우리 납입 일정 영향 모니터링]`).
- myAction: 나(수신자) 개인 기준 한 줄.
  · 내가 회신·조치할 건: `[직접 회신 필요] 서열자재 보급 기준 첫차·마지막 차량 PO 확인 후 요청자에게 회신`
  · 참조만 된 건: `[참조 · 핵심 관건 (예: 승인 용기 변경 여부) 확인 후 필요 시 우리 현장 의견 전달]`
    또는 `[참조 모니터링 · 우리 납입 일정·재고 영향 여부 확인 (별도 회신 불필요)]`
- **주담당 판별 규칙**: 본문에서 실제 회신을 요구받은 대상이 다른 주체면, 그 건은 타 주체 결론으로 쓰고
  replyStatus·actionItems 도 우리 팀 기준으로 비운다. 단, 결론 문구에 그 건의 핵심 관건은 반드시 남긴다.

[headline]
- 목록·검색에 쓰는 한 줄(60자 이내). "누가 → 무엇을 요청" 형태. 3단 요약을 다시 쓰지 않는다.

[summary]
- 위 재료를 3단 그대로 옮긴 텍스트. background·timeline·mainDept·deptBasis·ourDeptAction·myAction 을 채우면
  화면이 같은 형식으로 다시 조립하므로, 재료를 정확히 채우는 것이 우선이다.
- 수신인·참조 설명("수신 A·B(To)", "김성규 과장은 참조", "내 조치 없음")은 쓰지 않는다. 수신 정보는 별도 필드로 저장된다.
- 날짜·품번·수량·기한 같은 사실은 원문 그대로 옮긴다. 추측은 "확인 필요"로 표시한다.
- 본문이 리스트·표 위주면 무엇이 몇 건인지, 회신이 필요한지만 한 줄로 압축한다.

[actionItems]
- 김성규 과장이 실제로 해야 하는 일만 0~4개. 없으면 빈 배열.
- 보고/RE/FW 성격이거나 참조(CC)로 받은 기록 보관면 비운다.

[dueDate / deadline]
- 본문에 명시된 요청 기한을 YYYY-MM-DD 로. 없으면 dueDate 는 null, deadline 은 "별도 회신 기한 없음" 같은 한 줄.

[replyStatus]
- needs_reply: 김성규 과장이 답을 보내야 하는 요청·질의·확인 건.
- no_reply_needed: 기록 보관·참고·이미 종결된 건.
- replied: 본문에 이미 회신한 흔적이 뚜렷한 건.

[replyDrafts]
- replyStatus 가 needs_reply 일 때만 3가지를 쓴다(그 외에는 생략).
- **말투 3종이 아니라 실무 상황 3종**이다. 세 안이 서로 다른 내용이어야 하고, "확인했습니다. 회신드리겠습니다"
  같은 접수·보류 멘트만 있으면 실패다(실무에서 그런 답장은 보내지 않는다).
- attach(📎 자료 송부형): 요청받은 자료·파일을 정리해 첨부로 바로 보낼 때.
  예) "요청하신 11월 KPI & BPD 자료를 정리하여 첨부 송부드립니다. 첨부된 파일 확인 부탁드리며, 추가 확인 사항이
  있으시면 말씀해 주시기 바랍니다." 실제 자료명·품번·건수를 넣고, [첨부] 목록이 있으면 "- 첨부: 파일명 (크기)" 한 줄로 적는다.
- result(✅ 조치·결과 회신형): 첨부 없이 확인·조치 결과를 본문으로 답할 때.
  예) "요청하신 2품번 재고실사 건 확인 결과 공유드립니다. 13524683 11EA·13540045 5EA로 확인되었으니 참고 부탁드립니다."
  원문에 수치·품번·일자가 있으면 그대로 쓰고, 모르면 "- 확인 결과: [수치·조치 내용 기입]"처럼 채울 자리만 남긴다.
- forward(👉 담당자 전달·확인중): 담당자·유관부서 확인이 필요해 기한을 두고 다시 회신할 때.
  예) "보내주신 26년 11월 KPI 자료 회신 요청 건 확인했습니다. 본 건은 관련 담당자 확인 후 12/3(목)까지 재회신드리겠습니다."
- 말투 자동: 보낸 사람 주소가 @chunil.co.kr 이 아니면(외부·GM) 격식체 —
  첫 줄 "{이름 직급}님, 안녕하십니까." + 둘째 줄 "천일정기화물자동차(주) {부서} 김성규입니다.",
  사내(@chunil.co.kr)면 "{이름 직급}님, 안녕하세요." 로 시작한다.
- **인사말은 한 번만**: "안녕하세요."를 두 줄 연속 쓰거나 첫 인사 뒤에 또 쓰지 않는다.
- **호칭 띄어쓰기**: "과장 님"·"차장 님"처럼 직급과 '님'을 띄우지 않는다 → "과장님".
- 서명은 "김성규 드림"(외부면 그 아래 "천일정기화물자동차(주) {부서}")으로 끝낸다.

[category] 승인/검토, PTR, 결품/보고, 불량/품질, 재고/실사, 현황/보고, 출하/보급, 납입요청, 일정/공유,
조직/업무, 공지/안전, 자료요청, 자재/확인, 기타 중 하나.
[urgency] normal/high/urgent. [threadGist] 스레드에 한 줄로 남길 이 메일의 핵심(40자 이내).
[tags] 품번·차종·키워드 0~5개.
"""


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

    def analyze(self, mail: dict, *, kind: str = "받은편지함", brief: bool = False,
                contacts: dict | None = None, cache: dict | None = None) -> dict:
        #  v1.2.11 — 이전 회신을 자르지 않은 전체 스레드 + 사내 조직 컨텍스트.
        #  스레드 캐시(cache)가 있으면 '누적 타임라인 + 새 메일 1통'만 보낸다(증분 분석).
        text = analysis_input_text(mail, kind, contacts=contacts, cache=cache, brief=brief)
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": text}]}],
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



# ────────────────────────────────────────────────────────────── 실무형 답장 초안 3종 (v1.2.8)
#  말투 3종(정중·사실·사내)이 아니라 **실무 상황 3종**이다. 같은 내용을 말만 바꿔 쓰면
#  "확인했습니다. 회신드리겠습니다"밖에 남지 않아 실제 답장에 쓸 수 없다는 지적(11-24)을 반영했다.
#  개인판(정적 보드)에는 OWNER·DEFAULT_COMPANY 설정이 없어도 이 모듈이 돌아가게 한다.
try:                                                         # noqa: B018 - 존재 확인
    OWNER
except NameError:                                            # pragma: no cover
    OWNER = {"name": "김성규", "title": "과장", "dept": "납입1팀",
             "company": "천일정기화물자동차", "email": "kimpro@chunil.co.kr"}
try:
    DEFAULT_COMPANY
except NameError:                                            # pragma: no cover
    DEFAULT_COMPANY = "천일정기화물자동차"
DRAFT_DOMAIN = "chunil.co.kr"                                # 사내 메일 도메인(외부/사내 말투 판정)

TONE_KEYS = ("attach", "result", "forward")      # 📎 자료 송부형 · ✅ 조치·결과 회신형 · 👉 담당자 전달·확인중
TONE_LEGACY = {"attach": "polite", "result": "factual", "forward": "concise"}   # v1.2.7 이전 키
RANK_WORDS = (r"과장|차장|부장|대리|사원|주임|계장|팀장|실장|소장|상무|전무|이사|사장|"
              r"매니저|프로|선임|수석|원장|센터장|파트장")
RANK_NIM_RE = re.compile(rf"({RANK_WORDS})\s+님")        # "과장 님" → "과장님"
GREET_ONLY_RE = re.compile(r"^(?:안녕하십니까|안녕하세요|수고하십니다|수고하셨습니다|반갑습니다)\s*[,.!?]*$")
GREET_ANY_RE = re.compile(r"안녕하십니까|안녕하세요|수고하십니다|반갑습니다")
DRAFT_HEAD_RE = re.compile(r"^(?:수신|받는\s*사람|제목|참조|보낸\s*사람|일자|날짜)\s*[:：]")
DRAFT_TOPIC_TAIL_RE = re.compile(
    r"\s*(?:에\s*대한\s*)?(?:회신|송부|제출|확인|공유|자료)?\s*요청\s*(?:의\s*)?(?:건)?\s*$")
EMPTY_ACK_RE = re.compile(r"(?:확인|접수)\s*(?:을\s*)?(?:완료|했습니다|하였습니다|드렸습니다)")
PROMISE_RE = re.compile(r"(?:회신|송부|보내|연락|공유|전달)\s*드리(?:겠|도록)")


def fix_honorific(text: str) -> str:
    """'과장 님'·'차장 님' 처럼 직급과 '님' 사이를 띄운 표기를 붙여 쓴다."""
    t = RANK_NIM_RE.sub(r"\1님", text or "")
    return re.sub(r"\s+([,.])", r"\1", t)


def _honorific(name: str) -> str:
    """'김태영 과장' → '김태영 과장님', 직급이 없으면 '김태영 님'."""
    n = re.sub(r"\s*\([^)]*\)\s*", " ", name or "").strip()
    n = re.sub(r"\s+", " ", n)
    n = RANK_NIM_RE.sub(r"\1님", n)
    if not n:
        return "담당자님"
    if n.endswith("님"):
        return n
    if re.search(rf"(?:{RANK_WORDS})$", n):
        return n + "님"
    return n + " 님"


def call_name(sender: str, email: str = "", contacts: dict | None = None) -> str:
    """답장 첫 줄에 쓸 호칭 — 사내 주소록·발신자 표기에서 이름·직급을 뽑는다."""
    name, title, _org = _sender_parts(sender or "", email or "", contacts or {})
    base = f"{name} {title}".strip() if name else ""
    if not base:
        base = sender or ""
    return _honorific(base)


def is_external(sender_email: str, sender_label: str = "") -> bool:
    """받는 사람(원 발신자)이 외부인가 — @chunil.co.kr 이 아니면 외부(격식체로 쓴다)."""
    em = (sender_email or "").strip().lower()
    if em:
        return not em.endswith("@" + DRAFT_DOMAIN)
    return "GM" in (sender_label or "") or "고객사" in (sender_label or "")


def draft_topic(title: str, limit: int = 60) -> str:
    """'RE: 26년 11월 KPI & BPD 자료 … 회신 요청' → '26년 11월 KPI & BPD 자료 …'."""
    t = str(title or "").replace("\r", " ")
    for _ in range(3):
        t = re.sub(r"^\s*(?:re|fw|fwd|답장|전달)\s*[:：]\s*", "", t, flags=re.I)
    t = re.sub(r"^\s*\[[^\]]{0,24}\]\s*", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    cut = DRAFT_TOPIC_TAIL_RE.sub("", t).strip(" ,·-")
    if len(cut) >= 4:
        t = cut
    return (t[:limit].rstrip() + "…") if len(t) > limit else t


def draft_due_text(due: str) -> str:
    """'2026-12-03' → '12/3(목)' — 답장에 그대로 쓰는 기한 표기."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(due or ""))
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        wd = "월화수목금토일"[dt.date(y, mo, d).weekday()]
    except ValueError:
        return ""
    return f"{mo}/{d}({wd})"


def _draft_attach_line(atts: list[dict]) -> str:
    names = []
    for a in (atts or [])[:3]:
        nm = str(a.get("name") or "").strip()
        if not nm:
            continue
        size = str(a.get("size") or "").strip()
        names.append(f"{nm} ({size})" if size else nm)
    if not names:
        return ""
    more = len(atts) - len(names)
    return "- 첨부: " + ", ".join(names) + (f" 외 {more}건" if more > 0 else "")


def _draft_core(text: str) -> str:
    """예전 초안에서 인사말·수신/제목 줄·서명을 걷어낸 '알맹이'만 남긴다.

    한 줄짜리로 붙어 온 초안("○○님, 안녕하세요.\\n…내용…\\n감사합니다.\\n○○ 드림")에서도
    내용이 사라지지 않게 줄 단위로 걷어낸다.
    """
    out: list[str] = []
    for raw in str(text or "").replace("\r", "").split("\n"):
        s = re.sub(r"[ \t]+", " ", raw).strip()
        if not s:
            if out and out[-1] != "":
                out.append("")
            continue
        if GREET_ONLY_RE.match(s):
            continue
        if DRAFT_HEAD_RE.match(s):
            continue
        if re.match(r"^(?:감사합니다|수고하십시오)\s*[.!]?$", s):
            continue
        if re.search(r"(?:드림|배상)\s*$", s) and len(s) < 40:
            continue
        if re.match(r"^천일정기화물자동차", s) and len(s) < 60:     # 소속 소개 줄
            continue
        # "○○님, 안녕하세요." 처럼 호칭+인사가 한 줄에 붙은 경우 인사 부분만 걷어낸다
        s = re.sub(r"^\s*(?:[가-힣]{2,4}\s+)?(?:[가-힣]{2,3}\s*님\s*,?\s*)?"
                   r"(?:안녕하십니까|안녕하세요|수고하십니다)\s*[,.!]?\s*", "", s).strip()
        if not s:
            continue
        if re.match(r"^[가-힣]{2,4}\s*(?:[가-힣]{2,3}님)?\s*[,.]?$", s):   # 이름만 있는 줄
            continue
        if re.match(r"^(?:확인|검토)\s*(?:후|부탁)", s) and len(s) < 90:     # 맺음말
            continue
        if re.search(r"추가로?\s*(?:필요한|확인할)\s*(?:사항|부분)이?\s*있으면", s) and len(s) < 110:
            continue
        if re.match(r"^(?=[^0-9]*$).{0,60}(?:확인했습니다|접수했습니다|확인하였습니다)[.\s]*$", s):
            continue                                            # "…확인했습니다." 만 있는 줄
        out.append(s)
        if len(out) >= 8:
            break
    core = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return core[:600]


def _owner_sign() -> tuple[str, str]:
    """(서명 이름, 소속 한 줄) — 예) ("김성규", "천일정기화물자동차(주) 납입1팀")"""
    name = (OWNER.get("name") or "").strip()
    company = (OWNER.get("company") or DEFAULT_COMPANY).strip()
    if company and not re.search(r"\(주\)|㈜", company):
        company += "(주)"
    org = " ".join(x for x in (company, (OWNER.get("dept") or "").strip()) if x)
    return name, org


def _draft_has_dup_greeting(text: str) -> bool:
    """'안녕하세요.' 가 두 번(줄 연속·문단 연속) 들어간 초안인가."""
    t = str(text or "")
    lines = [ln.strip() for ln in t.split("\n")]
    greets = [ln for ln in lines if GREET_ONLY_RE.match(ln)]
    if len(greets) >= 2:
        return True
    return bool(re.search(r"안녕하십니까|안녕하세요", t) and
                len(re.findall(r"(?:안녕하십니까|안녕하세요)\s*[.!]?", t)) >= 2)


def _looks_empty_ack(text: str) -> bool:
    """'확인했습니다. 회신드리겠습니다' 식의 접수·보류 멘트뿐인가(옛 3종 초안 판별)."""
    core = _draft_core(text)
    if not core:
        return True
    if not (EMPTY_ACK_RE.search(core) and PROMISE_RE.search(core)):
        return False
    concrete = bool(re.search(r"\d{2,}|[A-Z]{2,}\d|품번|첨부|파일|까지", core))
    return not concrete


def _draft_bullets(acts: list[str], limit: int = 3) -> str:
    """할 일(actionItems)을 답장 본문에 넣을 글머리표로 — 실제 품번·기한이 그대로 남는다."""
    out = []
    for a in (acts or [])[:limit]:
        s = re.sub(r"\s+", " ", str(a or "")).strip()
        s = re.sub(r"^(?:\d{1,2}[).]\s*|[-•·]\s*)", "", s)
        if s:
            out.append("- " + s)
    return "\n".join(out)


def build_reply_drafts(item: dict, core: str = "") -> dict:
    """항목 하나로 실무형 3안을 만든다 (Gemini 없이도 쓰는 기본값).

    - attach  📎 자료 송부형        : 요청받은 자료를 정리해 첨부로 바로 보낼 때
    - result  ✅ 조치·결과 회신형   : 첨부 없이 결과·수치·조치를 본문으로 답할 때
    - forward 👉 담당자 전달·확인중 : 담당자·유관부서 확인 후 기한을 두고 재회신할 때
    말투는 받는 사람에 맞춰 자동 — 외부(@gm.com 등)는 격식체, 사내(@chunil.co.kr)는 간결체.
    """
    ext = is_external(item.get("senderEmail") or "",
                      item.get("sender") or item.get("senderName") or "")
    who = call_name(item.get("senderName") or item.get("sender") or "", item.get("senderEmail") or "")
    name, org = _owner_sign()
    topic = draft_topic(item.get("title") or "") or "보내주신 건"
    due = draft_due_text(item.get("dueDate") or "")
    obj = _josa(topic, "을", "를")
    hello = f"{who}, 안녕하십니까." if ext else f"{who}, 안녕하세요."
    head = (hello + "\n" + f"{org} {name}입니다." + "\n\n") if (ext and org and name) else (hello + "\n\n")
    sign = "\n\n감사합니다.\n\n" + (f"{name} 드림" if name else "드림") + (f"\n{org}" if ext and org else "")
    atts = item.get("attachments") or []
    att_line = _draft_attach_line(atts)
    bullets = _draft_bullets([str(a) for a in (item.get("actionItems") or [])])
    core = (core or "").strip()
    ask_ext = "추가로 확인하실 사항이 있으시면 말씀해 주시기 바랍니다."
    ask_int = "추가로 확인이 필요한 부분이 있으면 말씀 주세요."

    body = ([f"요청하신 {topic}{obj} 정리하여 첨부 송부드립니다."] if atts
            else [f"요청하신 {topic} 관련 내용 정리하여 공유드립니다."])
    if att_line:
        body.append(att_line)
    if bullets:
        body.append(bullets)
    elif core:
        body.append(core)
    if ext:
        body.append(("첨부한 파일 확인 부탁드리며, " if atts else "확인 부탁드리며, ") + ask_ext)
    else:
        body.append(("첨부한 파일 확인 부탁드리고, " if atts else "확인 부탁드리고, ") + ask_int)
    attach = head + "\n\n".join(body) + sign

    body = [f"요청하신 {topic}{obj} 확인한 결과 공유드립니다."]
    if bullets:
        body.append(bullets)
    elif core:
        body.append(core)
    else:
        body.append("- 확인 결과: [수치·조치 내용 기입]")
    body.append("이상 확인하였습니다. " + (ask_ext if ext else ask_int))
    result = head + "\n\n".join(body) + sign

    body = [f"보내주신 {topic} 건 확인했습니다.",
            "본 건은 관련 담당자·유관부서 확인 후 " + (f"{due}까지 " if due else "") + "재회신드리겠습니다.",
            "확인되는 대로 진행 상황을 공유드리겠습니다."]
    forward = head + "\n\n".join(body) + sign

    return {"attach": fix_honorific(attach), "result": fix_honorific(result), "forward": fix_honorific(forward)}


def polish_draft(text: str, who: str = "", ext: bool = False) -> str:
    """AI 가 쓴 초안 정리 — 인사말은 한 번만, 호칭은 '과장님', 서명은 한 번."""
    t = fix_honorific(str(text or "").replace("\r", ""))
    paras = [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]
    kept: list[str] = []
    seen_greet = False
    for p in paras:
        lines: list[str] = []
        for raw in p.split("\n"):
            s = raw.strip()
            if not s:
                continue
            if GREET_ONLY_RE.match(s):
                if seen_greet:
                    continue                     # 두 번째 인사말은 버린다
                seen_greet = True
                lines.append(s)
                continue
            if GREET_ANY_RE.search(s):
                seen_greet = True
            lines.append(s)
        if lines:
            kept.append("\n".join(lines))
    body = "\n\n".join(kept).strip()
    name, org = _owner_sign()
    first = body.split("\n", 1)[0] if body else ""
    if who and not (first.startswith(who) or GREET_ANY_RE.search(first[:40])):
        head = f"{who}, 안녕하십니까." if ext else f"{who}, 안녕하세요."
        if ext and org and name:
            head += f"\n{org} {name}입니다."
        body = head + ("\n\n" + body if body else "")
    if name and not re.search(rf"{re.escape(name)}\s*(?:드림|배상)", body[-80:]):
        body = (body + "\n\n감사합니다.\n\n" + name + " 드림" + (f"\n{org}" if ext and org else "")).strip()
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def normalize_reply_drafts(drafts: dict | None, item: dict) -> dict:
    """AI 초안을 새 3안 키로 옮기고 다듬는다. 빠진 안은 기본 초안으로 채운다.

    v1.2.7 이전 초안(polite/factual/concise)이라도 알맹이(수치·품번·조치)가 있으면
    그 내용을 살려 새 안에 담는다 — 말투만 바꾸는 초안으로 되돌아가지 않게.
    """
    src = drafts if isinstance(drafts, dict) else {}
    ext = is_external(item.get("senderEmail") or "", item.get("sender") or "")
    who = call_name(item.get("senderName") or item.get("sender") or "", item.get("senderEmail") or "")
    legacy_core = ""
    for k in TONE_KEYS:
        lv = src.get(TONE_LEGACY[k])
        if isinstance(lv, str) and lv.strip() and not _looks_empty_ack(lv):
            cand = _draft_core(lv)
            if len(cand) > len(legacy_core):
                legacy_core = cand
    base = build_reply_drafts(item, legacy_core)
    out: dict = {}
    for k in TONE_KEYS:
        v = src.get(k)
        out[k] = polish_draft(v, who, ext) if isinstance(v, str) and v.strip() else base[k]
    return out


def refresh_reply_drafts(items: list[dict]) -> int:
    """이미 저장된 옛 3종 초안(말투만 다른 접수 멘트 · 인사말 중복)을 실무형 3안으로 바꾼다 (v1.2.8).

    새 키(attach/result/forward)로 저장된 초안은 인사말 중복·호칭 띄어쓰기만 다듬는다.
    사용자가 브라우저에서 직접 고친 문구는 data 에 저장되지 않으므로(localStorage) 건드리지 않는다.
    """
    n = 0
    for it in items or []:
        if it.get("archived"):
            continue
        cur = it.get("replyDrafts")
        if not isinstance(cur, dict) or not cur:
            continue
        has_new = any(isinstance(cur.get(k), str) and cur.get(k).strip() for k in TONE_KEYS)
        broken = any(_draft_has_dup_greeting(v) or RANK_NIM_RE.search(v)
                     for v in cur.values() if isinstance(v, str))
        if has_new and not broken:
            continue
        new = normalize_reply_drafts(cur, it)
        if new != cur:
            it["replyDrafts"] = new
            n += 1
    return n


def heuristic_analysis(mail: dict, to_me: bool, contacts: dict | None = None,
                       *, cache: dict | None = None) -> dict:
    """AI 없이(또는 실패 시) 만드는 3단 분석 — AI 와 같은 형식(진행 경과·주담당·결론)으로 채운다."""
    body = clean_body(mail.get("bodyText") or "")
    core = strip_greeting(body) or body          # 인사말·소속 소개는 요약에서 뺀다
    first = _clean(core)[:280]
    title = mail.get("subject") or mail.get("title") or ""
    ref = mail.get("date") or mail.get("receivedAt") or ""
    # 본문에 `제출 기한 : 12월 2일` 처럼 적힌 기한은 AI 없이도 그대로 읽어 dueDate 를 채운다
    due = deadline_date(f"{title}\n{body}", ref)
    cat = guess_category(title, body)
    _line, gist = third_person_summary(mail, contacts, to_me)
    timeline = [str(t) for t in ((cache or {}).get("timeline") or []) if str(t).strip()]
    if not timeline:
        line_gist = gist or title
        reason = _reason_clause(body)          # `(MY26 EOP 자재로 입항지연 예상 )` 같은 사유도 함께
        if reason and reason not in line_gist:
            line_gist = f"{line_gist} ({reason})"
        timeline = [timeline_line(mail, contacts, line_gist)]
    ruling = dept_ruling(mail, contacts)
    main_dept = ruling["main"]
    basis = ruling["basis"]
    our_action, my_action = concrete_actions(mail, contacts, ruling=ruling)
    needs_reply = bool(ruling["to_me"] and ruling["ours"])
    return {
        "summary": compose_analysis(timeline, main_dept, basis, our_action, my_action),
        "headline": _clip(gist or first or title, 60),
        "mainDept": main_dept,
        "deptBasis": basis,
        "ourDeptAction": our_action,
        "myAction": my_action,
        "timeline": timeline,
        "actionItems": [],
        "dueDate": due,
        "deadline": (deadline_label(due) if due
                     else ("별도 회신 기한 없음" if not to_me else "확인 필요")),
        "replyStatus": "needs_reply" if needs_reply else "no_reply_needed",
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


def _item_to_me(item: dict) -> bool:
    """항목(item)의 수신(To) 명단에 로그인 사용자가 직접 들어 있는가 (v1.2.14).

    데이터의 `recipientRole` 은 옛 수집분·스레드 병합분에서 낡아 있을 수 있어(수신인데 CC 로 남는 등)
    등급을 정할 때는 저장된 구분값과 별개로 실제 수신 명단을 다시 본다.
    """
    target = (OWNER.get("email") or ME or "").strip().lower()
    if not target:
        return False
    for raw in item.get("to") or []:
        if _addr_email(raw) == target:
            return True
    return False


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
    # v1.2.21 — 본문 아래 인용 스레드(From:/Sent:/보낸사람:)를 히스토리에 합친다
    merged_thread = merge_quoted_thread(item.get("thread") or [], mail.get("bodyText") or "")
    if merged_thread:
        item["thread"] = merged_thread
    # v1.2.21 — 첨부에서 뽑은 핵심 내용: 분석 재실행용 원문(짧게) + 화면 한 줄 요약
    if mail.get("attachmentText"):
        item["attachmentText"] = str(mail["attachmentText"])[:ATT_STORE_CHARS]
        one = _att_one_line(mail)
        if one:
            item["attachmentSummary"] = one
    if analysis.get("tags"):
        item["tags"] = analysis["tags"]
    # 3단 분석 재료·AI 생략 표시 (v1.2.11)
    for key in ("headline", "mainDept", "deptBasis", "ourDeptAction", "myAction", "aiSkipReason"):
        if analysis.get(key):
            item[key] = analysis[key]
    if analysis.get("aiSkipped"):
        item["aiSkipped"] = True
        item["replyDrafts"] = {}
    if analysis.get("analysisBody"):
        item["analysisBody"] = str(analysis["analysisBody"])[:4000]
    body = clean_body(mail.get("bodyText") or "")
    if body:
        item["bodyText"] = body[:2500]
    if item["replyStatus"] == "needs_reply":
        # 실무형 3안(📎 자료 송부·✅ 조치 결과·👉 담당자 전달) — AI 초안이 없으면 기본값으로 채운다 (v1.2.8)
        item["replyDrafts"] = normalize_reply_drafts(analysis.get("replyDrafts"), item)
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


def dedup_thread(thread: list) -> list:
    """스레드 줄에서 같은 메일(id)·같은 (분 단위 시각, 발신자, 제목) 중복을 1건으로 병합한다 (v1.2.18).

    같은 메일이 동기화마다 다시 병합되면 같은 줄이 12번씩 쌓였다(2026-11-24 사용자 신고).
    반환값은 시간순으로 정렬된 새 목록이다.
    """
    out: list[dict] = []
    by_id: dict[str, dict] = {}
    by_key: dict[tuple[str, str, str], dict] = {}
    for entry in thread or []:
        if not isinstance(entry, dict):
            continue
        rec = dict(entry)
        eid = str(rec.get("id") or "").strip()
        key = (str(rec.get("date") or "").replace("T", " ").strip()[:16],
               re.sub(r"\s+", " ", str(rec.get("from") or "")).strip(),
               _norm_title(str(rec.get("gist") or "")))
        prev = (by_id.get(eid) if eid else None) or by_key.get(key)
        if prev is not None:
            if not prev.get("link") and rec.get("link"):
                prev["link"] = rec["link"]
            if len(str(rec.get("gist") or "")) > len(str(prev.get("gist") or "")):
                prev["gist"] = rec["gist"]
            if eid:
                by_id[eid] = prev
            by_key.setdefault(key, prev)
            continue
        if eid:
            by_id[eid] = rec
        by_key[key] = rec
        out.append(rec)
    out.sort(key=lambda m: str(m.get("date") or ""))
    return out


def dedup_all_threads(items: list[dict]) -> int:
    """이미 쌓인 히스토리 중복을 정리한다 — 동기화마다 1회 (v1.2.18). 정리한 줄 수를 돌려준다."""
    merged = 0
    for it in items or []:
        thr = it.get("thread")
        if not isinstance(thr, list) or len(thr) < 2:
            continue
        clean = dedup_thread(thr)
        if len(clean) >= len(thr):
            continue
        merged += len(thr) - len(clean)
        it["thread"] = clean
        last = max([str(m.get("date") or "") for m in clean] + [str(it.get("receivedAt") or "")])[:16]
        if last:
            it["lastAt"] = last
    return merged


def merge_into_thread(item: dict, new_item: dict) -> None:
    """새 메일을 기존 항목의 스레드에 덧붙이고, 최신 메일 기준으로 앞면을 갱신한다."""
    entry = dict(new_item["thread"][0])
    item.setdefault("thread", []).append(entry)
    item["thread"] = dedup_thread(item["thread"])
    item["lastAt"] = max([str(m.get("date") or "") for m in item["thread"]] + [item.get("receivedAt") or ""])[:16]
    # v1.2.14 — 최신 메일이 나를 수신(To)으로 지정했으면 항목의 수신 구분도 따라 올린다
    #           (옛 코드는 등급만 올려 ‘수신인(To)’ 배지와 등급이 어긋났다)
    if new_item.get("recipientRole") == "To":
        item["recipientRole"] = "To"
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
    # 3단 정밀 분석이 경량/생략 요약으로 덮이지 않게 한다(v1.2.11)
    new_sum, old_sum = new_item.get("summary") or "", item.get("summary") or ""
    if new_sum and (is_detailed_summary(new_sum) or not is_detailed_summary(old_sum)):
        item["summary"] = new_sum
    for key in ("headline", "mainDept", "deptBasis", "ourDeptAction", "myAction"):
        if new_item.get(key):
            item[key] = new_item[key]
    if is_detailed_summary(item.get("summary")) and not new_item.get("aiSkipped"):
        item.pop("aiSkipped", None)
        item.pop("aiSkipReason", None)
    elif new_item.get("aiSkipped"):
        # 이번 회신이 기록 보관/타 부서 참조로 건너뛴 것이면 그 표시도 함께 옮긴다(화면의 [✨ AI 요약] 버튼)
        item["aiSkipped"] = True
        item["aiSkipReason"] = new_item.get("aiSkipReason") or "simple_share"
    if new_item.get("actionItems"):
        item["actionItems"] = new_item["actionItems"]
    if new_item.get("attachments"):
        item["attachments"] = new_item["attachments"]
    if new_item.get("bodyText"):
        item["bodyText"] = new_item["bodyText"]
    if new_item.get("images"):
        item["images"] = new_item["images"]
        item["imageCount"] = len(new_item["images"])


def refresh_attachment_meta(nm, items: list[dict], *, limit: int = 4) -> int:
    """예전 수집분의 첨부 목록을 다시 읽어 이름 없는 빈 항목·주소 없는 사진을 고친다(한 번에 limit 건).

    v1.2.17 — 사진 첨부 썸네일은 첨부 url 이 있어야 포털이 중계할 수 있다. 새로 오는 메일은
    parse_read 가 url 까지 담지만, 이미 쌓인 메일은 다시 읽지 않으면 옛 기록이 그대로 남는다.
    사진 첨부가 있거나 빈 항목이 섞인 최근 메일만, 실행마다 몇 건씩 조용히 갱신한다.
    """
    if limit <= 0:
        return 0
    cands: list[tuple[str, int, dict]] = []
    for it in items:
        atts = it.get("attachments") or []
        if not atts:
            continue
        needs = any(not str(a.get("name") or "").strip() or not str(a.get("url") or "").strip() for a in atts)
        if not needs:
            continue
        if not any(_att_image_name(a.get("name") or "") or not str(a.get("name") or "").strip() for a in atts):
            continue                      # 사진이 아닌 첨부는 주소가 없어도 화면에 지장이 없다
        m = re.fullmatch(r"[ms]-(\d+)", str(it.get("id") or ""))
        if not m:
            continue
        cands.append((str(it.get("lastAt") or it.get("receivedAt") or ""), int(m.group(1)), it))
    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    fixed = 0
    for _when, mf_no, it in cands[:limit]:
        box = it.get("mailbox") or "inbox"
        try:
            detail = nm.read_mail(box, mf_no)
        except Exception:  # noqa: BLE001 - 한 건 실패가 동기화를 막지 않는다
            continue
        new = detail.get("attachments") or []
        if new and new != it.get("attachments"):
            it["attachments"] = new
            fixed += 1
    return fixed


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
    target["thread"] = dedup_thread(target["thread"])
    target["lastAt"] = max([str(m.get("date") or "") for m in target["thread"]] + [target.get("receivedAt") or ""])[:16]
    return target


# ────────────────────────────────────────────────────────────── 동기화 본체
def max_mf_no(data: dict) -> int:
    """이미 받아 둔 가장 큰 메일 번호 — 스레드에 병합된 회신·발신 번호까지 본다 (v1.2.18).

    예전에는 항목(id)만 보아, 스레드에 병합된 회신은 기준선을 올리지 못했다.
    그래서 같은 메일을 동기화마다 새 메일로 다시 읽어 히스토리에 12줄씩 쌓였다.
    """
    best = 0

    def bump(value: Any) -> None:
        nonlocal best
        m = re.fullmatch(r"[msa]-(\d+)", str(value or ""))
        if m:
            best = max(best, int(m.group(1)))

    for it in data.get("items") or []:
        bump(it.get("id"))
        for entry in it.get("thread") or []:
            if isinstance(entry, dict):
                bump(entry.get("id"))
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

    drafts_refreshed = refresh_reply_drafts(items)   # 옛 답장 초안 정리 (v1.2.8)
    # v1.2.14 — 새 메일이 없어도, 수신(To)인데 🟡로 남은 항목은 규칙만 다시 적용해 바로잡는다
    to_repaired = repair_to_priority(items)
    if to_repaired:
        report["toRepaired"] = to_repaired
    # v1.2.18 — 새 메일이 없어도 ① 쌓인 히스토리 중복 ② 웹메일 메뉴가 첨부로 붙은 기록을 정리한다
    # v1.2.21 — 예전 수집분도 본문 인용 스레드를 히스토리에 채운다(새 메일이 없어도 1회)
    hist_enriched = enrich_threads_from_body(items)
    hist_merged = dedup_all_threads(items) + hist_enriched
    att_cleaned = clean_attachments(items)
    if hist_enriched:
        report["historyEnriched"] = hist_enriched
    if hist_merged:
        report["historyMerged"] = hist_merged
    if att_cleaned:
        report["attachmentsCleaned"] = att_cleaned
    if (not inbox and not sent and not is_new and not contacts_added and not inferred
            and not drafts_refreshed and not to_repaired and not hist_merged and not att_cleaned):
        report["changed"] = False
        return report

    gem = Gemini(args.gemini_key, args.gemini_model) if args.gemini_key else None

    thread_cache = data.setdefault("thread_cache", {})

    def analyze(mail: dict, kind: str) -> dict:
        # v1.2.21 — 기록 보관만 AI 생략, 나머지는 3단 정밀 분석(스레드 증분) · 첨부 핵심 내용 포함
        return analyze_mail(gem, mail, kind, contacts, thread_cache=thread_cache, stats=report, fetcher=nm)

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
    # v1.2.17 — 사진 첨부 썸네일: 이름 빈 칩·주소 없는 사진 첨부를 몇 건씩 다시 읽어 고친다
    att_fixed = refresh_attachment_meta(nm, items, limit=int(getattr(args, "attach_refresh", 4) or 0))
    if att_fixed:
        report["attachmentsRefreshed"] = att_fixed
    if refreshed:
        report["summariesRefreshed"] = refreshed
    if drafts_refreshed:                      # 옛 답장 초안 3종 → 실무형 3안 (v1.2.8)
        report["draftsRefreshed"] = drafts_refreshed
    changed = bool(refreshed or drafts_refreshed or added or report["replied"] or contacts_added or inferred
                   or to_repaired or att_fixed or hist_merged or att_cleaned
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
    ap.add_argument("--attach-refresh", dest="attach_refresh", type=int, default=4,
                    help="예전 메일의 첨부 목록을 한 실행에 몇 건까지 다시 읽을지(기본 4, 0=끔)")
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
