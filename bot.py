#!/usr/bin/env python3
"""텔레그램 봇으로 조작하는 KTX 매크로 상주 프로세스.

서버에 이 프로세스 하나만 띄워두고, 휴대폰의 텔레그램에서 구간을 등록·취소한다.
웹 UI 와 달리 포트를 열지 않으므로 인증·HTTPS 설정이 필요 없다.

코레일 로그인은 **한 번만** 하고, 등록된 구간들을 한 세션에서 번갈아 조회한다.
같은 계정으로 여러 프로세스가 로그인하면 세션이 서로 밀리기 때문이다.

실행: python bot.py
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import ktx_macro
from ktx_macro import (
    ERROR_ALERT_THRESHOLD,
    KTXMacro,
    format_deadline,
    format_passengers,
    format_train,
    has_assigned_seat,
    load_credentials,
    logger,
    parse_time,
    send_telegram,
    telegram_config,
)
from korail_mobile_api import KorailPassengerCounts

# 컨테이너에서는 KTX_JOBS_FILE 로 볼륨 안 경로를 준다. 없으면 스크립트 옆에 둔다.
JOBS_FILE = Path(os.getenv("KTX_JOBS_FILE") or Path(__file__).with_name("jobs.json"))
DEFAULT_INTERVAL = 8
# 등록 직후 오타(잘못된 역 이름 등)를 알려주되, 일시적 네트워크 오류로는 떠들지 않는다.
JOB_ERROR_THRESHOLD = 3

HELP = """🚄 KTX 매크로 봇

/add 출발역 도착역 날짜 시작 종료 [도착기한]
  예) /add 광명 목포 20260923 18:00 23:59
  예) /add 용산 목포 20260924 10:00 23:59 220000
/list  등록된 구간 보기
/cancel <번호>  구간 취소 (/cancel all 이면 전부)
/status  워커 상태 보기
/help  이 도움말

지정석(일반실·특실)이 나오면 자동으로 예약하고 알려드립니다.
입석은 잡지 않고, 결제는 코레일톡에서 직접 하셔야 합니다."""


# ---------------------------------------------------------------------------
# 구간(작업) 저장소
# ---------------------------------------------------------------------------
@dataclass
class Job:
    """등록된 조회 구간 하나. 재시작해도 살아남도록 파일에 저장한다."""

    id: int
    dep: str
    arr: str
    date: str
    start_time: str
    end_time: str
    arrive_before: str | None = None
    status: str = "watching"  # watching / reserved / cancelled
    note: str = ""
    created_at: str = ""
    error_streak: int = field(default=0, repr=False)
    error_alerted: bool = field(default=False, repr=False)

    def describe(self) -> str:
        arrive_txt = f", 도착 {self.arrive_before} 전" if self.arrive_before else ""
        return (
            f"{self.date} {self.dep}→{self.arr} "
            f"(출발 {self.start_time}~{self.end_time}{arrive_txt})"
        )

    @property
    def active(self) -> bool:
        return self.status == "watching"


class JobStore:
    """구간 목록. 봇 스레드와 워커 스레드가 함께 쓰므로 락으로 감싼다."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._jobs: list[Job] = []
        self._next_id = 1
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("작업 파일을 읽지 못했습니다(%s). 빈 목록으로 시작합니다.", exc)
            return
        for item in raw.get("jobs", []):
            item.pop("error_streak", None)
            item.pop("error_alerted", None)
            try:
                self._jobs.append(Job(**item))
            except TypeError as exc:
                logger.warning("작업 항목을 건너뜁니다: %s (%s)", item, exc)
        self._next_id = max((job.id for job in self._jobs), default=0) + 1
        logger.info("저장된 구간 %s개를 불러왔습니다.", len(self._jobs))

    def _save_locked(self) -> None:
        payload = {"jobs": [asdict(job) for job in self._jobs]}
        for item in payload["jobs"]:
            item.pop("error_streak", None)
            item.pop("error_alerted", None)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, job: Job) -> Job:
        with self._lock:
            job.id = self._next_id
            self._next_id += 1
            job.created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
            self._jobs.append(job)
            self._save_locked()
        return job

    def all(self) -> list[Job]:
        with self._lock:
            return list(self._jobs)

    def active(self) -> list[Job]:
        with self._lock:
            return [job for job in self._jobs if job.active]

    def find(self, job_id: int) -> Job | None:
        with self._lock:
            return next((job for job in self._jobs if job.id == job_id), None)

    def update(self, job: Job, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(job, key, value)
            self._save_locked()


# ---------------------------------------------------------------------------
# 텔레그램 입출력
# ---------------------------------------------------------------------------
def api_call(token: str, method: str, params: dict, timeout: int = 40):
    """텔레그램 Bot API 호출. 실패하면 None."""
    url = f"https://api.telegram.org/bot{token}/{method}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("텔레그램 %s 실패: %s", method, str(exc).replace(token, "***"))
        return None
    if not body.get("ok"):
        logger.warning("텔레그램 %s 오류 응답: %s", method, body)
        return None
    return body.get("result")


def drain_pending(token: str) -> int:
    """재시작 전에 쌓인 명령이 한꺼번에 실행되지 않도록 offset 을 최신으로 맞춘다."""
    result = api_call(token, "getUpdates", {"offset": -1, "timeout": 0}, timeout=15)
    if not result:
        return 0
    return result[-1]["update_id"] + 1


# ---------------------------------------------------------------------------
# 명령 처리
# ---------------------------------------------------------------------------
def parse_add(args: list[str]) -> Job:
    """/add 인자를 Job 으로. 쉼표든 공백이든 받아준다."""
    if len(args) not in (5, 6):
        raise ValueError(
            "형식: /add 출발역 도착역 날짜 시작 종료 [도착기한]\n"
            "예: /add 광명 목포 20260923 18:00 23:59"
        )
    dep, arr, date_txt, start_txt, end_txt = args[:5]
    arrive_before = args[5] if len(args) == 6 else None

    if dep == arr:
        raise ValueError("출발역과 도착역이 같습니다.")
    # strptime 은 "2026923" 처럼 자릿수가 모자란 값도 받아주므로 길이를 먼저 본다.
    if len(date_txt) != 8 or not date_txt.isdigit():
        raise ValueError(f"날짜 형식이 잘못됐습니다: {date_txt} (YYYYMMDD, 8자리)")
    try:
        datetime.strptime(date_txt, "%Y%m%d")
    except ValueError:
        raise ValueError(f"날짜 형식이 잘못됐습니다: {date_txt} (YYYYMMDD)") from None

    start = parse_time(start_txt)
    end = parse_time(end_txt)
    if start > end:
        raise ValueError("출발 시작 시각이 종료 시각보다 늦습니다.")
    if arrive_before:
        parse_time(arrive_before)

    return Job(
        id=0,
        dep=dep,
        arr=arr,
        date=date_txt,
        start_time=start_txt,
        end_time=end_txt,
        arrive_before=arrive_before,
    )


def format_list(jobs: list[Job]) -> str:
    if not jobs:
        return "등록된 구간이 없습니다. /add 로 추가하세요."
    marks = {"watching": "🔍", "reserved": "✅", "cancelled": "🚫"}
    lines = []
    for job in jobs:
        line = f"{marks.get(job.status, '?')} [{job.id}] {job.describe()}"
        if job.note:
            line += f"\n     {job.note}"
        lines.append(line)
    return "\n".join(lines)


def handle_command(text: str, store: JobStore, state: "WorkerState") -> str:
    """명령 문자열 하나를 처리하고 답장 내용을 돌려준다."""
    text = text.strip()
    if not text.startswith("/"):
        return "명령은 / 로 시작합니다. /help 를 보내보세요."

    parts = text.replace(",", " ").split()
    command = parts[0].split("@")[0].lower()
    args = parts[1:]

    if command in ("/start", "/help"):
        return HELP

    if command == "/add":
        try:
            job = parse_add(args)
        except ValueError as exc:
            return f"⚠️ {exc}"
        store.add(job)
        return f"등록했습니다. [{job.id}] {job.describe()}\n지정석이 나오면 알려드립니다."

    if command == "/list":
        return format_list(store.all())

    if command == "/cancel":
        if not args:
            return "취소할 번호를 붙여주세요. 예: /cancel 1 (전부 취소는 /cancel all)"
        if args[0].lower() == "all":
            targets = store.active()
            for job in targets:
                store.update(job, status="cancelled", note="사용자가 취소")
            return f"{len(targets)}개 구간을 취소했습니다." if targets else "취소할 구간이 없습니다."
        if not args[0].isdigit():
            return f"번호는 숫자입니다: {args[0]}"
        job = store.find(int(args[0]))
        if job is None:
            return f"[{args[0]}] 구간을 찾을 수 없습니다. /list 로 확인하세요."
        if not job.active:
            return f"[{job.id}] 은 이미 {job.status} 상태입니다."
        store.update(job, status="cancelled", note="사용자가 취소")
        return f"취소했습니다. [{job.id}] {job.describe()}"

    if command == "/status":
        return state.summary(store)

    return f"모르는 명령입니다: {command}\n/help 를 보내보세요."


# ---------------------------------------------------------------------------
# 워커
# ---------------------------------------------------------------------------
class WorkerState:
    """/status 로 보여줄 워커 현황."""

    def __init__(self, passengers: KorailPassengerCounts, interval: int):
        self.passengers = passengers
        self.interval = interval
        self.started_at = datetime.now()
        self.cycles = 0
        self.last_check: datetime | None = None
        self.last_error: str = ""
        self.account_name: str = ""

    def summary(self, store: JobStore) -> str:
        jobs = store.all()
        watching = sum(1 for job in jobs if job.active)
        reserved = sum(1 for job in jobs if job.status == "reserved")
        last = self.last_check.strftime("%H:%M:%S") if self.last_check else "아직 없음"
        lines = [
            "🚄 워커 상태",
            f"계정: {self.account_name or '(로그인 중)'}",
            f"인원: {format_passengers(self.passengers)}",
            f"조회 간격: {self.interval}초 / 누적 {self.cycles}주기",
            f"마지막 조회: {last}",
            f"구간: 감시 중 {watching} · 예약 완료 {reserved} · 전체 {len(jobs)}",
            f"가동 시작: {self.started_at.strftime('%m-%d %H:%M')}",
        ]
        if self.last_error:
            lines.append(f"최근 오류: {self.last_error}")
        return "\n".join(lines)


def worker_loop(macro: KTXMacro, store: JobStore, state: WorkerState, stop: threading.Event) -> None:
    """등록된 구간을 번갈아 조회하고, 지정석이 나오면 예약한다."""
    logger.info("워커 시작 (간격 %s초)", state.interval)
    while not stop.is_set():
        jobs = store.active()
        if not jobs:
            stop.wait(state.interval)
            continue

        try:
            macro._ensure_login()
        except Exception as exc:
            state.last_error = f"로그인 실패: {exc}"
            logger.error("로그인 실패: %s", exc)
            send_telegram(f"❌ 코레일 로그인 실패\n{exc}\n{state.interval * 4}초 뒤 다시 시도합니다.")
            stop.wait(state.interval * 4)
            continue

        state.cycles += 1
        state.last_check = datetime.now()

        for job in jobs:
            if stop.is_set():
                break
            try:
                trains = macro.search_trains(
                    job.date, job.dep, job.arr, job.start_time, job.end_time, job.arrive_before
                )
            except Exception as exc:
                message = str(exc).splitlines()[0][:200]
                state.last_error = message
                logger.error("[%s] 검색 중 예외: %s", job.id, exc)
                job.error_streak += 1
                if job.error_streak >= JOB_ERROR_THRESHOLD and not job.error_alerted:
                    job.error_alerted = True
                    store.update(job, note=f"검색 실패: {message}")
                    send_telegram(
                        f"⚠️ [{job.id}] {job.describe()}\n"
                        f"검색이 {job.error_streak}회 연속 실패했습니다.\n{exc}"
                    )
                continue

            job.error_streak = 0
            job.error_alerted = False

            seated = [train for train in trains if has_assigned_seat(train)]
            logger.info(
                "[%s] %s | 조회 %s편 (지정석 %s편)", job.id, job.describe(), len(trains), len(seated)
            )
            if not seated:
                continue

            target = seated[0]
            logger.info("[%s] 지정석 발견, 예약 시도: %s", job.id, format_train(target))
            result = macro.try_reserve(target)
            if not result:
                logger.warning("[%s] 예약 실패. 다음 주기에 다시 시도합니다.", job.id)
                continue

            deadline = format_deadline(result)
            store.update(
                job,
                status="reserved",
                note=f"{format_train(target)}" + (f" · 구입기한 {deadline}" if deadline else ""),
            )
            logger.info("[%s] 예약 완료. 구입기한: %s", job.id, deadline or "(응답에 없음)")
            ktx_macro.beep()
            send_telegram(
                "\n".join(
                    line
                    for line in (
                        "✅ KTX 예약 완료",
                        f"[{job.id}] {job.describe()}",
                        f"열차: {format_train(target)}",
                        f"인원: {format_passengers(macro.passengers)}",
                        f"구입기한: {deadline}" if deadline else None,
                        "기한 안에 코레일톡/홈페이지에서 결제하세요.",
                    )
                    if line
                )
            )

        stop.wait(state.interval)
    logger.info("워커 종료")


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="텔레그램 봇으로 조작하는 KTX 매크로")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="조회 간격(초)")
    parser.add_argument("--adult", type=int, default=1, help="어른 인원 (기본 1)")
    parser.add_argument("--teen", type=int, default=0, help="청소년 인원")
    parser.add_argument("--child", type=int, default=0, help="어린이 인원")
    parser.add_argument("--senior", type=int, default=0, help="경로 인원")
    parser.add_argument("--infant", type=int, default=0, help="동반 유아 인원 (좌석 없음)")
    args = parser.parse_args()

    config = telegram_config()
    if config is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN 과 TELEGRAM_CHAT_ID 를 .env 에 설정해주세요.")
    token, chat_id = config

    try:
        passengers = KorailPassengerCounts(
            adult=args.adult,
            teenager=args.teen,
            child=args.child,
            infant=args.infant,
            senior=args.senior,
        )
    except ValueError as exc:
        parser.error(f"인원 설정 오류: {exc} (총 1~9명, 음수 불가)")

    korail_id, korail_pw = load_credentials()
    store = JobStore(JOBS_FILE)
    state = WorkerState(passengers, args.interval)

    try:
        macro = KTXMacro(korail_id, korail_pw, passengers=passengers)
    except Exception as exc:
        send_telegram(f"❌ 코레일 로그인 실패로 봇을 시작하지 못했습니다.\n{exc}")
        raise
    state.account_name = getattr(macro, "korail_id", "")

    stop = threading.Event()
    worker = threading.Thread(target=worker_loop, args=(macro, store, state, stop), daemon=True)
    worker.start()

    offset = drain_pending(token)
    send_telegram("🚄 KTX 매크로 봇이 시작됐습니다.\n" + HELP)
    logger.info("봇 대기 중. 텔레그램에서 명령을 보내세요.")

    # systemd 와 쿠버네티스는 SIGTERM 으로 끈다. Ctrl+C 와 같은 경로로 정리하게 한다.
    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        while True:
            updates = api_call(token, "getUpdates", {"offset": offset, "timeout": 30})
            if updates is None:
                time.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue
                sender = str(message.get("chat", {}).get("id", ""))
                if sender != str(chat_id):
                    # 등록된 사람 외에는 무시한다. 계정과 예약 정보가 걸려 있다.
                    logger.warning("허용되지 않은 chat_id 의 메시지 무시: %s", sender)
                    continue
                text = message.get("text") or ""
                if not text:
                    continue
                logger.info("명령 수신: %s", text)
                try:
                    reply = handle_command(text, store, state)
                except Exception as exc:
                    logger.exception("명령 처리 중 오류")
                    reply = f"명령 처리 중 오류가 났습니다: {exc}"
                send_telegram(reply)
    except KeyboardInterrupt:
        logger.info("사용자 중단")
    finally:
        stop.set()
        worker.join(timeout=state.interval + 5)
        macro.close()


if __name__ == "__main__":
    main()
