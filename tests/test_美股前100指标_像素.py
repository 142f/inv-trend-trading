"""Pixel regression for the QQQ/SPY board. Uses the shipped page and Playwright.

Baselines are CSS pixels from Playwright driving the installed Chrome at device_scale_factor 1.
A difference of one pixel fails. Set UPDATE_PIXEL_BASELINES=1 to rewrite them.
"""
from __future__ import annotations

import os
import zlib
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "fixtures" / "美股前100像素"
board = SourceFileLoader(
    "us_top100_pixels", str(ROOT / "scripts" / "美股前100指标.py"),
).load_module()

SCENES = (
    ("completed-1440", False, 1440, 900),
    ("partial-1440", True, 1440, 900),
    ("completed-412", False, 412, 900),
    ("partial-412", True, 412, 900),
)


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    distance_left = abs(estimate - left)
    distance_up = abs(estimate - up)
    distance_upper_left = abs(estimate - upper_left)
    if distance_left <= distance_up and distance_left <= distance_upper_left:
        return left
    if distance_up <= distance_upper_left:
        return up
    return upper_left


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """Return width, height, and tightly packed RGBA for an 8-bit PNG."""

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    position = 8
    width = height = color_type = None
    compressed = bytearray()
    while position < len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        kind = data[position + 4:position + 8]
        chunk = data[position + 8:position + 8 + length]
        position += 12 + length
        if kind == b"IHDR":
            width = int.from_bytes(chunk[0:4], "big")
            height = int.from_bytes(chunk[4:8], "big")
            if chunk[8] != 8 or chunk[12] != 0:
                raise ValueError("only 8-bit non-interlaced png is supported")
            color_type = chunk[9]
        elif kind == b"IDAT":
            compressed += chunk
        elif kind == b"IEND":
            break
    channels = {2: 3, 6: 4}[color_type]
    raw = zlib.decompress(bytes(compressed))
    stride = width * channels
    rows = []
    cursor = 0
    previous = bytearray(stride)
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor:cursor + stride])
        cursor += stride
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                value = row[index]
            elif filter_type == 1:
                value = row[index] + left
            elif filter_type == 2:
                value = row[index] + up
            elif filter_type == 3:
                value = row[index] + ((left + up) // 2)
            elif filter_type == 4:
                value = row[index] + _paeth(left, up, upper_left)
            else:
                raise ValueError(f"unknown png filter {filter_type}")
            row[index] = value & 255
        previous = row
        rows.append(bytes(row))
    pixels = b"".join(rows)
    if channels == 3:
        packed = bytearray()
        for index in range(0, len(pixels), 3):
            packed += pixels[index:index + 3] + b"\xff"
        pixels = bytes(packed)
    return width, height, pixels


def count_diff_pixels(left: bytes, right: bytes) -> int:
    left_size = decode_png(left)
    right_size = decode_png(right)
    if left_size[:2] != right_size[:2]:
        raise AssertionError(f"size {left_size[:2]} != {right_size[:2]}")
    count = 0
    for index in range(0, len(left_size[2]), 4):
        if left_size[2][index:index + 4] != right_size[2][index:index + 4]:
            count += 1
    return count


def test_pixel_compare_rejects_one_changed_pixel():
    width, height = 2, 1
    raw = bytearray()
    for _ in range(height):
        raw.append(0)
        raw += b"\x10\x20\x30\xff" * width
    red = bytes(raw)
    changed = bytearray(raw)
    changed[1] = 0x11

    def pack(body: bytes) -> bytes:
        def chunk(kind: bytes, payload: bytes) -> bytes:
            import struct
            crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

        header = b"\x00\x00\x00\x02\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(body))
            + chunk(b"IEND", b"")
        )

    assert count_diff_pixels(pack(red), pack(bytes(changed))) == 1


def _payload(partial: bool) -> dict:
    shared = {
        "as_of": "2026-10-26",
        "close": 180.25,
        "adjusted_close": 90.125,
        "adjustment_factor": 0.5,
        "bias": "多",
        "pairs": {"10_20": "多", "10_55": "多", "20_55": "空"},
        "sma_bias": "混",
        "macd": "多",
        "adx": 27.5,
        "relative_volume": 0.8852,
        "partial": partial,
    }
    mu = {
        **shared,
        "symbol": "MU",
        "funds": "QQQ,SPY",
        "qqq_rank": 4,
        "spy_rank": 16,
        "sma": {"5": 1, "10": 1, "20": 1, "55": 1, "120": 1},
        "ema": {"10": 1, "20": 1, "55": 1, "144": 1, "169": 1},
        "warmup_ok": True,
        "bar_count": 400,
    }
    hona = {
        **shared,
        "symbol": "HONA",
        "funds": "QQQ",
        "qqq_rank": 76,
        "spy_rank": None,
        "sma": {"5": 1, "10": 1, "20": 1, "55": 1, "120": None},
        "ema": {"10": 1, "20": 1, "55": 1, "144": None, "169": None},
        "relative_volume": None,
        "warmup_ok": False,
        "bar_count": 93,
        "bias": "",
        "sma_bias": "",
        "macd": "",
    }
    return {
        "partial": partial,
        "rows": [mu, hona],
        "failed": [{"symbol": "SPCX", "funds": "QQQ", "error": "未登记"}],
        "incomplete": [{"symbol": "HONA", "bar_count": 93, "reason": "warmup<169"}],
        "universe": "QQQ与SPY 前100，按代码去重，未按公司合并",
        "price_basis": "provider_adjusted_close",
        "data_cutoff": "2026-10-26T20:00:00Z",
        "as_of": "2026-10-26",
    }


def _html(partial: bool) -> str:
    return board._page(_payload(partial), {
        "provider": "yahoo_chart",
        "data_cutoff": "2026-10-26T20:00:00Z",
        "price_basis": "provider_adjusted_close",
        "config_hash": "abc123",
        "content_hash": "def456",
        "script_git_commit": "34dd62d",
    })


def _shoot(page, html: str) -> bytes:
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.set_content(html, wait_until="load")
    page.evaluate("document.fonts.ready")
    page.locator("#count").wait_for()
    options = {"full_page": False, "animations": "disabled", "caret": "hide", "type": "png"}
    first = page.screenshot(**options)
    second = page.screenshot(**options)
    assert errors == []
    assert count_diff_pixels(first, second) == 0
    return first


def test_board_pixels_match_baseline():
    sync_api = pytest.importorskip("playwright.sync_api")
    update = os.environ.get("UPDATE_PIXEL_BASELINES") == "1"
    if update:
        BASELINE.mkdir(parents=True, exist_ok=True)
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--force-color-profile=srgb"],
        )
        try:
            for name, partial, width, height in SCENES:
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=1,
                    color_scheme="light",
                    reduced_motion="reduce",
                )
                page = context.new_page()
                actual = _shoot(page, _html(partial))
                if partial:
                    display = page.locator("#banner").evaluate(
                        "element => getComputedStyle(element).display",
                    )
                    assert display == "inline-block"
                    assert "盘中快照" in page.locator("#banner").inner_text()
                else:
                    assert page.locator("#banner").count() == 0
                context.close()
                target = BASELINE / f"{name}.png"
                if update:
                    target.write_bytes(actual)
                    continue
                assert target.is_file(), f"missing baseline {target.name}"
                difference = count_diff_pixels(target.read_bytes(), actual)
                assert difference == 0, f"{name} differs by {difference} pixels"
        finally:
            browser.close()
