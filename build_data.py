#!/usr/bin/env python3
"""data.json -> data.enc.json (PBKDF2-SHA256 100,000회 + AES-256-GCM)

사용:
    MAILBOARD_PIN=**** python3 build_data.py
    python3 build_data.py --pin ****
    python3 build_data.py --in data.json --out data.enc.json --iterations 200000
    python3 build_data.py --no-compress      # 압축 없이 예전 형식(v1)으로 만들기

경량화(2026-11-22): 암호문 안쪽 평문을 gzip 으로 한 번 더 압축한다(v2 · comp=gzip).
  AES-GCM 암호문은 압축률이 0이라 그 전에 평문을 압축해야 전송량이 줄고, 브라우저는
  복호화 직후 DecompressionStream('gzip') 으로 풀어서 그대로 JSON.parse 한다.
  index.html 이 DecompressionStream 을 지원하지 않는 상태(예전 배포본)라면 자동으로
  압축하지 않고 v1 으로 만든다 — 배포본과 데이터 형식이 어긋나 로그인이 막히지 않게.

PIN은 인자나 환경변수(MAILBOARD_PIN)로만 받는다. 소스에 적어 두지 않는다.
data.json · make_data.py 는 절대 GitHub 에 올리지 않는다(.gitignore). 암호문(data.enc.json)만 배포한다.
"""
import argparse
import base64
import gzip
import json
import os
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

DEFAULT_ITERATIONS = 100_000


def derive(pin: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(pin.encode("utf-8"))


def page_supports_gzip() -> bool:
    """배포되는 index.html 이 gzip 압축 해제를 지원하는지 확인한다.

    publish.sh 는 같은 작업 폴더의 index.html 과 data.enc.json 을 함께 올리므로,
    이 검사만 통과하면 라이브 페이지와 데이터 형식이 항상 짝이 맞는다.
    """
    page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    try:
        with open(page, encoding="utf-8") as f:
            src = f.read()
        return "gunzipBytes" in src and "DecompressionStream" in src
    except OSError:
        return False


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Encrypt data.json into data.enc.json")
    ap.add_argument("--pin", default=os.environ.get("MAILBOARD_PIN"))
    ap.add_argument("--in", dest="src", default=os.path.join(here, "data.json"))
    ap.add_argument("--out", dest="dst", default=os.path.join(here, "data.enc.json"))
    ap.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    ap.add_argument("--no-compress", action="store_true", help="gzip 압축 없이 v1 형식으로 만든다")
    args = ap.parse_args()

    if not args.pin:
        print("PIN이 필요합니다: --pin <PIN> 또는 MAILBOARD_PIN 환경변수", file=sys.stderr)
        return 2

    with open(args.src, encoding="utf-8") as f:
        payload = json.load(f)

    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    use_gzip = (not args.no_compress) and page_supports_gzip()
    if use_gzip:
        packed = gzip.compress(plaintext, 9, mtime=0)
    else:
        packed = plaintext

    salt = os.urandom(16)
    iv = os.urandom(12)
    key = derive(args.pin, salt, args.iterations)
    ciphertext = AESGCM(key).encrypt(iv, packed, None)

    out = {
        "v": 2 if use_gzip else 1,
        "kdf": "PBKDF2-SHA256",
        "iterations": args.iterations,
        "cipher": "AES-256-GCM",
        "comp": "gzip" if use_gzip else "none",
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "plainBytes": len(plaintext),
        "updatedAt": payload.get("updatedAt"),
    }
    with open(args.dst, "w", encoding="utf-8") as f:
        # 배포 파일은 한 줄로 — 들여쓰기 공백만 수백 KB 다
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    ratio = (len(packed) / len(plaintext) * 100) if plaintext else 0
    print(f"OK  {os.path.basename(args.src)} ({len(plaintext):,} bytes) "
          f"-> {'gzip ' if use_gzip else 'raw '}{len(packed):,} bytes ({ratio:.1f}%) "
          f"-> {os.path.basename(args.dst)} ({os.path.getsize(args.dst):,} bytes, "
          f"PBKDF2 {args.iterations:,}회)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
