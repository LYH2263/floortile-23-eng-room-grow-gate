#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""房间加长片数抬升校验（不落库）。

对种子「客餐厅」配种子「600x600」砖依次断言：
  ① 钉选：长边 6.0 时不落库测算 order_count 恰为 81
  ② 加长：长边改成 7.0 后再测 order_count 必须 > 81
  ③ 恢复：长边改回 6.0 后三测 order_count 必须再次 == 81
公式与种子砖边一律不动；任一步失败即非零退出并在输出里标出步骤名。

监听地址
--------
默认 http://localhost:9600 （docker compose 把后端 9600 映射到宿主机 9600，见 README）。
可用环境变量覆盖：
  FLOORTILE_BASE_URL      服务根地址（默认 http://localhost:9600）
  FLOORTILE_WAIT_SECONDS  等待服务可用的秒数（默认 30）

请求顺序（全部走 HTTP；三次测算都不落库：save 缺省为 false，且逐次断言响应 run_id 为 null）
------------------------------------------------------------------------------------------
  1. GET   /api/health                         等待服务可用
  2. GET   /api/rooms                          按名称「客餐厅」定位种子房间
  3. GET   /api/tiles                          按名称「600x600」定位种子砖，并核对砖边为 0.6x0.6
  4. PATCH /api/rooms/{id}  {"length": 6.0}    仅当长边当前不是 6.0 时的前置复位（幂等，保证脚本可连续执行）
  5. GET   /api/estimate?room_id=R&tile_id=T   [钉选] order_count 必须恰为 81
  6. PATCH /api/rooms/{id}  {"length": 7.0}    [加长] 长边 6.0 -> 7.0
  7. GET   /api/estimate?room_id=R&tile_id=T   [加长] order_count 必须 > 81
  8. PATCH /api/rooms/{id}  {"length": 6.0}    [恢复] 长边改回 6.0（无论加长成败都会执行）
  9. GET   /api/estimate?room_id=R&tile_id=T   [恢复] order_count 必须再次 == 81

注：种子客餐厅 6.0 x 4.5，长边字段为 length；脚本按 max(length, width) 动态判定长边字段，
故 PATCH 的字段名随实际数据而定（上例为 length）。

退出码
------
  0  钉选/加长/恢复三步全部通过
  1  [钉选] 失败
  2  [加长] 失败
  3  [恢复] 失败
  4  前置失败（服务不可达 / 种子数据缺失 / 种子砖边被改动 / 脚本内部错误）
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("FLOORTILE_BASE_URL", "http://localhost:9600").rstrip("/")
WAIT_SECONDS = float(os.environ.get("FLOORTILE_WAIT_SECONDS", "30"))
HTTP_TIMEOUT = 10

ROOM_NAME = "客餐厅"
TILE_NAME = "600x600"
TILE_SIDE = 0.6
BASE_LONG = 6.0
LONGER_LONG = 7.0
EXPECTED_BASE_COUNT = 81

EXIT_PIN = 1
EXIT_LENGTHEN = 2
EXIT_RESTORE = 3
EXIT_PREFLIGHT = 4


class CheckFailure(Exception):
    """某个校验步骤失败；step 为步骤名（钉选/加长/恢复/前置），code 为退出码。"""

    def __init__(self, step, code, message):
        super().__init__(message)
        self.step = step
        self.code = code


def http(method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def describe(exc):
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        return f"HTTP {exc.code} {detail}".strip()
    return f"{type(exc).__name__}: {exc}"


def wait_for_service():
    deadline = time.monotonic() + WAIT_SECONDS
    last = None
    while True:
        try:
            http("GET", "/api/health")
            return
        except Exception as exc:  # 服务未就绪的任何错误都重试，直到超时
            last = exc
            if time.monotonic() >= deadline:
                raise CheckFailure(
                    "前置", EXIT_PREFLIGHT,
                    f"服务在 {WAIT_SECONDS:.0f}s 内不可用（GET {BASE_URL}/api/health）：{describe(last)}",
                )
            time.sleep(1)


def patch_room(room_id, fields, step, code):
    try:
        updated = http("PATCH", f"/api/rooms/{room_id}", fields)
    except Exception as exc:
        raise CheckFailure(step, code, f"PATCH /api/rooms/{room_id} {fields} 失败：{describe(exc)}")
    for key, val in fields.items():
        if abs(updated.get(key, 0) - val) > 1e-9:
            raise CheckFailure(step, code, f"PATCH 后房间 {key}={updated.get(key)}，期望 {val}")
    return updated


def estimate(room_id, tile_id, step, code):
    # 不落库：save 缺省为 false；并断言 run_id 为 null 作为不落库证据
    try:
        r = http("GET", f"/api/estimate?room_id={room_id}&tile_id={tile_id}")
    except Exception as exc:
        raise CheckFailure(step, code, f"GET /api/estimate 失败：{describe(exc)}")
    if r.get("run_id") is not None:
        raise CheckFailure(step, code, f"测算要求不落库，但响应 run_id={r.get('run_id')}")
    return r


def preflight():
    """等待服务、定位种子房间与种子砖、核对砖边、必要时把长边复位到 6.0。"""
    wait_for_service()
    try:
        rooms = http("GET", "/api/rooms")["items"]
        tiles = http("GET", "/api/tiles")["items"]
    except Exception as exc:
        raise CheckFailure("前置", EXIT_PREFLIGHT, f"读取种子数据失败：{describe(exc)}")

    room = next((r for r in rooms if r.get("name") == ROOM_NAME), None)
    tile = next((t for t in tiles if t.get("name") == TILE_NAME), None)
    if room is None:
        raise CheckFailure("前置", EXIT_PREFLIGHT, f"找不到种子房间「{ROOM_NAME}」")
    if tile is None:
        raise CheckFailure("前置", EXIT_PREFLIGHT, f"找不到种子砖「{TILE_NAME}」")
    if abs(tile["tile_l"] - TILE_SIDE) > 1e-9 or abs(tile["tile_w"] - TILE_SIDE) > 1e-9:
        raise CheckFailure(
            "前置", EXIT_PREFLIGHT,
            f"种子砖「{TILE_NAME}」砖边为 {tile['tile_l']}x{tile['tile_w']}，"
            f"不是约定的 {TILE_SIDE}x{TILE_SIDE}；脚本不改种子砖边凑数，终止",
        )

    long_field = "length" if room["length"] >= room["width"] else "width"
    if abs(room[long_field] - BASE_LONG) > 1e-9:
        print(f"[前置] 长边 {long_field} 当前为 {room[long_field]}，先复位为 {BASE_LONG}（保证脚本可连续执行）")
        patch_room(room["id"], {long_field: BASE_LONG}, "前置", EXIT_PREFLIGHT)
    return room, tile, long_field


def main():
    print(f"目标服务（监听地址）: {BASE_URL}")
    print("请求顺序: GET /api/health → GET /api/rooms → GET /api/tiles → [必要时 PATCH 复位]"
          " → ①estimate[钉选] → ②PATCH 7.0 + estimate[加长] → ③PATCH 6.0 + estimate[恢复]")

    try:
        room, tile, long_field = preflight()
    except CheckFailure as e:
        print(f"[{e.step}] FAIL: {e}", file=sys.stderr)
        return e.code

    rid, tid = room["id"], tile["id"]
    print(f"[前置] OK 种子房间「{ROOM_NAME}」id={rid}（长边字段 {long_field}={BASE_LONG}），"
          f"种子砖「{TILE_NAME}」id={tid}（{TILE_SIDE}x{TILE_SIDE}）")

    # ① 钉选
    try:
        r1 = estimate(rid, tid, "钉选", EXIT_PIN)
        if r1["order_count"] != EXPECTED_BASE_COUNT:
            raise CheckFailure(
                "钉选", EXIT_PIN,
                f"order_count={r1['order_count']}，期望恰为 {EXPECTED_BASE_COUNT}"
                f"（raw_count={r1.get('raw_count')}, waste_pct={r1.get('waste_pct')}）",
            )
    except CheckFailure as e:
        print(f"[{e.step}] FAIL: {e}", file=sys.stderr)
        return e.code
    print(f"[钉选] OK order_count={r1['order_count']} == {EXPECTED_BASE_COUNT}"
          f"（raw_count={r1['raw_count']}, waste_pct={r1['waste_pct']}, run_id=null 不落库）")

    # ② 加长（无论成败，③都会把长边改回 6.0，保证脚本可连续执行）
    lengthen_error = None
    try:
        patch_room(rid, {long_field: LONGER_LONG}, "加长", EXIT_LENGTHEN)
        r2 = estimate(rid, tid, "加长", EXIT_LENGTHEN)
        if r2["order_count"] <= EXPECTED_BASE_COUNT:
            lengthen_error = CheckFailure(
                "加长", EXIT_LENGTHEN,
                f"长边 {long_field} {BASE_LONG}->{LONGER_LONG} 后 order_count={r2['order_count']}，"
                f"期望 > {EXPECTED_BASE_COUNT}",
            )
        else:
            print(f"[加长] OK 长边 {long_field} {BASE_LONG}->{LONGER_LONG}，"
                  f"order_count={r2['order_count']} > {EXPECTED_BASE_COUNT}")
    except CheckFailure as e:
        lengthen_error = e

    # ③ 恢复
    restore_error = None
    try:
        patch_room(rid, {long_field: BASE_LONG}, "恢复", EXIT_RESTORE)
        if lengthen_error is None:
            r3 = estimate(rid, tid, "恢复", EXIT_RESTORE)
            if r3["order_count"] != EXPECTED_BASE_COUNT:
                restore_error = CheckFailure(
                    "恢复", EXIT_RESTORE,
                    f"长边改回 {BASE_LONG} 后 order_count={r3['order_count']}，"
                    f"期望再次 == {EXPECTED_BASE_COUNT}",
                )
            else:
                print(f"[恢复] OK 长边 {long_field} 改回 {BASE_LONG}，"
                      f"order_count={r3['order_count']} == {EXPECTED_BASE_COUNT}")
    except CheckFailure as e:
        restore_error = e

    if lengthen_error is not None:
        print(f"[加长] FAIL: {lengthen_error}", file=sys.stderr)
        if restore_error is not None:
            print(f"[恢复] 复位也未成功: {restore_error}", file=sys.stderr)
        return lengthen_error.code
    if restore_error is not None:
        print(f"[恢复] FAIL: {restore_error}", file=sys.stderr)
        return restore_error.code

    print("PASS: 钉选/加长/恢复 三步全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CheckFailure as e:
        print(f"[{e.step}] FAIL: {e}", file=sys.stderr)
        sys.exit(e.code)
    except Exception as e:  # 脚本自身问题一律按前置失败处理
        print(f"[前置] FAIL: 脚本内部错误 {describe(e)}", file=sys.stderr)
        sys.exit(EXIT_PREFLIGHT)
