#!/usr/bin/env python3
"""🔗 유사 사례 · ✨ AI 정밀 분석 섹션 렌더 점검.

caseSectionHtml / caseAiBodyHtml / caseAiRowsHtml / caseAiNoteHtml 과 유사도 표시 헬퍼를
뽑아 quickjs 로 돌린다. % 배지·게이지 바·점수대 색·0~100 클램프까지 확인한다.
실행: python3 check_case_ai.py [index.html]
"""
from __future__ import annotations

import json
import re
import sys

import quickjs

from check_rules_list import extract_fn

PATH = sys.argv[1] if len(sys.argv) > 1 else "index.html"

STUBS = r"""
var PORTAL = true;
var CASE_AI = new Map();
var CASE_AI_TIERS = [
  { min: 85, key: "hi", label: "매우 유사" },
  { min: 65, key: "mid", label: "관련 높음" },
  { min: 0, key: "lo", label: "참고" },
];
var ITEMS = [];
for (var i = 0; i < 812; i++) ITEMS.push({ id: "m-" + i, title: "메일 " + i });
function items() { return ITEMS; }
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function caseShortDate(s) { return String(s || "").slice(5, 10).replace("-", "/"); }
function caseKwLabel(w) { return String(w).toUpperCase(); }
"""

CHECK = r"""
var out = {};
CASE_AI.set("m-1", { state: "loading", rows: [], message: "" });
out.loadingBtn = caseSectionHtml({ id: "m-1", title: "t", thread: [] }, []).indexOf("AI가 맥락을 분석 중") >= 0;
out.loadingDisabled = caseSectionHtml({ id: "m-1", title: "t", thread: [] }, []).indexOf("disabled") >= 0;

/* 92 = 매우 유사(초록) · 72 = 관련 높음(파랑) · 58 = 참고(주황) */
CASE_AI.set("m-2", { state: "done", cached: true, rows: [
  { id: "m-9", score: 92, reason: "같은 PTR 막차 납입 건으로 이전 합의 확인", date: "2025-03-04 10:00", title: "PTR 납입 일정", from: "이방형 부장 (GM)" },
  { id: "m-8", score: 72, reason: "동일 품번 결품 이력", date: "2024-11-20 09:00", title: "결품 대응", from: "엄태동 상무" },
  { id: "m-7", score: 58, reason: "같은 라인 출하 문의", date: "2024-09-01 08:30", title: "출하 문의", from: "김기경 과장" }
] });
var h2 = caseSectionHtml({ id: "m-2", title: "t", thread: [] }, []);
out.doneRows = (h2.match(/class="case-row"/g) || []).length;
out.hasBadge = h2.indexOf('<span class="case-score hi"><b>92</b><span class="pc">%</span></span>') >= 0;
out.hasGauge = h2.indexOf('<span class="case-gauge hi" role="img"') >= 0 && h2.indexOf('style="width:92%"') >= 0;
out.gaugeLabel = h2.indexOf("원문 대비 유사도 92퍼센트 · 매우 유사") >= 0;
out.tierHi = h2.indexOf('<span class="case-lv hi">매우 유사</span>') >= 0;
out.tierMid = h2.indexOf('<span class="case-lv mid">관련 높음</span>') >= 0 && h2.indexOf('<span class="case-gauge mid"') >= 0;
out.tierLo = h2.indexOf('<span class="case-lv lo">참고</span>') >= 0 && h2.indexOf('<span class="case-gauge lo"') >= 0;
out.midWidth = h2.indexOf('style="width:72%"') >= 0;
out.loWidth = h2.indexOf('style="width:58%"') >= 0;
out.hasReason = h2.indexOf("같은 PTR 막차 납입 건으로 이전 합의 확인") >= 0;
out.cachedNote = h2.indexOf("저장된 결과(추가 호출 없음)") >= 0;
out.foundLabel = h2.indexOf("3건 · AI 선별") >= 0;

/* 범위를 벗어난 점수·문자열 점수도 0~100 안으로 가둔다 */
CASE_AI.set("m-6", { state: "done", cached: false, rows: [
  { id: "m-9", score: 140, reason: "상한", date: "2025-01-01 09:00", title: "위", from: "A" },
  { id: "m-8", score: -3, reason: "하한", date: "2025-01-02 09:00", title: "아래", from: "B" },
  { id: "m-7", score: "88", reason: "문자열", date: "2025-01-03 09:00", title: "문자", from: "C" }
] });
var h6 = caseSectionHtml({ id: "m-6", title: "t", thread: [] }, []);
out.clampHigh = h6.indexOf('<b>100</b>') >= 0 && h6.indexOf('style="width:100%"') >= 0;
out.clampLow = h6.indexOf('<b>0</b>') >= 0 && h6.indexOf('style="width:0%"') >= 0;
out.stringScore = h6.indexOf('<b>88</b>') >= 0 && h6.indexOf('style="width:88%"') >= 0 && h6.indexOf('case-score hi') >= 0;

/* 점수가 아예 없으면 게이지 없이 기존 ✨ AI 선별 표시로 물러난다 */
CASE_AI.set("m-7", { state: "done", cached: false, rows: [
  { id: "m-6", reason: "근거만 있음", date: "2025-02-02 09:00", title: "점수 없음", from: "D" }
] });
var h7 = caseSectionHtml({ id: "m-7", title: "t", thread: [] }, []);
out.noScoreTag = h7.indexOf('class="case-tag ai">✨ AI 선별') >= 0 && h7.indexOf("case-gauge") < 0;
out.noScoreReason = h7.indexOf("근거만 있음") >= 0;

CASE_AI.set("m-3", { state: "done", cached: false, rows: [] });
var h3 = caseSectionHtml({ id: "m-3", title: "t", thread: [] }, []);
out.zero = h3.indexOf("0건 — 맥락상 유사한 과거 사례 없음") >= 0;
out.zeroLabel = h3.indexOf("AI 선별 0건") >= 0;

CASE_AI.set("m-4", { state: "error", rows: [], message: "Gemini API 키가 설정되지 않아 AI 정밀 분석을 쓸 수 없습니다" });
var h4 = caseSectionHtml({ id: "m-4", title: "t", thread: [] }, []);
out.errMsg = h4.indexOf("GEMINI") < 0 && h4.indexOf("Gemini API 키가 설정되지 않아") >= 0;
out.errWarn = h4.indexOf("case-warn") >= 0 && h4.indexOf("아래 기본 목록을 먼저 보여드립니다") >= 0;
out.errRetry = h4.indexOf("다시 시도") >= 0 && h4.indexOf('data-case-ai="m-4"') >= 0;
out.errEmptyKeepsNote = h4.indexOf("품번·핵심 키워드가 겹치는 과거 메일이 없습니다") >= 0;

/* AI 가 한도(429)로 실패해도 기존 기본 유사 사례 목록은 그대로 남는다 */
var errRows = [
  { id: "m-11", sc: 3.2, date: "2025-01-02 09:00", title: "기본 유사 1", from: "엄태동 상무", codes: ["5418201"], kws: ["PTR"] },
  { id: "m-12", sc: 2.1, date: "2025-01-03 09:00", title: "기본 유사 2", from: "최수철 부장", codes: [], kws: ["재고"] }
];
CASE_AI.set("m-10", { state: "error", rows: [], message: "요청 한도(429)를 넘었습니다. 잠시 후 다시 시도해 주세요." });
var h10 = caseSectionHtml({ id: "m-10", title: "t", thread: [] }, errRows);
out.errKeepsRows = (h10.match(/class="case-row"/g) || []).length;
out.errKeepsRowsTitle = h10.indexOf("기본 유사 1") >= 0 && h10.indexOf("기본 유사 2") >= 0;
out.errWarnKeepsRows = h10.indexOf("case-warn") >= 0 && h10.indexOf("요청 한도(429)") >= 0 && h10.indexOf("다시 시도") >= 0;
out.errHeaderCount = h10.indexOf("2건") >= 0;

/* AI 를 아직 안 눌렀으면 기존 로컬 후보 목록이 그대로 나온다 */
var rows = [{ id: "m-5", sc: 3.2, date: "2025-01-02 09:00", title: "기존 유사", from: "엄태동 상무", codes: ["5418201"], kws: ["PTR"] }];
var h5 = caseSectionHtml({ id: "m-5", title: "t", thread: [] }, rows);
out.localRows = (h5.match(/class="case-row"/g) || []).length;
out.hasAiBtn = h5.indexOf("AI 정밀 분석") >= 0;

/* 포털 모드가 아니면(GitHub Pages 판) AI 버튼은 나오지 않는다 */
PORTAL = false;
var h8 = caseSectionHtml({ id: "m-5", title: "t", thread: [] }, rows);
out.hiddenOutsidePortal = h8.indexOf("AI 정밀 분석") < 0;
JSON.stringify(out);
"""

CSS_CHECKS = [
    ("점수대 초록(#16a34a 계열)", ".case-gauge.hi i{background:linear-gradient(90deg,#4ade80,#16a34a)}"),
    ("점수대 파랑(#2563eb 계열)", ".case-gauge.mid i{background:linear-gradient(90deg,#60a5fa,#2563eb)}"),
    ("점수대 주황(#d97706 계열)", ".case-gauge.lo i{background:linear-gradient(90deg,#fbbf24,#d97706)}"),
    ("큰 % 배지 크기", ".case-score b{font-size:15.5px"),
    ("게이지 라운드 바", ".case-gauge{display:block; height:5px"),
    ("게이지 채움 애니메이션", "@keyframes casebar{from{transform:scaleX(0)}"),
    ("움직임 줄이기 대응", ".case-gauge i,\n    .badge.pop"),
]


def main() -> int:
    html = open(PATH, encoding="utf-8").read()
    src = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", html, re.S)[-1]
    ctx = quickjs.Context()
    ctx.eval(STUBS)
    for name in ("caseAiScore", "caseAiTier", "caseAiScoreHtml", "caseAiGaugeHtml",
                 "caseAiRowsHtml", "caseAiBodyHtml", "caseAiNoteHtml", "caseSectionHtml",
                 "caseLocalRowsHtml"):
        ctx.eval(extract_fn(src, name))
    res = json.loads(ctx.eval(CHECK))
    ok = True

    def check(name, cond, got):
        nonlocal ok
        print(f"  {'OK ' if cond else 'NG '} {name} — {got}")
        ok = ok and bool(cond)

    print("[🔗 유사 사례 · ✨ AI 정밀 분석]")
    check("로딩 표시", res["loadingBtn"], res["loadingBtn"])
    check("로딩 중 버튼 잠금", res["loadingDisabled"], res["loadingDisabled"])
    check("AI 결과 3건 카드", res["doneRows"] == 3, res["doneRows"])
    check("큰 % 배지(92% 유사)", res["hasBadge"], res["hasBadge"])
    check("게이지 바 폭 score%", res["hasGauge"], res["hasGauge"])
    check("게이지 접근성 라벨", res["gaugeLabel"], res["gaugeLabel"])
    check("85↑ 매우 유사(hi)", res["tierHi"], res["tierHi"])
    check("65~84 관련 높음(mid)", res["tierMid"], res["tierMid"])
    check("65↓ 참고(lo)", res["tierLo"], res["tierLo"])
    check("중간 점수 게이지 폭", res["midWidth"], res["midWidth"])
    check("낮은 점수 게이지 폭", res["loWidth"], res["loWidth"])
    check("reason 한 줄", res["hasReason"], res["hasReason"])
    check("저장된 결과 표시", res["cachedNote"], res["cachedNote"])
    check("헤더 '3건 · AI 선별'", res["foundLabel"], res["foundLabel"])
    check("상한 클램프 100", res["clampHigh"], res["clampHigh"])
    check("하한 클램프 0", res["clampLow"], res["clampLow"])
    check("문자열 점수 처리", res["stringScore"], res["stringScore"])
    check("점수 없음 → AI 선별 표시", res["noScoreTag"], res["noScoreTag"])
    check("점수 없음 → reason 유지", res["noScoreReason"], res["noScoreReason"])
    check("0건 안내", res["zero"], res["zero"])
    check("0건 헤더 표기", res["zeroLabel"], res["zeroLabel"])
    check("키 없음 안내 문구", res["errMsg"], res["errMsg"])
    check("AI 실패 경고 배너", res["errWarn"], res["errWarn"])
    check("AI 실패 시 다시 시도 버튼", res["errRetry"], res["errRetry"])
    check("AI 실패 + 기본 목록 없음 안내", res["errEmptyKeepsNote"], res["errEmptyKeepsNote"])
    check("AI 실패해도 기본 목록 유지(2건)", res["errKeepsRows"] == 2, res["errKeepsRows"])
    check("AI 실패해도 기본 목록 내용", res["errKeepsRowsTitle"], res["errKeepsRowsTitle"])
    check("AI 실패 경고 + 목록 함께", res["errWarnKeepsRows"], res["errWarnKeepsRows"])
    check("AI 실패 시 헤더 건수 유지", res["errHeaderCount"], res["errHeaderCount"])
    check("AI 미실행 시 기존 목록 유지", res["localRows"] == 1 and res["hasAiBtn"], (res["localRows"], res["hasAiBtn"]))
    check("비포털 모드에서는 버튼 숨김", res["hiddenOutsidePortal"], res["hiddenOutsidePortal"])
    for name, needle in CSS_CHECKS:
        check(name, needle in html, needle in html)
    print("결과:", "통과" if ok else "실패")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
