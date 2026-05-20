# 本地 850A Mock 调试步骤

这套本地调试用于在无法访问真实 Danfoss 850A 设备时，验证南向链路：

```text
Gateway.py -> mock_850a_server.py -> /html/xml.cgi -> XML response -> danfoss_850a.py parser
```

## 1. 启动本地 Mock Server

在项目目录执行：

```bash
python3 mock_850a_server.py --host 127.0.0.1 --port 8088 --points mock_850a_points.json
```

服务地址：

```text
http://127.0.0.1:8088/html/xml.cgi
```

## 2. 单独验证 Danfoss 客户端

另开一个终端执行：

```bash
python3 - <<'PY'
from danfoss_850a import Danfoss850AClient, DanfossPointSpec

client = Danfoss850AClient(
    endpoint_url="http://127.0.0.1:8088/html/xml.cgi",
    username="mock_user",
    password="mock_password",
    auth_mode="basic_header",
    min_interval_sec=0,
)

print(client.read_date_time())
values = client.read_values([
    DanfossPointSpec(
        key="ColdStagingControlTemp",
        nodetype=16,
        node=1,
        tag="TP-A1-7007A-01-CHILL02-CO-TI01",
    )
])
print(values["ColdStagingControlTemp"])
PY
```

## 3. 验证 Gateway 采集入口

不覆盖正式 `config.json`，直接通过环境变量指定本地 mock 配置：

```bash
GATEWAY_CONFIG_PATH=config.localmock.json python3 - <<'PY'
import Gateway

print(Gateway.DANFOSS_API_MODE)
print(Gateway.danfoss_850a_enabled())
print(Gateway.fetch_danfoss_value_and_time())
PY
```

期望结果：

```text
850a_xml
True
(数值, datetime)
```

## 4. 跑完整 Gateway Demo

Mock Server 保持运行，然后执行：

```bash
GATEWAY_CONFIG_PATH=config.localmock.json python3 Gateway.py
```

看到下面日志说明南向路径已切到 850A XML mock：

```text
[CFG] Danfoss southbound: 850A XML POST endpoint=http://127.0.0.1:8088/html/xml.cgi
[DATA] ...
```

## 5. 点表说明

`mock_850a_points.json` 是从客户 PDF 点表 `TP-A-0801A-IOL04-V001-20260116 - group.pdf` 抽取出来的 169 个点。

注意：该点表提供的是 BMS `Tag No.` 和 Modbus `Point Address`，不是 Danfoss XML 原生的 `node/cid/vid/tag` 映射。当前本地 mock 使用 `Tag No.` 作为 XML `tag` 来验证程序链路；真实 850A 联调时，还需要客户提供或通过 `read_parm_info/read_device_history_cfg` 获取实际 XML 点位映射。
