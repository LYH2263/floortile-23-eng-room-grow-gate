#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
房间加长片数抬升校验脚本（不落库测算）

校验对象：种子房间「客餐厅」(6.0m x 4.5m) + 种子砖「600x600」(0.6m x 0.6m)，
损耗取服务端设置默认值 8%。面积法 order_count = ceil(ceil(面积/单片面积) * 1.08)。

本脚本只做校验：
  * 不修改任何计算逻辑（tile_math 公式一行不动）；
  * 不修改种子砖边长（tiles 表只读，并断言 0.6 x 0.6）；
  * 测算请求一律不带 save（save 默认 false），并断言响应 run_id 为 null、
    calc_runs 表行数全程不增加。

监听地址
  默认 http://127.0.0.1:9600 （与 docker-compose 映射的后端端口 9600 一致）。
  可用环境变量覆盖：
    BASE_URL       服务基址，默认 http://127.0.0.1:9600
    DATA_DIR       服务使用的 SQLite 目录，默认 <仓库>/backend/data
    DB_PATH        直接指定 app.db 全路径（优先级高于 DATA_DIR）
    HEALTH_TIMEOUT 健康轮询超时秒数，默认 60

请求 / 操作顺序
  0. 轮询 GET  {BASE_URL}/api/health                 等待服务可用
  1. GET  {BASE_URL}/api/rooms                       按名称定位种子「客餐厅」
  2. GET  {BASE_URL}/api/tiles                       按名称定位种子「600x600」并校验边长
  3. 直接写 SQLite：将该房间 length 钉回种子值 6.0（仅在与 6.0 不一致时）
  4. GET  {BASE_URL}/api/estimate?room_id=R&tile_id=T
     【钉选】长边 6.0：order_count 必须恰为 81
  5. 直接写 SQLite：UPDATE rooms SET length=7.0
  6. GET  {BASE_URL}/api/estimate?room_id=R&tile_id=T
     【加长】长边 7.0：order_count 必须大于 81
  7. 直接写 SQLite：UPDATE rooms SET length=6.0
  8. GET  {BASE_URL}/api/estimate?room_id=R&tile_id=T
     【恢复】长边 6.0：order_count 必须再次恰为 81

退出码：全部断言通过为 0；任一阶段失败为 1，输出中以
        「失败阶段：钉选 / 加长 / 恢复」明确标出失败步骤。
脚本结束（含失败）时总会把房间 length 收尾恢复为 6.0，可连续重复执行。

用法
  先启动后端，例如：
    cd backend && uvicorn app.main:app --host 127.0.0.1 --port 9600
    # 或： docker compose up --build （此时需让本脚本与容器使用同一 DATA_DIR）
  然后：
    python3 scripts/check_length_raise.py
"""

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:9600").rstrip("/")
HEALTH_TIMEOUT = float(os.environ.get("HEALTH_TIMEOUT", "60"))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_ROOM_NAME = "客餐厅"
SEED_TILE_NAME = "600x600"
SEED_LENGTH = 6.0
RAISED_LENGTH = 7.0
EXPECTED_ORDER = 81
EPS = 1e-9


def fail(stage: str, msg: str):
    print(f"✗ 失败阶段：【{stage}】{msg}")
    raise SystemExit(1)


def resolve_db_path() -> str:
    explicit = os.environ.get("DB_PATH")
    if explicit:
        return explicit
    data_dir = os.environ.get("DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "app.db")
    return os.path.normpath(os.path.join(SCRIPT_DIR, "..", "backend", "data", "app.db"))


def http_get(path: str, params: dict | None = None, timeout: float = 10):
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"GET {path} -> HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {path} -> 连接失败: {exc.reason}") from exc


def wait_for_service() -> None:
    print(f"[准备] 等待服务可用：{BASE_URL}/api/health（超时 {HEALTH_TIMEOUT:g}s）")
    deadline = time.monotonic() + HEALTH_TIMEOUT
    last_err = ""
    while time.monotonic() < deadline:
        try:
            data = http_get("/api/health", timeout=3)
            if data.get("ok") is True:
                print(f"[准备] 服务可用：{data}")
                return
            last_err = f"health 响应异常：{data}"
        except Exception as exc:  # noqa: BLE001 - 轮询期间任何错误都重试
            last_err = str(exc)
        time.sleep(0.5)
    fail("准备", f"在 {HEALTH_TIMEOUT:g}s 内服务未就绪：{last_err}")


def find_seed_room() -> dict:
    items = http_get("/api/rooms").get("items", [])
    for row in items:
        if row.get("name") == SEED_ROOM_NAME:
            return row
    names = ", ".join(str(r.get("name")) for r in items) or "<空>"
    fail("准备", f"未找到种子房间「{SEED_ROOM_NAME}」，现有房间：{names}")


def find_seed_tile() -> dict:
    items = http_get("/api/tiles").get("items", [])
    for row in items:
        if row.get("name") == SEED_TILE_NAME:
            return row
    names = ", ".join(str(r.get("name")) for r in items) or "<空>"
    fail("准备", f"未找到种子砖「{SEED_TILE_NAME}」，现有砖：{names}")


def set_room_length(db_path: str, room_id: int, length: float) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE rooms SET length=? WHERE id=?", (length, room_id))
        conn.commit()
        if conn.total_changes == 0:
            raise RuntimeError("UPDATE 未命中任何行")
    finally:
        conn.close()


def count_runs(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM calc_runs").fetchone()[0])
    finally:
        conn.close()


def estimate(room_id: int, tile_id: int) -> dict:
    # 刻意不传 save：默认 false，要求不落库
    return http_get(
        "/api/estimate",
        {"room_id": room_id, "tile_id": tile_id},
    )


def assert_no_save(stage: str, result: dict) -> None:
    if result.get("run_id") is not None:
        fail(stage, f"测算发生落库，run_id={result.get('run_id')}（要求不落库）")


def main() -> int:
    db_path = resolve_db_path()
    room_id = None
    print(f"[准备] BASE_URL = {BASE_URL}")
    print(f"[准备] SQLite  = {db_path}")

    try:
        wait_for_service()

        room = find_seed_room()
        tile = find_seed_tile()
        room_id = int(room["id"])
        tile_id = int(tile["id"])

        if room.get("data_quality") == "dirty":
            fail("准备", f"房间「{SEED_ROOM_NAME}」被标记 dirty，服务端会拒绝测算")
        if abs(float(room["width"]) - 4.5) > EPS:
            fail("准备", f"种子房间 width 被改动：{room['width']}（期望 4.5），拒绝凑数")
        if abs(float(tile["tile_l"]) - 0.6) > EPS or abs(float(tile["tile_w"]) - 0.6) > EPS:
            fail(
                "准备",
                f"种子砖「{SEED_TILE_NAME}」边长被改动：{tile['tile_l']}x{tile['tile_w']}"
                "（期望 0.6x0.6），不许改砖边凑数",
            )

        try:
            runs_before = count_runs(db_path)
        except sqlite3.Error as exc:
            fail("准备", f"无法打开/读取 SQLite {db_path}：{exc}")

        # 钉选：先把长边钉回种子 6.0，保证可重复执行
        if abs(float(room["length"]) - SEED_LENGTH) > EPS:
            print(f"[钉选] 检测到残留 length={room['length']}，先钉回种子值 {SEED_LENGTH}")
            set_room_length(db_path, room_id, SEED_LENGTH)
        else:
            print(f"[钉选] 长边已是种子值 {SEED_LENGTH}")

        # —— 第一步：钉选 6.0，order_count 必须恰为 81 ——
        r1 = estimate(room_id, tile_id)
        assert_no_save("钉选", r1)
        oc1 = int(r1["order_count"])
        print(f"[钉选] length={SEED_LENGTH} -> raw={r1['raw_count']} "
              f"waste={r1['waste_pct']}% order_count={oc1}（期望 {EXPECTED_ORDER}）")
        if oc1 != EXPECTED_ORDER:
            fail("钉选", f"order_count={oc1}，不等于 {EXPECTED_ORDER}")

        # —— 第二步：加长到 7.0，order_count 必须大于 81 ——
        set_room_length(db_path, room_id, RAISED_LENGTH)
        r2 = estimate(room_id, tile_id)
        assert_no_save("加长", r2)
        oc2 = int(r2["order_count"])
        print(f"[加长] length={RAISED_LENGTH} -> raw={r2['raw_count']} "
              f"waste={r2['waste_pct']}% order_count={oc2}（必须 > {EXPECTED_ORDER}）")
        if not oc2 > EXPECTED_ORDER:
            fail("加长", f"order_count={oc2}，未大于 {EXPECTED_ORDER}，抬升未发生")

        # —— 第三步：恢复 6.0，order_count 必须再次恰为 81 ——
        set_room_length(db_path, room_id, SEED_LENGTH)
        r3 = estimate(room_id, tile_id)
        assert_no_save("恢复", r3)
        oc3 = int(r3["order_count"])
        print(f"[恢复] length={SEED_LENGTH} -> raw={r3['raw_count']} "
              f"waste={r3['waste_pct']}% order_count={oc3}（期望再次等于 {EXPECTED_ORDER}）")
        if oc3 != EXPECTED_ORDER:
            fail("恢复", f"order_count={oc3}，恢复后未回到 {EXPECTED_ORDER}")

        # —— 全局：不落库 ——
        runs_after = count_runs(db_path)
        if runs_after != runs_before:
            fail("不落库校验", f"calc_runs 行数由 {runs_before} 增至 {runs_after}，测算被落库")
        print(f"[校验] calc_runs 行数全程为 {runs_after}，三次测算均未落库")

    finally:
        # 无论成功失败，收尾把长边恢复为种子值，保证脚本可连续执行
        if room_id is not None:
            try:
                set_room_length(db_path, room_id, SEED_LENGTH)
                print(f"[收尾] 房间「{SEED_ROOM_NAME}」length 已恢复为 {SEED_LENGTH}")
            except Exception as exc:  # noqa: BLE001 - 收尾失败只告警
                print(f"[收尾] 警告：恢复 length={SEED_LENGTH} 失败：{exc}", file=sys.stderr)

    print("✓ 全部通过：钉选=81，加长>81，恢复=81，且全程不落库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
