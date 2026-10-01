#!/usr/bin/env python3
"""
PSN online ID availability checker.

Checks whether PlayStation Network online IDs (usernames) are free using
PSN's own availability endpoint:

    POST https://accounts.api.playstation.com/api/v1/accounts/onlineIds
    Body: {"onlineId": "<name>", "reserveIfAvailable": false}

No login, NPSSO, or authentication is required.

Response decoding (verified 2026):

    200..299                         -> AVAILABLE
    400  X-ErrorCode: accounts:3101  -> TAKEN
    400  X-ErrorCode: accounts:3208  -> IMPROPER (policy)
    400  X-ErrorCode: korra:1100     -> INVALID pattern/length
    406                              -> REJECTED BY POLICY
    429                              -> RATE LIMITED (back off)

Notes
-----
* PSN rejects every online ID shorter than 5 characters with HTTP 406, so
  3- and 4-character IDs are no longer registrable. This script refuses
  --length < 5 unless you pass --allow-short.
* ``reserveIfAvailable`` is False, so a 201 does NOT reserve or claim the
  name -- it only reports that the name is free.
* Bulk automated access may violate PSN's terms of service. Use
  responsibly; see README.

Requirements: Python 3.8+ and ``pip install requests``.
"""

from __future__ import annotations

import argparse
import itertools
import random
import string
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Iterator, Set

import requests

API_URL = "https://accounts.api.playstation.com/api/v1/accounts/onlineIds"
MIN_PSN_LENGTH = 5
DEFAULT_CHARSET = string.ascii_lowercase

# Header set the endpoint expects (verified working without auth).
HEADERS = {
    "Connection": "keep-alive",
    "sec-ch-ua-platform": '"Windows"',
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "*/*",
    "Origin": "https://id.sonyentertainmentnetwork.com",
    "Referer": "https://id.sonyentertainmentnetwork.com/",
    "Accept-Language": "en-US,en;q=0.9",
}

# Results that are definite answers and worth caching for resume.
DEFINITE = {"available", "taken", "invalid", "improper", "rejected"}


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------
def classify(resp: requests.Response) -> str:
    """Map an HTTP response from the availability endpoint to a result."""
    status = resp.status_code
    if 200 <= status < 300:
        return "available"
    if status == 429:
        return "rate_limited"
    if status == 406:
        return "rejected"
    if status == 400:
        code = resp.headers.get("X-ErrorCode", "")
        if not code:
            try:
                payload = resp.json()
                code = payload[0].get("code", "") if isinstance(payload, list) else ""
            except Exception:  # noqa: BLE001
                code = ""
        if "3101" in code:
            return "taken"
        if "3208" in code:
            return "improper"
        return "invalid"
    return "error"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _human(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------
def matches_filters(args: argparse.Namespace, name: str) -> bool:
    low = name.lower()
    prefix = (args.starts_with or "").lower()
    suffix = (args.ends_with or "").lower()
    if prefix and not low.startswith(prefix):
        return False
    if suffix and not low.endswith(suffix):
        return False
    if args.pattern:
        if len(args.pattern) != len(low):
            return False
        for ch, pat in zip(low, args.pattern):
            if pat == "?":
                continue
            if pat == "V":
                if ch not in args.vowels:
                    return False
            elif pat == "C":
                if ch in args.vowels or not ch.isalpha():
                    return False
            elif ch != pat.lower():
                return False
    if args.vowel_positions:
        if not any(
            1 <= pos <= len(low) and low[pos - 1] in args.vowels
            for pos in args.vowel_positions
        ):
            return False
    return True


def iter_candidates(args: argparse.Namespace, skip: Set[str]) -> Iterator[str]:
    if args.from_file:
        seen: Set[str] = set()
        with open(args.from_file, "r", encoding="utf-8") as fh:
            for line in fh:
                name = line.strip()
                if not name or name in skip or name in seen:
                    continue
                if not matches_filters(args, name):
                    continue
                seen.add(name)
                yield name
        return

    for combo in itertools.product(args.charset, repeat=args.length):
        name = "".join(combo)
        if name in skip or not matches_filters(args, name):
            continue
        yield name


def total_candidates(args: argparse.Namespace, skip: Set[str]) -> int:
    if args.from_file:
        with open(args.from_file, "r", encoding="utf-8") as fh:
            return len({line.strip() for line in fh if line.strip()} - skip)
    if not args.starts_with and not args.ends_with and not args.vowel_positions and not args.pattern:
        return len(args.charset) ** args.length - len(skip)
    return sum(1 for _ in iter_candidates(args, skip))


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------
class Checker:
    def __init__(self, args: argparse.Namespace, skip: Set[str]) -> None:
        self.args = args
        self.total = total_candidates(args, skip)
        self._skip = skip
        self._lock = threading.Lock()
        self._delay = args.delay
        self.checked = 0
        self.available = 0
        self.started = time.time()
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.output_fh = open(args.output, "a", encoding="utf-8")
        self.state_fh = None if args.no_resume else open(args.state, "a", encoding="utf-8")

    def close(self) -> None:
        self.output_fh.close()
        if self.state_fh is not None:
            self.state_fh.close()

    @staticmethod
    def load_state(path: str) -> Set[str]:
        state = Path(path)
        if not state.exists():
            return set()
        with state.open("r", encoding="utf-8") as fh:
            return {line.strip() for line in fh if line.strip()}

    def _write(self, handle, text: str) -> None:
        with self._lock:
            handle.write(text + "\n")
            handle.flush()

    @property
    def delay(self) -> float:
        with self._lock:
            return self._delay

    def _adapt(self, rate_limited: bool) -> float:
        with self._lock:
            if rate_limited:
                self._delay = _clamp(self._delay * 2, self.args.min_delay, self.args.max_delay)
            else:
                self._delay = _clamp(self._delay * 0.95, self.args.min_delay, self.args.max_delay)
            return self._delay

    def check(self, name: str) -> str:
        payload = {"onlineId": name, "reserveIfAvailable": False}
        for attempt in range(1, self.args.max_retries + 1):
            try:
                resp = self.session.post(API_URL, json=payload, timeout=self.args.timeout)
            except requests.RequestException as exc:
                print(f"  [{name}] network error: {exc} (attempt {attempt}/{self.args.max_retries})")
                self._adapt(rate_limited=True)
                time.sleep(self.args.retry_wait)
                continue

            result = classify(resp)
            if result == "rate_limited":
                delay = self._adapt(rate_limited=True)
                print(f"  [{name}] rate limited (429); delay -> {delay:.1f}s "
                      f"(attempt {attempt}/{self.args.max_retries})")
                time.sleep(self.args.retry_wait)
                continue
            if result == "error" and 500 <= resp.status_code < 600:
                self._adapt(rate_limited=True)
                print(f"  [{name}] server error {resp.status_code}; retrying")
                time.sleep(self.args.retry_wait)
                continue
            self._adapt(rate_limited=False)
            return result
        return "error"

    def process(self, name: str) -> str:
        result = self.check(name)
        with self._lock:
            self.checked += 1
            done = self.checked
            if result == "available":
                self.available += 1

        if result == "available":
            print(f"[{done:,}/{self.total:,}] AVAILABLE: {name}")
            self._write(self.output_fh, name)
        elif result in ("taken", "improper", "rejected", "invalid"):
            print(f"[{done:,}/{self.total:,}] {name}: {result}")
        else:
            print(f"[{done:,}/{self.total:,}] {name}: {result} (will retry next run)")

        if result in DEFINITE and self.state_fh is not None:
            self._write(self.state_fh, name)

        if self.args.progress_every and done % self.args.progress_every == 0:
            elapsed = time.time() - self.started
            rate = done / elapsed if elapsed else 0
            eta = (self.total - done) / rate if rate else 0
            print(f"--- progress: {done:,}/{self.total:,} | available: "
                  f"{self.available} | delay: {self.delay:.1f}s | ETA {_human(eta)} ---")
        return result


def run_single(checker: Checker, args: argparse.Namespace) -> None:
    for name in iter_candidates(args, checker._skip):
        if args.limit and checker.checked >= args.limit:
            break
        checker.process(name)
        if checker.delay:
            time.sleep(checker.delay * random.uniform(0.85, 1.15))


def run_threaded(checker: Checker, args: argparse.Namespace) -> None:
    submitted = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = set()
        for name in iter_candidates(args, checker._skip):
            if args.limit and submitted >= args.limit:
                break
            pending.add(pool.submit(checker.process, name))
            submitted += 1
            if len(pending) >= args.workers * 4:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
                if checker.delay:
                    time.sleep(checker.delay * random.uniform(0.85, 1.15))
        for future in as_completed(pending):
            future.result()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check PSN online ID availability via PSN's own endpoint.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--length", type=int, default=5,
                        help="characters per generated name (PSN min is 5)")
    parser.add_argument("--charset", default=DEFAULT_CHARSET,
                        help="alphabet used to build names")
    parser.add_argument("--from-file", default=None,
                        help="check names from this file instead of generating")
    parser.add_argument("--starts-with", default=None,
                        help="only keep names starting with this")
    parser.add_argument("--ends-with", default=None,
                        help="only keep names ending with this")
    parser.add_argument("--vowel-positions", default="",
                        help="comma-separated 1-based positions, any must hold a vowel")
    parser.add_argument("--vowels", default="aeiou",
                        help="characters treated as vowels")
    parser.add_argument("--pattern", default=None,
                        help="positional pattern: literal chars, '?'=any, "
                             "'V'=vowel, 'C'=consonant (e.g. dVCVs for 'davis')")
    parser.add_argument("--output", default="available_ids.txt",
                        help="file available IDs are appended to")
    parser.add_argument("--state", default="checked_ids.txt",
                        help="file used to remember checked IDs for resuming")
    parser.add_argument("--no-resume", action="store_true",
                        help="do not read or write the state file")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many names (0 = no limit)")
    parser.add_argument("--workers", type=int, default=1,
                        help="concurrent workers; >1 is faster but risks blocks")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="base seconds between requests (adaptive)")
    parser.add_argument("--min-delay", type=float, default=0.0)
    parser.add_argument("--max-delay", type=float, default=30.0)
    parser.add_argument("--retry-wait", type=float, default=5.0,
                        help="seconds to wait after a 429/5xx before retrying")
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--progress-every", type=int, default=100,
                        help="print a progress line every N names")
    parser.add_argument("--allow-short", action="store_true",
                        help="allow --length < 5 (PSN rejects these with 406)")
    parser.add_argument("--dry-run", type=int, default=0, metavar="N",
                        help="print the first N generated names and exit")
    args = parser.parse_args(argv)
    args.vowel_positions = [int(p) for p in args.vowel_positions.replace(",", " ").split()]
    args.vowels = args.vowels.lower()
    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.length < 1:
        print("error: --length must be >= 1", file=sys.stderr)
        return 2
    if not args.charset and not args.from_file:
        print("error: --charset must not be empty", file=sys.stderr)
        return 2
    if args.output == args.state and not args.no_resume:
        print("error: --output and --state must differ", file=sys.stderr)
        return 2
    if args.pattern and not args.from_file and len(args.pattern) != args.length:
        print("error: --pattern length must equal --length", file=sys.stderr)
        return 2
    if not args.from_file and args.length < MIN_PSN_LENGTH and not args.allow_short:
        print(
            f"error: PSN rejects every online ID shorter than {MIN_PSN_LENGTH} "
            f"characters with HTTP 406.\n"
            f"       Generating {args.length}-character names would only ever "
            f"produce 'rejected'.\n"
            f"       Use --length {MIN_PSN_LENGTH} (or more), or pass "
            f"--allow-short to proceed anyway.",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        if args.from_file:
            print(f"Dry run: first {args.dry_run} name(s) from {args.from_file}")
        else:
            print(f"Dry run: length={args.length}, charset={args.charset!r}, "
                  f"starts-with={args.starts_with!r}, ends-with={args.ends_with!r}, "
                  f"vowel-positions={args.vowel_positions}, pattern={args.pattern!r}")
        for index, name in enumerate(iter_candidates(args, set()), 1):
            print(name)
            if index >= args.dry_run:
                break
        return 0

    skip = set() if args.no_resume else Checker.load_state(args.state)
    total = total_candidates(args, skip)

    print("PSN online ID availability checker")
    if args.from_file:
        print(f"  candidates   : {total:,} (from {args.from_file})")
    else:
        print(f"  combinations : {total:,} (length={args.length}, charset={args.charset!r})")
        if args.starts_with or args.ends_with or args.vowel_positions or args.pattern:
            print(f"  filter       : starts-with={args.starts_with!r}, "
                  f"ends-with={args.ends_with!r}, "
                  f"vowel-positions={args.vowel_positions}, "
                  f"pattern={args.pattern!r}")
    print(f"  results file : {Path(args.output).resolve()}")
    print(f"  resume       : {'disabled' if args.no_resume else f'{len(skip):,} already checked'}")
    print(f"  workers      : {args.workers}, base delay: {args.delay}s\n")

    checker = Checker(args, skip)
    try:
        if args.workers > 1:
            run_threaded(checker, args)
        else:
            run_single(checker, args)
    except KeyboardInterrupt:
        print("\nInterrupted -- progress saved. Re-run to resume.")
    finally:
        checker.close()

    print(f"\nDone. Checked {checker.checked:,} name(s), found {checker.available:,} available.")
    print(f"Available IDs: {Path(args.output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
