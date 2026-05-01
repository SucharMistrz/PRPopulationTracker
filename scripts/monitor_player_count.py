#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
import tempfile
import time
import urllib.request
import json
from datetime import datetime, time as datetime_time
from pathlib import Path
from zoneinfo import ZoneInfo


API_URL = "https://servers.realitymod.com/api/ServerInfo"
DEFAULT_OUTPUT_FILE = Path("data/player_counts.tsv")
DEFAULT_TIMEZONE = "Europe/Warsaw"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor Project Reality human player count."
    )

    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=30,
        help="How often to fetch data. Default: 30 seconds.",
    )

    parser.add_argument(
        "--duration-minutes",
        type=int,
        default=None,
        help=(
            "Maximum number of minutes to run. "
            "If omitted, the script runs until --stop-at-local-time."
        ),
    )

    parser.add_argument(
        "--stop-at-local-time",
        default=None,
        help=(
            "Local time when monitoring should stop, in HH:MM or HH:MM:SS format. "
            "Example: 21:00:00"
        ),
    )

    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Timezone used for timestamps and stop time. Default: {DEFAULT_TIMEZONE}",
    )

    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_FILE),
        help=f"Output TSV file. Default: {DEFAULT_OUTPUT_FILE}",
    )

    return parser.parse_args()


def fetch_server_info() -> dict:
    request = urllib.request.Request(
        API_URL,
        headers={
            "User-Agent": "pr-player-count-monitor/1.0"
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"API returned HTTP {response.status}")

        raw = response.read().decode("utf-8")

    data = json.loads(raw)

    if not isinstance(data, dict):
        raise RuntimeError("API response is not a JSON object")

    if not isinstance(data.get("servers"), list):
        raise RuntimeError("API response does not contain a 'servers' list")

    return data


def fetch_server_info_with_retries(max_attempts: int = 3) -> dict:
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return fetch_server_info()
        except Exception as exc:
            last_error = exc

            if attempt < max_attempts:
                wait_seconds = attempt * 5
                print(
                    f"Fetch attempt {attempt} failed: {exc}. "
                    f"Retrying in {wait_seconds} seconds...",
                    file=sys.stderr,
                )
                time.sleep(wait_seconds)

    raise RuntimeError(f"All fetch attempts failed. Last error: {last_error}")


def is_human_player(player: object) -> bool:
    if not isinstance(player, dict):
        return False

    value = player.get("isAI")

    if isinstance(value, bool):
        return value is False

    return str(value).strip() == "0"


def count_human_players(api_data: dict) -> int:
    human_players = 0

    for server in api_data["servers"]:
        if not isinstance(server, dict):
            continue

        players = server.get("players", [])

        if not isinstance(players, list):
            continue

        for player in players:
            if is_human_player(player):
                human_players += 1

    return human_players


def parse_stop_time(value: str) -> datetime_time:
    value = value.strip()

    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            pass

    raise ValueError(
        f"Invalid --stop-at-local-time value: {value!r}. "
        "Use HH:MM or HH:MM:SS."
    )


def should_stop(
    *,
    started_monotonic: float,
    duration_minutes: int | None,
    stop_at_local_time: datetime_time | None,
    timezone: ZoneInfo,
) -> bool:
    now_monotonic = time.monotonic()

    if duration_minutes is not None:
        max_seconds = duration_minutes * 60

        if now_monotonic - started_monotonic >= max_seconds:
            return True

    if stop_at_local_time is not None:
        now_local = datetime.now(timezone)

        stop_datetime = datetime.combine(
            now_local.date(),
            stop_at_local_time,
            tzinfo=timezone,
        )

        if now_local >= stop_datetime:
            return True

    return False


def seconds_until_next_stop(
    *,
    started_monotonic: float,
    duration_minutes: int | None,
    stop_at_local_time: datetime_time | None,
    timezone: ZoneInfo,
) -> float | None:
    limits: list[float] = []

    if duration_minutes is not None:
        max_seconds = duration_minutes * 60
        elapsed_seconds = time.monotonic() - started_monotonic
        limits.append(max_seconds - elapsed_seconds)

    if stop_at_local_time is not None:
        now_local = datetime.now(timezone)

        stop_datetime = datetime.combine(
            now_local.date(),
            stop_at_local_time,
            tzinfo=timezone,
        )

        limits.append((stop_datetime - now_local).total_seconds())

    if not limits:
        return None

    return max(0.0, min(limits))


def make_entry(human_players: int, timezone: ZoneInfo) -> str:
    timestamp = datetime.now(timezone).isoformat(timespec="seconds")

    # Player count is intentionally first.
    return f"{human_players}\t{timestamp}\n"


def parse_entry_for_sorting(line: str) -> tuple[int, datetime | None, str]:
    """
    Expected line format:
    players<TAB>timestamp

    Example:
    423    2026-05-01T17:00:00+02:00

    Returns data for sorting newest-first.
    Invalid lines are preserved, but pushed to the bottom.
    """
    stripped = line.rstrip("\n")
    parts = stripped.split("\t")

    if len(parts) < 2:
        return (1, None, stripped)

    timestamp_text = parts[1].strip()

    try:
        timestamp = datetime.fromisoformat(timestamp_text)
    except ValueError:
        return (1, None, stripped)

    return (0, timestamp, stripped)


def sort_entries_newest_first(lines: list[str]) -> list[str]:
    valid_lines: list[tuple[datetime, str]] = []
    invalid_lines: list[str] = []

    for line in lines:
        if not line.strip():
            continue

        invalid_flag, timestamp, stripped = parse_entry_for_sorting(line)

        if invalid_flag == 0 and timestamp is not None:
            valid_lines.append((timestamp, stripped))
        else:
            invalid_lines.append(stripped)

    valid_lines.sort(key=lambda item: item[0], reverse=True)

    sorted_lines = [f"{line}\n" for _, line in valid_lines]
    sorted_lines.extend(f"{line}\n" for line in invalid_lines)

    return sorted_lines


def read_existing_lines(path: Path) -> list[str]:
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8", newline="") as file:
        return file.readlines()


def write_lines_atomically(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    sorted_lines = sort_entries_newest_first(lines)

    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        delete=False,
        dir=path.parent,
    ) as temp_file:
        temp_file.writelines(sorted_lines)
        temp_path = Path(temp_file.name)

    temp_path.replace(path)


def run_monitor(
    *,
    output_file: Path,
    interval_seconds: int,
    duration_minutes: int | None,
    stop_at_local_time: datetime_time | None,
    timezone: ZoneInfo,
) -> int:
    if interval_seconds < 1:
        raise ValueError("--interval-seconds must be at least 1")

    if duration_minutes is None and stop_at_local_time is None:
        raise ValueError(
            "Provide either --duration-minutes or --stop-at-local-time."
        )

    started_monotonic = time.monotonic()
    existing_lines = read_existing_lines(output_file)
    new_lines: list[str] = []

    print(f"Writing output to: {output_file}")
    print(f"Timezone: {timezone.key}")
    print(f"Interval: {interval_seconds} seconds")

    if duration_minutes is not None:
        print(f"Maximum duration: {duration_minutes} minutes")

    if stop_at_local_time is not None:
        print(f"Stop at local time: {stop_at_local_time}")

    sample_number = 0

    while True:
        if should_stop(
            started_monotonic=started_monotonic,
            duration_minutes=duration_minutes,
            stop_at_local_time=stop_at_local_time,
            timezone=timezone,
        ):
            break

        sample_number += 1

        try:
            api_data = fetch_server_info_with_retries()
            human_players = count_human_players(api_data)
            entry = make_entry(human_players, timezone)
            new_lines.append(entry)

            # Keep file updated during the run as well.
            # GitHub still commits only once at the end.
            write_lines_atomically(output_file, new_lines + existing_lines)

            print(f"Sample {sample_number}: {entry.strip()}")

        except Exception as exc:
            print(f"Sample {sample_number} failed: {exc}", file=sys.stderr)

        seconds_left = seconds_until_next_stop(
            started_monotonic=started_monotonic,
            duration_minutes=duration_minutes,
            stop_at_local_time=stop_at_local_time,
            timezone=timezone,
        )

        if seconds_left is not None and seconds_left <= 0:
            break

        sleep_seconds = interval_seconds

        if seconds_left is not None:
            sleep_seconds = min(sleep_seconds, seconds_left)

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    write_lines_atomically(output_file, new_lines + existing_lines)

    print(f"Finished. New samples collected: {len(new_lines)}")
    return 0


def main() -> int:
    args = parse_args()

    timezone = ZoneInfo(args.timezone)
    output_file = Path(args.output)

    stop_at_local_time = None

    if args.stop_at_local_time:
        stop_at_local_time = parse_stop_time(args.stop_at_local_time)

    return run_monitor(
        output_file=output_file,
        interval_seconds=args.interval_seconds,
        duration_minutes=args.duration_minutes,
        stop_at_local_time=stop_at_local_time,
        timezone=timezone,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
