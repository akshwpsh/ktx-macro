#!/usr/bin/env python3
"""KTX 매크로 - 지정 시간대에서 지정석이 나올 때까지 검색 후 예약.

korail2 는 2026년 기준 MACRO ERROR 로 로그인이 막힌다.
현재 코레일톡 API 는 korail-mobile-api (앱 6.5.0 기준) 를 사용한다.
k-skill ktx-booking 은 공개 시간표 조회 전용으로 바뀌어 예약은 하지 않는다.

입석 / 입석+좌석은 예약하지 않는다. 결제는 코레일톡/홈페이지에서 직접 해야 한다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(interpolate=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

from korail_mobile_api import (
    KorailAuthError,
    KorailClient,
    KorailConfig,
    KorailNoResultsError,
    KorailPassengerCounts,
    KorailSeatClass,
    KorailSessionExpiredError,
    KorailSoldOutError,
    MutationConsent,
    TrainSearchQuery,
)

ASSIGNED_SEAT_CODE = "11"
# 검색 예외가 이만큼 연속되면 텔레그램으로 한 번 알린다.
ERROR_ALERT_THRESHOLD = 5
KTX_TRAIN_GROUP = "100"
MUGUNGHWA_MARKERS = ("무궁화",)


# 역 목록 조회에 실패했을 때 쓰는 기본 목록 (코레일 앱 주요역 순서).
DEFAULT_STATIONS = [
    "서울", "용산", "광명", "수서", "영등포", "수원", "평택", "천안아산", "천안", "오송",
    "조치원", "대전", "서대전", "김천구미", "구미", "동대구", "대구", "경주", "울산(통도사)", "포항",
    "경산", "밀양", "부산", "구포", "창원중앙", "평창", "진부(오대산)", "강릉", "익산", "전주",
    "광주송정", "목포", "순천", "청량리", "여수EXPO", "동해", "정동진", "안동", "서원주", "원주",
    "마산", "행신", "나주", "정읍", "남원",
]


def fetch_station_names() -> list[str]:
    """코레일 전체 역 이름 목록을 로그인 없이 받아온다.

    주요역(코레일 앱 major 순위)을 먼저, 나머지는 가나다순으로 뒤에 붙인다.
    실패하면 DEFAULT_STATIONS 를 돌려준다.
    """
    client = KorailClient(KorailConfig(enable_dynapath=True))
    try:
        stations = list(client.get_station_data().stations)
    except Exception as exc:  # 네트워크/프로토콜 오류 등
        logger.warning("역 목록 조회 실패, 기본 목록 사용: %s", exc)
        return list(DEFAULT_STATIONS)
    finally:
        try:
            client.close()
        except Exception:
            pass

    def major_rank(st) -> int:
        try:
            return int(st.major)
        except (TypeError, ValueError):
            return 10**6

    majors = sorted((s for s in stations if major_rank(s) < 10**6), key=major_rank)
    others = sorted((s for s in stations if major_rank(s) >= 10**6), key=lambda s: s.name)
    names: list[str] = []
    for s in majors + others:
        if s.name and s.name not in names:
            names.append(s.name)
    return names or list(DEFAULT_STATIONS)


def parse_time(value: str) -> str:
    """HHMMSS / HHMM / HH:MM / HH:MM:SS 를 HHMMSS 로 정규화."""
    raw = value.replace(":", "").strip()
    if len(raw) == 4:
        raw += "00"
    if not raw.isdigit() or len(raw) != 6:
        raise ValueError(f"시간 형식 오류: {value} (HHMMSS 또는 HH:MM)")
    hh, mm, ss = int(raw[:2]), int(raw[2:4]), int(raw[4:])
    if hh > 23 or mm > 59 or ss > 59:
        raise ValueError(f"잘못된 시각: {value}")
    return raw


def as_hhmmss(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).replace(":", "").strip()
    if not cleaned:
        return None
    if len(cleaned) == 4 and cleaned.isdigit():
        cleaned += "00"
    if len(cleaned) >= 6 and cleaned[:6].isdigit():
        return cleaned[:6]
    return parse_time(cleaned)


def format_hhmm(value: str | None) -> str:
    hhmmss = as_hhmmss(value)
    if not hhmmss:
        return "?"
    return f"{hhmmss[:2]}:{hhmmss[2:4]}"


def is_time_in_range(dep_time: str | None, start: str, end: str) -> bool:
    dep = as_hhmmss(dep_time)
    return dep is not None and start <= dep <= end


def arrives_before(arr_time: str | None, deadline: str) -> bool:
    arr = as_hhmmss(arr_time)
    return arr is not None and arr < deadline


def normalize_login_id(value: str) -> str:
    """휴대폰은 하이픈 없이 숫자만. 이메일은 그대로."""
    value = value.strip()
    if "@" in value:
        return value
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits.startswith("01") and len(digits) in {10, 11}:
        return digits
    return value


def is_mugunghwa(train) -> bool:
    name = f"{getattr(train, 'train_class_name', '') or ''} {getattr(train, 'train_group_name', '') or ''}"
    return any(marker in name for marker in MUGUNGHWA_MARKERS)


def has_assigned_seat(train) -> bool:
    """일반실/특실 지정석만 True. 입석·입석+좌석은 False."""
    return (
        getattr(train, "general_reservation_code", None) == ASSIGNED_SEAT_CODE
        or getattr(train, "special_reservation_code", None) == ASSIGNED_SEAT_CODE
    )


def preferred_seat_class(train) -> KorailSeatClass:
    if getattr(train, "general_reservation_code", None) == ASSIGNED_SEAT_CODE:
        return KorailSeatClass.GENERAL
    return KorailSeatClass.SPECIAL


def seat_status(train) -> str:
    kinds = []
    if getattr(train, "general_reservation_code", None) == ASSIGNED_SEAT_CODE:
        kinds.append("일반실")
    if getattr(train, "special_reservation_code", None) == ASSIGNED_SEAT_CODE:
        kinds.append("특실")
    if kinds:
        return ",".join(kinds) + " 예약가능"
    return "지정석 없음"


def format_train(train) -> str:
    type_name = getattr(train, "train_class_name", "") or "KTX"
    train_no = getattr(train, "train_no", "?")
    dep = format_hhmm(getattr(train, "departure_time", None))
    arr = format_hhmm(getattr(train, "arrival_time", None))
    return f"[{type_name}] {train_no} {dep}→{arr} ({seat_status(train)})"


def seated_count(passengers: KorailPassengerCounts) -> int:
    """조회에 쓸 좌석 점유 인원. 동반유아는 좌석이 없으므로 제외한다."""
    return max(1, passengers.total - passengers.infant)


def format_passengers(passengers: KorailPassengerCounts) -> str:
    labels = (
        ("adult", "어른"),
        ("teenager", "청소년"),
        ("child", "어린이"),
        ("senior", "경로"),
        ("infant", "유아"),
    )
    parts = [f"{label} {getattr(passengers, attr)}" for attr, label in labels if getattr(passengers, attr)]
    return f"총 {passengers.total}명 ({', '.join(parts)})"


def telegram_config() -> tuple[str, str] | None:
    """.env 의 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. 둘 다 있어야 알림을 보낸다."""
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return None
    return token, chat_id


def send_telegram(text: str) -> bool:
    """텔레그램으로 알림을 보낸다. 설정이 없으면 조용히 건너뛴다.

    서버(리눅스)에서는 비프음이 들리지 않으므로 이 알림이 유일한 통보 수단이다.
    전송에 실패해도 예약 흐름은 멈추지 않는다.
    """
    config = telegram_config()
    if config is None:
        return False
    token, chat_id = config
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except Exception as exc:
        # 오류 메시지에 토큰이 섞여 나가지 않도록 가린다.
        logger.warning("텔레그램 알림 전송 실패: %s", str(exc).replace(token, "***"))
        return False
    return True


def beep() -> None:
    try:
        import winsound

        winsound.Beep(1000, 600)
        winsound.Beep(1400, 800)
    except Exception:
        print("\a", end="", flush=True)


def notify_success(message: str | None = None) -> None:
    beep()
    if message:
        send_telegram(message)


def notify_error(message: str) -> None:
    """로그인 실패·연속 예외·비정상 종료처럼 사람이 개입해야 하는 상황."""
    send_telegram(message)


def format_deadline(result) -> str | None:
    """예약 응답에서 구입기한 문자열을 뽑는다. 없으면 None."""
    deadline_date = getattr(result, "payment_deadline_date", None)
    deadline_time = getattr(result, "payment_deadline_time", None)
    if deadline_date and deadline_time:
        return f"{deadline_date} {format_hhmm(deadline_time)}"
    return getattr(result, "payment_deadline_message", None) or None


@dataclass
class Leg:
    """조회·예약할 구간 하나. 왕복이면 두 개를 만든다."""

    name: str
    date: str
    dep: str
    arr: str
    start_time: str
    end_time: str
    arrive_before: str | None = None
    result: object = field(default=None, repr=False)

    @property
    def done(self) -> bool:
        return self.result is not None

    def describe(self) -> str:
        arrive_txt = f", 도착 {self.arrive_before} 전" if self.arrive_before else ""
        return f"{self.date} {self.dep} -> {self.arr} (출발 {self.start_time}~{self.end_time}{arrive_txt})"


class KTXMacro:
    def __init__(
        self,
        korail_id: str,
        korail_pw: str,
        passengers: KorailPassengerCounts | None = None,
    ):
        self.korail_id = normalize_login_id(korail_id)
        self.korail_pw = korail_pw
        self.passengers = passengers or KorailPassengerCounts()
        self.client = KorailClient(KorailConfig(enable_dynapath=True))
        self._login()

    def close(self) -> None:
        try:
            self.client.logout()
        except Exception:
            pass
        try:
            self.client.close()
        except Exception:
            pass

    def _login(self) -> None:
        try:
            session = self.client.login(self.korail_id, self.korail_pw)
        except KorailAuthError as exc:
            raise RuntimeError(f"코레일 로그인 실패: {exc}") from exc
        name = (session.raw or {}).get("strCustNm") or ""
        suffix = f" ({name})" if name else ""
        logger.info("코레일 로그인 성공%s", suffix)

    def _ensure_login(self) -> None:
        if getattr(self.client.session, "current", None) is None:
            logger.warning("세션이 없습니다. 다시 로그인합니다.")
            self._login()

    def search_trains(
        self,
        date: str,
        dep: str,
        arr: str,
        start_time: str,
        end_time: str,
        arrive_before: str | None,
    ):
        start = parse_time(start_time)
        end = parse_time(end_time)
        deadline = parse_time(arrive_before) if arrive_before else None
        query = TrainSearchQuery(
            dep,
            arr,
            date,
            departure_time=start,
            passengers=seated_count(self.passengers),
            train_group_code=KTX_TRAIN_GROUP,
        )

        trains = []
        continuation = None
        while True:
            try:
                result = self.client.search_trains(query, continuation=continuation)
            except KorailNoResultsError:
                break
            except KorailSessionExpiredError:
                self._login()
                continuation = None
                continue

            page = list(result.trains or [])
            if not page:
                break
            trains.extend(page)
            last_dep = as_hhmmss(getattr(page[-1], "departure_time", None))
            if last_dep and last_dep > end:
                break
            continuation = result.next_page()
            if continuation is None:
                break

        matched = []
        for train in trains:
            if is_mugunghwa(train):
                continue
            if not is_time_in_range(getattr(train, "departure_time", None), start, end):
                continue
            if deadline and not arrives_before(getattr(train, "arrival_time", None), deadline):
                continue
            matched.append(train)
        return matched

    def try_reserve(self, train):
        if not has_assigned_seat(train):
            logger.info("지정석이 없어 예약하지 않음: %s", format_train(train))
            return None
        consent = MutationConsent(allow_reserve=True, dry_run=False)
        try:
            hold = self.client.reserve(
                train,
                consent=consent,
                passengers=self.passengers,
                seat_class=preferred_seat_class(train),
            )
            logger.info("예약 성공: %s", hold)
            return hold
        except KorailSessionExpiredError:
            self._login()
            return self.try_reserve(train)
        except KorailSoldOutError as exc:
            logger.warning("예약 중 매진: %s", exc)
            return None
        except Exception as exc:
            logger.error("예약 실패: %s", exc)
            return None

    def run(
        self,
        date: str,
        dep: str,
        arr: str,
        start_time: str,
        end_time: str,
        arrive_before: str | None,
        interval: int = 8,
        max_attempts: int = 0,
        search_only: bool = False,
        stop_event: threading.Event | None = None,
    ):
        """편도 한 구간. 예약 응답을 돌려주고, 못 잡으면 None."""
        leg = Leg("편도", date, dep, arr, start_time, end_time, arrive_before)
        self.run_legs(
            [leg],
            interval=interval,
            max_attempts=max_attempts,
            search_only=search_only,
            stop_event=stop_event,
        )
        return leg.result

    def run_legs(
        self,
        legs: list[Leg],
        interval: int = 8,
        max_attempts: int = 0,
        search_only: bool = False,
        stop_event: threading.Event | None = None,
    ) -> list[Leg]:
        """여러 구간을 한 세션에서 번갈아 조회한다. 예약된 구간은 빼고 남은 구간만 계속 찾는다."""
        if not legs:
            raise ValueError("조회할 구간이 없습니다.")
        multi = len(legs) > 1
        attempts_txt = "무제한" if max_attempts <= 0 else f"{max_attempts}회"
        logger.info("검색 시작 | 간격 %s초 | 최대 %s | 구간 %s개", interval, attempts_txt, len(legs))
        for leg in legs:
            logger.info("  [%s] %s", leg.name, leg.describe())
        logger.info("인원: %s", format_passengers(self.passengers))
        logger.info("입석/입석+좌석은 무시하고, 일반실·특실 지정석만 예약합니다. 무궁화호는 제외합니다.")

        attempt = 0
        error_streak = 0
        error_alerted = False
        while True:
            if stop_event is not None and stop_event.is_set():
                logger.info("중지 요청으로 종료합니다.")
                return legs
            attempt += 1
            if max_attempts > 0 and attempt > max_attempts:
                logger.warning("최대 시도 횟수 도달. 종료")
                return legs

            self._ensure_login()
            logger.info("[%s] 검색 중...", attempt if max_attempts <= 0 else f"{attempt}/{max_attempts}")

            for leg in legs:
                if leg.done:
                    continue
                tag = f"[{leg.name}] " if multi else ""
                try:
                    trains = self.search_trains(
                        leg.date, leg.dep, leg.arr, leg.start_time, leg.end_time, leg.arrive_before
                    )
                except Exception as exc:
                    logger.error("%s검색 중 예외: %s", tag, exc)
                    error_streak += 1
                    if error_streak >= ERROR_ALERT_THRESHOLD and not error_alerted:
                        # 조용히 실패만 반복하는 상태를 며칠 뒤에 발견하는 일을 막는다.
                        error_alerted = True
                        notify_error(
                            f"⚠️ KTX 매크로 검색 실패 {error_streak}회 연속\n"
                            f"{leg.describe()}\n마지막 오류: {exc}"
                        )
                    continue
                error_streak = 0
                error_alerted = False

                if not trains:
                    logger.info("%s조건에 맞는 열차 없음. 대기...", tag)
                    continue

                seated = [train for train in trains if has_assigned_seat(train)]
                for train in trains:
                    logger.info("  %s%s", tag, format_train(train))

                if search_only:
                    logger.info(
                        "%s조회만 수행 (--search-only). 지정석 %s편 / 입석·매진 %s편",
                        tag,
                        len(seated),
                        len(trains) - len(seated),
                    )
                    continue

                if not seated:
                    logger.info("%s아직 지정석 없음 (입석+좌석/매진). 계속 대기합니다.", tag)
                    continue

                target = seated[0]
                logger.info("%s지정석 발견, 예약 시도: %s", tag, format_train(target))
                result = self.try_reserve(target)
                if not result:
                    logger.warning("%s예약 실패. 다음 주기에 다시 시도합니다.", tag)
                    continue
                leg.result = result
                logger.info("%s예약 완료. 코레일톡/홈페이지에서 결제하세요.", tag)
                deadline = format_deadline(result)
                if deadline:
                    logger.info("%s구입기한: %s", tag, deadline)
                notify_success(
                    "\n".join(
                        line
                        for line in (
                            "✅ KTX 예약 완료",
                            f"구간: {leg.name} {leg.describe()}" if multi else f"구간: {leg.describe()}",
                            f"열차: {format_train(target)}",
                            f"인원: {format_passengers(self.passengers)}",
                            f"구입기한: {deadline}" if deadline else None,
                            "기한 안에 코레일톡/홈페이지에서 결제하세요.",
                        )
                        if line
                    )
                )

            if search_only:
                return legs
            if all(leg.done for leg in legs):
                if multi:
                    logger.info("모든 구간 예약 완료.")
                return legs
            remaining = [leg.name for leg in legs if not leg.done]
            if multi and len(remaining) < len(legs):
                logger.info("남은 구간 계속 조회: %s (예약된 구간은 구입기한 안에 결제하세요)", ", ".join(remaining))
            self._wait(interval, stop_event)

    @staticmethod
    def _wait(seconds: float, stop_event: threading.Event | None) -> None:
        """조회 간격만큼 대기. 중지 요청이 오면 바로 깨어난다."""
        if stop_event is None:
            time.sleep(seconds)
        else:
            stop_event.wait(seconds)


def load_credentials() -> tuple[str, str]:
    korail_id = os.getenv("KORAIL_ID")
    korail_pw = os.getenv("KORAIL_PW")
    if not korail_id or not korail_pw:
        raise ValueError("KORAIL_ID 와 KORAIL_PW 를 .env 파일에 설정해주세요.")
    return korail_id, korail_pw


def build_legs(args) -> list[Leg]:
    """--leg 를 쓰면 그대로, 아니면 --date/--start-time/--end-time 으로 한 구간을 만든다."""
    if not args.leg:
        datetime.strptime(args.date, "%Y%m%d")
        parse_time(args.start_time)
        parse_time(args.end_time)
        if args.arrive_before:
            parse_time(args.arrive_before)
        return [
            Leg(
                "편도",
                args.date,
                args.dep,
                args.arr,
                args.start_time,
                args.end_time,
                args.arrive_before,
            )
        ]

    legs: list[Leg] = []
    for raw in args.leg:
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) not in (3, 4):
            raise ValueError(f"--leg 형식이 잘못됐습니다: {raw!r} (날짜,시작,종료[,도착기한])")
        date_txt, start_txt, end_txt = parts[:3]
        arrive_before = parts[3] if len(parts) == 4 and parts[3] else None
        try:
            datetime.strptime(date_txt, "%Y%m%d")
        except ValueError:
            raise ValueError(f"--leg 날짜가 잘못됐습니다: {date_txt!r} (YYYYMMDD)") from None
        for time_txt in (start_txt, end_txt) + ((arrive_before,) if arrive_before else ()):
            try:
                parse_time(time_txt)
            except ValueError:
                raise ValueError(f"--leg 시각이 잘못됐습니다: {time_txt!r} (HHMMSS 또는 HH:MM)") from None
        name = f"{date_txt[4:6]}/{date_txt[6:8]} {start_txt}~"
        legs.append(Leg(name, date_txt, args.dep, args.arr, start_txt, end_txt, arrive_before))
    return legs


def main() -> None:
    parser = argparse.ArgumentParser(description="KTX 시간대 자동 예약 매크로 (지정석만)")
    parser.add_argument("--date", help="예약 날짜 (YYYYMMDD, 예: 20260827)")
    parser.add_argument("--dep", help="출발역 (예: 대전)")
    parser.add_argument("--arr", help="도착역 (예: 서울)")
    parser.add_argument("--start-time", default="080000", help="출발 시작 시각 (HHMMSS 또는 HH:MM)")
    parser.add_argument("--end-time", default="235959", help="출발 종료 시각 (HHMMSS 또는 HH:MM)")
    parser.add_argument(
        "--arrive-before",
        default=None,
        help="이 시각 전 도착만 (HHMMSS 또는 HH:MM). 예: 180000",
    )
    parser.add_argument("--adult", type=int, default=1, help="어른 인원 (기본 1)")
    parser.add_argument("--teen", type=int, default=0, help="청소년 인원")
    parser.add_argument("--child", type=int, default=0, help="어린이 인원")
    parser.add_argument("--senior", type=int, default=0, help="경로 인원")
    parser.add_argument("--infant", type=int, default=0, help="동반 유아 인원 (좌석 없음)")
    parser.add_argument("--interval", type=int, default=8, help="검색 간격 (초)")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=0,
        help="최대 시도 횟수. 0이면 좌석이 나올 때까지 계속",
    )
    parser.add_argument(
        "--search-only",
        action="store_true",
        help="예약하지 않고 한 번만 조회",
    )
    parser.add_argument(
        "--leg",
        action="append",
        metavar="날짜,시작,종료[,도착기한]",
        help=(
            "날짜/시간대를 여러 개 지정 (여러 번 사용 가능). "
            "예: --leg 20260923,18:00,23:59 --leg 20260924,10:00,23:59. "
            "--dep/--arr 는 공통으로 쓰이고, --date/--start-time/--end-time 은 무시됩니다"
        ),
    )
    parser.add_argument(
        "--notify-test",
        action="store_true",
        help="텔레그램 알림만 한 번 보내보고 종료 (설정 확인용)",
    )
    args = parser.parse_args()

    if args.notify_test:
        if telegram_config() is None:
            parser.error("TELEGRAM_BOT_TOKEN 과 TELEGRAM_CHAT_ID 를 .env 에 설정해주세요.")
        if send_telegram("🚄 KTX 매크로 텔레그램 알림 테스트입니다."):
            logger.info("텔레그램 알림을 보냈습니다. 휴대폰을 확인하세요.")
        else:
            raise SystemExit("텔레그램 전송에 실패했습니다. 토큰과 chat_id 를 확인하세요.")
        return

    required = ("dep", "arr") if args.leg else ("date", "dep", "arr")
    missing = [name for name in required if not getattr(args, name)]
    if missing:
        parser.error("다음 인자가 필요합니다: " + ", ".join(f"--{name}" for name in missing))

    try:
        legs = build_legs(args)
    except ValueError as exc:
        parser.error(str(exc))

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
    route = "\n".join(leg.describe() for leg in legs)
    try:
        macro = KTXMacro(korail_id, korail_pw, passengers=passengers)
    except Exception as exc:
        notify_error(f"❌ KTX 매크로 로그인 실패\n{route}\n{exc}")
        raise

    try:
        macro.run_legs(
            legs,
            interval=args.interval,
            max_attempts=args.max_attempts,
            search_only=args.search_only,
        )
    except Exception as exc:
        # 서버에서 무인 실행 중 죽으면 알림 없이는 알 방법이 없다.
        notify_error(f"❌ KTX 매크로 비정상 종료\n{route}\n{type(exc).__name__}: {exc}")
        raise
    else:
        if not args.search_only and not any(leg.done for leg in legs):
            notify_error(f"⏹️ KTX 매크로가 예약 없이 종료했습니다.\n{route}")
    finally:
        macro.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("사용자 중단")
