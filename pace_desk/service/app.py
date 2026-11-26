"""PACE 버그 제보 데스크 웹 서비스 (8단계).

실행 (프로젝트 폴더에서):
  .venv/bin/python -m uvicorn app:app --app-dir pace_desk/service --host 127.0.0.1 --port 8010
그다음 브라우저에서 http://127.0.0.1:8010 을 연다.

주소:
  GET  /                              제보 화면
  POST /api/reports                   {"text": 제보 글} → 처리 결과와 대화 번호(session_id)
  POST /api/reports/{id}/followup     {"text": 덧붙인 글} → 처음 글과 합쳐 다시 처리한 결과
  GET  /api/reports/{id}              지금까지의 대화와 마지막 결과
  GET  /api/health                    살아 있는지 확인
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from pipeline import add_info, get_session, start_report

app = FastAPI(title="PACE 버그 제보 데스크")


class ReportIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/reports")
def create_report(body: ReportIn):
    session_id, result = start_report(body.text.strip())
    return {"session_id": session_id, "result": result}


@app.post("/api/reports/{session_id}/followup")
def follow_up(session_id: str, body: ReportIn):
    try:
        result = add_info(session_id, body.text.strip())
    except KeyError:
        raise HTTPException(status_code=404, detail="없는 제보 번호예요. 처음부터 다시 보내 주세요.")
    return {"session_id": session_id, "result": result}


@app.get("/api/reports/{session_id}")
def read_report(session_id: str):
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="없는 제보 번호예요.")
    return session


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE


PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PACE 버그 제보 데스크</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;600;700&display=swap">
<style>
:root { --bg:#f2f5f3; --surface:#fcfcfb; --ink:#14201a; --ink2:#46524b; --muted:#66716a; --line:#d6ddd8;
  --accent:#16774a; --accent-soft:#dfefe6; --warn:#9c5304; --warn-soft:#faead5; --quote:#e9efeb;
  --p4:#104281; --p3:#256abf; --p2:#5598e7; --p1:#86b6ef; }
@media (prefers-color-scheme: dark) { :root { --bg:#111513; --surface:#1a1a19; --ink:#e9efeb; --ink2:#b5bfb8;
  --muted:#919b94; --line:#2d3430; --accent:#4fc08b; --accent-soft:#17301f; --warn:#f0a44c; --warn-soft:#392912;
  --quote:#212723; --p4:#b7d3f6; --p3:#6da7ec; --p2:#2a78d6; --p1:#184f95; color-scheme: dark; } }
body { margin:0; background:var(--bg); color:var(--ink); font-family:"IBM Plex Sans KR","Apple SD Gothic Neo","Malgun Gothic",sans-serif;
  font-size:16px; line-height:1.65; word-break:keep-all; }
.wrap { max-width:820px; margin:0 auto; padding:32px 16px 64px; display:grid; gap:20px; }
h1 { margin:0; font-size:30px; } h2 { margin:0; font-size:18px; }
.lede { color:var(--ink2); margin:4px 0 0; }
.card { background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:18px; display:grid; gap:12px; }
textarea { width:100%; box-sizing:border-box; min-height:110px; padding:12px; border:1px solid var(--line); border-radius:8px;
  background:var(--bg); color:var(--ink); font:inherit; resize:vertical; }
button { font:inherit; font-weight:600; border:0; border-radius:8px; padding:10px 18px; background:var(--accent); color:#fff; cursor:pointer; }
button.ghost { background:var(--quote); color:var(--ink2); font-weight:400; padding:6px 12px; font-size:14px; }
button:disabled { opacity:.5; cursor:wait; }
:focus-visible { outline:3px solid var(--p3); outline-offset:2px; }
.row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
.chip { display:inline-block; padding:2px 12px; border-radius:999px; font-weight:700; background:var(--accent-soft); color:var(--accent); }
.pri { display:inline-flex; gap:8px; align-items:center; font-weight:700; font-size:20px; }
.pri i { width:14px; height:14px; border-radius:3px; display:inline-block; }
.sum { display:flex; flex-wrap:wrap; gap:6px; align-items:center; color:var(--ink2); }
.sum span.t { background:var(--quote); padding:2px 10px; border-radius:6px; }
table { border-collapse:collapse; width:100%; }
td, th { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; width:8em; }
.muted { color:var(--muted); font-size:14px; }
.warn { background:var(--warn-soft); color:var(--warn); border-radius:10px; padding:12px 14px; }
.issue { border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
ul { margin:0; padding-left:1.2em; }
.spin { display:inline-block; width:14px; height:14px; border:2px solid var(--line); border-top-color:var(--accent); border-radius:50%;
  animation:s 0.8s linear infinite; vertical-align:-2px; }
@keyframes s { to { transform:rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation:none; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>PACE 버그 제보 데스크</h1>
    <p class="lede">사용자 제보를 붙여 넣으면 분류하고, 정보를 뽑고, 알려진 이슈와 연결해서 얼마나 급한지 알려 드려요.</p>
  </header>
  <section class="card">
    <label for="report"><h2>제보 글</h2></label>
    <textarea id="report" placeholder="예: 1.2.0 쓰는데 걸음수가 삼성헬스보다 하루에 천보씩 적게 나와요. 남편 폰도 그렇대요. 갤럭시 S23이에요"></textarea>
    <div class="row">
      <button id="send">분석하기</button>
      <span class="muted">예시:</span>
      <button class="ghost" data-ex="1.2.0 쓰는데 걸음수가 삼성헬스보다 하루에 천보씩 적게 나와요. 남편 폰도 그렇대요. 갤럭시 S23이에요">재발 버그</button>
      <button class="ghost" data-ex="사진은 한 번에 몇 장까지, 몇 MB까지 올릴 수 있어요?">문의</button>
      <button class="ghost" data-ex="앱이 자꾸 이상해요">정보 부족</button>
    </div>
    <p class="muted" id="status" role="status"></p>
  </section>
  <div id="result" class="wrap" style="padding:0"></div>
</div>
<script>
const $ = (s) => document.querySelector(s);
let sessionId = null;
const LEVEL = { "긴급": "var(--p4)", "높음": "var(--p3)", "보통": "var(--p2)", "낮음": "var(--p1)" };
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

document.querySelectorAll("[data-ex]").forEach((b) => b.addEventListener("click", () => { $("#report").value = b.dataset.ex; }));
$("#send").addEventListener("click", () => submit("/api/reports", $("#report").value, true));

async function submit(url, text, fresh) {
  if (!text.trim()) { $("#status").textContent = "제보 글을 먼저 적어 주세요."; return; }
  document.querySelectorAll("button").forEach((b) => b.disabled = true);
  $("#status").innerHTML = '<span class="spin"></span> 분석 중이에요. 10초쯤 걸려요.';
  try {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "요청 실패");
    sessionId = body.session_id;
    render(body.result);
    $("#status").textContent = fresh ? "분석을 마쳤어요." : "덧붙인 내용을 합쳐서 다시 분석했어요.";
  } catch (err) {
    $("#status").textContent = "문제가 생겼어요: " + err.message;
  } finally {
    document.querySelectorAll("button").forEach((b) => b.disabled = false);
  }
}

function render(r) {
  if (!r.ok) { $("#result").innerHTML = `<section class="card warn">${esc(r.error)}</section>`; return; }
  const e = r.extract, s = r.scores;
  const pri = s ? `<div class="pri"><i style="background:${LEVEL[s.priority_label]}"></i>${esc(s.priority_label)} <span class="muted">(${s.priority}점)</span></div>
      <div class="sum"><span class="t">심각도 ${s.severity}</span>+<span class="t">영향 범위 ${s.impact}</span>+<span class="t">해결 상태 ${s.resolution}</span>=<b>${s.priority}</b></div>
      <p class="muted">심각도는 Haiku가 매기고, 영향 범위(제보한 사람 수)와 해결 상태(새 문제·고치는 중·이미 고침)는 기록부를 보고 코드가 계산해요.</p>`
    : `<p class="muted">무엇에 대한 글인지 알 수 없어서 점수를 매기지 않았어요.</p>`;
  const issues = r.linked_issues.length ? r.linked_issues.map((i) => `<div class="issue"><b>${esc(i.id)}</b> ${esc(i.title)}
      <div class="muted">상태: ${esc(i.status)}${i.fixed_in.length ? " · 고친 버전: " + esc(i.fixed_in.join(", ")) : ""} · 지금까지 ${i.report_count}명 제보</div></div>`).join("")
    : `<p class="muted">같은 이슈가 기록부에 없어요.</p>`;
  const missing = r.missing.length ? `<section class="card"><h2>더 필요한 정보</h2><ul>${r.missing.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>
      <label for="more" class="muted">사용자에게 받은 답을 적으면, 처음 글과 합쳐서 다시 분석해요.</label>
      <textarea id="more" placeholder="예: 앱이고 버전은 1.2.0이에요. 홈 화면에서 걸음 수를 보면 그래요"></textarea>
      <div class="row"><button id="addmore">덧붙여서 다시 분석</button></div></section>` : "";
  const calls = r.calls.map((c) => `${esc(c.step)} ${(c.latency_ms / 1000).toFixed(1)}초${c.from_cache ? "(저장된 답)" : ""}${c.tool_calls.length ? " · 도구 " + c.tool_calls.map((t) => esc(t.name)).join(", ") : ""}`).join(" · ");
  $("#result").innerHTML = `
    <section class="card"><h2>분류</h2><div class="row"><span class="chip">${esc(r.category)}</span></div><p>${esc(r.reason)}</p></section>
    <section class="card"><h2>우선순위</h2>${pri}</section>
    <section class="card"><h2>연결된 이슈</h2>${issues}${r.guidance ? `<p><b>안내:</b> ${esc(r.guidance)}</p>` : ""}</section>
    <section class="card"><h2>뽑은 정보</h2><table>
      <tr><th>플랫폼</th><td>${esc(e.platform ?? "모름")}</td></tr>
      <tr><th>기기</th><td>${esc(e.env_detail ?? "없음")}</td></tr>
      <tr><th>앱 버전</th><td>${esc(e.app_version ?? "없음")}</td></tr>
      <tr><th>재현 절차</th><td>${esc(e.steps ?? "없음")}</td></tr>
      ${r.completeness !== null ? `<tr><th>정보 완성도</th><td>${r.completeness} / 2</td></tr>` : ""}
    </table></section>
    ${missing}
    <p class="muted">처리 ${r.elapsed_sec}초 · 비용(정가 환산) 약 ${r.cost_usd.toFixed(4)}달러 · ${calls}</p>`;
  const more = $("#addmore");
  if (more) more.addEventListener("click", () => submit(`/api/reports/${sessionId}/followup`, $("#more").value, false));
}
</script>
</body>
</html>"""
