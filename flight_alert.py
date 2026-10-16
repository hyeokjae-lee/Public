"""
EY ICN 운항편 이메일 알림
- 매일 10:00 / 14:00 (KST) 요약
- 출발/도착 2시간 전 알림 (편당 1회)
- 지연(예정시각 15분 이상 변경), 게이트 변경, 결항/회항 시 즉시 알림
데이터: 공공데이터포털 '인천국제공항공사_여객편 운항현황(다국어)'
"""
import json
import os
import smtplib
import sys
import datetime as dt
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")

# 편명: (구분, 구간)
FLIGHTS = {
    "EY822": ("ARR", "AUH→ICN"),
    "EY823": ("DEP", "ICN→AUH"),
    "EY826": ("ARR", "AUH→ICN"),
    "EY827": ("DEP", "ICN→AUH"),
}
AIRLINE = "EY"
BASE = "https://apis.data.go.kr/B551177/StatusOfPassengerFlightsDeOdp"
STATE_FILE = "state.json"

SUMMARY_TIMES = [(10, 0), (14, 0)]   # 요약 시각 (KST)
SUMMARY_GRACE_MIN = 180              # 요약 시각 이후 이 시간 안에만 발송
PRE_ALERT_MIN = 120                  # 출도착 몇 분 전부터 사전 알림
DELAY_THRESHOLD_MIN = 15             # 예정시각이 이만큼 바뀌면 알림
ALERT_REMARKS = ("지연", "결항", "회항", "DELAY", "CANCEL", "DIVERT")

DRY_RUN = os.environ.get("DRY_RUN") == "1"


# ---------------- 데이터 조회 ----------------
def fetch(kind):
    op = "getPassengerArrivalsDeOdp" if kind == "ARR" else "getPassengerDeparturesDeOdp"
    params = {
        "serviceKey": os.environ["SERVICE_KEY"],
        "airline": AIRLINE,
        "from_time": "0000",
        "to_time": "2400",
        "lang": "K",
        "type": "json",
    }
    r = requests.get(f"{BASE}/{op}", params=params, timeout=30)
    r.raise_for_status()
    body = r.json()["response"]["body"]
    items = body.get("items") or []
    if isinstance(items, dict):
        items = items.get("item", [])
    if isinstance(items, dict):
        items = [items]
    return items


def parse_time(s, today):
    """'2215' 또는 '202610162215' 형식 모두 처리"""
    if not s:
        return None
    s = str(s).strip()
    try:
        if len(s) >= 12:
            return dt.datetime.strptime(s[:12], "%Y%m%d%H%M").replace(tzinfo=KST)
        if len(s) == 4:
            t = dt.datetime.strptime(s, "%H%M").time()
            return dt.datetime.combine(today, t, tzinfo=KST)
    except ValueError:
        pass
    return None


def collect(now, fetcher=fetch):
    raw = {"ARR": fetcher("ARR"), "DEP": fetcher("DEP")}
    result = {}
    for code, (kind, route) in FLIGHTS.items():
        for it in raw[kind]:
            if str(it.get("flightId", "")).replace(" ", "") != code:
                continue
            sched = parse_time(it.get("scheduleDateTime"), now.date())
            est = parse_time(it.get("estimatedDateTime"), now.date()) or sched
            if not sched:
                continue
            key = f"{code}|{sched:%Y%m%d%H%M}"
            result[key] = {
                "code": code, "kind": kind, "route": route,
                "sched": sched.isoformat(), "est": est.isoformat(),
                "gate": str(it.get("gatenumber") or "").strip(),
                "remark": str(it.get("remark") or "").strip(),
                "terminal": str(it.get("terminalid") or "").strip(),
                "carousel": str(it.get("carousel") or "").strip(),
                "exit": str(it.get("exitnumber") or "").strip(),
                "checkin": str(it.get("chkinrange") or "").strip(),
            }
    return result


# ---------------- 표시 ----------------
def hm(iso):
    t = dt.datetime.fromisoformat(iso)
    today = dt.datetime.now(KST).date()
    suffix = " (+1)" if t.date() > today else ""
    return t.strftime("%H:%M") + suffix


def line(f):
    kind = "도착" if f["kind"] == "ARR" else "출발"
    t = hm(f["sched"])
    if f["est"] != f["sched"]:
        t += f" → {hm(f['est'])}"
    parts = [f"{f['code']} {f['route']} {kind} {t}"]
    if f["terminal"]:
        parts.append(f["terminal"])
    if f["kind"] == "ARR":
        if f["gate"]:
            parts.append(f"게이트 {f['gate']}")
        if f["carousel"]:
            parts.append(f"수하물 {f['carousel']}")
        if f["exit"]:
            parts.append(f"출구 {f['exit']}")
    else:
        if f["checkin"]:
            parts.append(f"카운터 {f['checkin']}")
        if f["gate"]:
            parts.append(f"게이트 {f['gate']}")
    if f["remark"]:
        parts.append(f"[{f['remark']}]")
    return " / ".join(parts)


def summary_text(flights, now):
    lines = [f"EY ICN 운항 현황 ({now:%Y-%m-%d %H:%M} KST 기준)", ""]
    found = {f["code"] for f in flights.values()}
    for f in sorted(flights.values(), key=lambda x: x["sched"]):
        lines.append("• " + line(f))
    for code in FLIGHTS:
        if code not in found:
            lines.append(f"• {code} {FLIGHTS[code][1]}: 오늘 운항 정보 없음")
    return "\n".join(lines)


# ---------------- 메일 ----------------
def send_mail(subject, body):
    subject = f"[EY ICN] {subject}"
    if DRY_RUN:
        print("=" * 60, "\n", subject, "\n", body, sep="")
        return
    user = os.environ["GMAIL_USER"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = os.environ["MAIL_TO"]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, os.environ["GMAIL_APP_PASSWORD"])
        s.sendmail(user, [a.strip() for a in os.environ["MAIL_TO"].split(",")], msg.as_string())


# ---------------- 상태 ----------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fp:
            return json.load(fp)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"summaries": [], "flights": {}}


def save_state(state, now):
    cutoff = (now - dt.timedelta(days=2)).strftime("%Y%m%d")
    state["flights"] = {k: v for k, v in state["flights"].items() if k.split("|")[1][:8] >= cutoff}
    state["summaries"] = [s for s in state["summaries"] if s.replace("-", "")[:8] >= cutoff]
    with open(STATE_FILE, "w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=1, sort_keys=True)


# ---------------- 메인 로직 ----------------
def run(now=None, fetcher=fetch):
    now = now or dt.datetime.now(KST)
    state = load_state()
    flights = collect(now, fetcher)

    # 1) 변경 알림 (지연 / 게이트 / 결항·회항)
    for key, f in flights.items():
        prev = state["flights"].get(key)
        changes = []
        sched = dt.datetime.fromisoformat(f["sched"])
        est = dt.datetime.fromisoformat(f["est"])
        if prev is None:
            if (est - sched).total_seconds() / 60 >= DELAY_THRESHOLD_MIN:
                changes.append(f"지연: {hm(f['sched'])} → {hm(f['est'])}")
        else:
            pest = dt.datetime.fromisoformat(prev["est"])
            diff = (est - pest).total_seconds() / 60
            if abs(diff) >= DELAY_THRESHOLD_MIN:
                word = "지연" if diff > 0 else "앞당김"
                changes.append(f"시각 변경({word}): {hm(prev['est'])} → {hm(f['est'])}")
            if prev.get("gate") and f["gate"] and prev["gate"] != f["gate"]:
                changes.append(f"게이트 변경: {prev['gate']} → {f['gate']}")
            if f["remark"] != prev.get("remark") and any(w in f["remark"].upper() for w in ALERT_REMARKS):
                changes.append(f"상태 변경: {prev.get('remark') or '-'} → {f['remark']}")
        if changes:
            send_mail(f"{f['code']} " + changes[0].split(":")[0],
                      "\n".join(changes) + "\n\n현재 정보\n• " + line(f))

        # 2) 출도착 2시간 전 알림
        pre_sent = (prev or {}).get("pre_sent", False)
        mins_left = (est - now).total_seconds() / 60
        if not pre_sent and 0 < mins_left <= PRE_ALERT_MIN:
            kind = "도착" if f["kind"] == "ARR" else "출발"
            send_mail(f"{f['code']} {kind} {int(mins_left)}분 전",
                      f"{f['code']} {kind}까지 약 {int(mins_left)}분 남았습니다.\n\n• " + line(f))
            pre_sent = True

        f["pre_sent"] = pre_sent
        state["flights"][key] = f

    # 3) 정기 요약 (10:00, 14:00)
    for h, m in SUMMARY_TIMES:
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        tag = f"{t:%Y-%m-%d %H%M}"
        if t <= now < t + dt.timedelta(minutes=SUMMARY_GRACE_MIN) and tag not in state["summaries"]:
            send_mail(f"{h:02d}:{m:02d} 운항 요약", summary_text(flights, now))
            state["summaries"].append(tag)

    save_state(state, now)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        now = dt.datetime.now(KST)
        send_mail("테스트 메일", summary_text(collect(now), now))
    else:
        run()
