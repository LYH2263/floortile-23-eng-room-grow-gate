# 17-floortile（铺地砖）

Floortile — 面积/单片面积向上取整再加损耗百分比

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:4600 |
| API | http://localhost:9600 |

## 主链

房间尺寸+砖规格+损耗 → 片数 → 铺贴预览

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 校验脚本：房间加长片数抬升（不落库）

`scripts/check_room_lengthen.py` 校验「房长边加长 → 订购片数抬升 → 恢复后回落」，三次测算均不落库（`save` 缺省为 `false`，脚本逐次断言响应 `run_id` 为 `null`）：

- **监听地址**：默认 `http://localhost:9600`（docker compose 把后端 9600 映射到宿主机 9600）；可用环境变量 `FLOORTILE_BASE_URL` 覆盖，`FLOORTILE_WAIT_SECONDS` 控制等待服务可用的秒数（默认 30）。
- **请求顺序**：`GET /api/health`（等服务可用）→ `GET /api/rooms` / `GET /api/tiles`（按名定位种子「客餐厅」与「600x600」，并核对砖边 0.6×0.6）→ 长边不是 6.0 时先 `PATCH /api/rooms/{id}` 复位 → ① `GET /api/estimate?room_id=…&tile_id=…` 钉选 `order_count == 81` → ② `PATCH` 长边为 7.0 后再测 `order_count > 81` → ③ `PATCH` 长边回 6.0 后三测 `order_count == 81`。
- **失败退出码**：钉选=1、加长=2、恢复=3、前置（服务不可达/种子缺失/砖边被改）=4；输出里会标出失败步骤名。无论加长成败，脚本都会把长边改回 6.0，因此可连续重复执行。

```bash
python3 scripts/check_room_lengthen.py   # 连续执行两次均应通过
```

配套接口：`PATCH /api/rooms/{id}`，请求体 `{"length": …, "width": …}`（字段均可选，仅更新提供的尺寸），返回更新后的房间。
