# 客户电脑本地 850A 调试操作说明

本文用于现场远程控制客户电脑时使用。公网只用于远程桌面；850A API 调用应在客户电脑本机或客户内网完成，不需要把 850A 直接暴露到公网。

## 1. 确认 850A 地址

优先向客户确认实际地址。如果不确定，按下面顺序试：

```text
http://127.0.0.1/html/xml.cgi
http://172.28.238.109/html/xml.cgi
http://<客户提供的850A IP>/html/xml.cgi
https://<客户提供的850A IP>/html/xml.cgi
```

注意：浏览器直接打开 `/html/xml.cgi` 不一定有正常页面，因为 850A XML API 需要 POST XML 请求。

## 2. 准备项目

把 `NewGateway` 目录放到客户电脑，例如：

```text
C:\NOVO\NewGateway
```

如果客户电脑有 Python，在项目目录运行：

```bat
python -m pip install -r requirements.txt
```

如果不方便安装 Python，则先使用打包好的 `DanfossGatewayIR.exe + config.json` 跑完整网关；但 API 诊断脚本仍推荐用 Python 跑，方便定位问题。

## 3. 配置诊断参数

复制 `config.customer.example.json` 为现场配置，例如：

```text
config.customer.json
```

修改这些字段：

```json
"danfoss_api_mode": "850a_xml",
"danfoss_endpoint_url": "http://<850A IP>/html/xml.cgi",
"danfoss_auth_mode": "basic_header",
"danfoss_username": "<userName>",
"danfoss_password": "<passWord>",
"danfoss_verify_tls": true
```

如果现场是 HTTPS 自签名证书，联调阶段可临时设：

```json
"danfoss_verify_tls": false
```

`danfoss_points` 可以先为空，先验证时间、设备列表和历史配置；确认真实 `cid/vid/tag` 后再填点位。

## 4. 运行 850A API 诊断

Windows 命令行：

```bat
run_850a_diagnostics.bat config.customer.json
```

也可以临时覆盖 endpoint：

```bat
run_850a_diagnostics.bat config.customer.json http://172.28.238.109/html/xml.cgi
```

或直接运行 Python：

```bat
python diagnose_850a.py --config config.customer.json --nodes 1-8,81,82
```

诊断脚本会依次测试：

- TCP 是否能连到 850A 端口
- `read_date_time`
- `read_devices`
- `read_device_history_cfg`，默认节点 `1-8,81,82`
- `read_val`，仅当配置里有 `danfoss_points` 时执行

## 5. 判断结果

成功时应看到：

```text
[DATE_TIME] OK ...
[DEVICES] OK ...
[HISTORY_CFG] node=1: ...
Result: OK
```

重点检查 `read_devices` 是否能看到客户邮件里提到的模块名称：

```text
TP-A1-7007A-01-01 ... TP-A1-7007A-01-08
水冷机组/Water CHILL 01
风冷机组 /Air CHILL 02
```

如果备用 850A 里能读到通讯中断，通常会在设备列表、状态字段、历史配置或后续点位读取里出现 `Offline`、`online=0`、`Comm`、`status` 等线索。

## 6. 确认点位映射

新版可编辑点表里的 `Point Address` 是 BMS/Modbus 地址参考，不要直接当成正式 850A XML `vid`。

正式 `read_val` 点位必须以以下来源为准：

- 850A API 返回的 `read_device_history_cfg`
- 客户提供的 850A XML 点位映射
- 现场用 `read_val` 验证成功的 `nodetype/node + cid/vid`
- 现场用 `read_val` 验证成功的 `tag`

确认后，把点位写入 `config.customer.json`：

```json
"danfoss_primary_point": "ColdStagingControlTemp",
"danfoss_points": [
  {
    "key": "ColdStagingControlTemp",
    "nodetype": 16,
    "node": 1,
    "tag": "TP-A1-7007A-01-CHILL02-CO-TI01"
  }
]
```

或：

```json
"danfoss_points": [
  {
    "key": "MotorFreq",
    "nodetype": 16,
    "node": 5,
    "cid": 43,
    "vid": 21
  }
]
```

## 7. 运行完整 Gateway

API 诊断成功后再运行完整网关：

```bat
set GATEWAY_CONFIG_PATH=config.customer.json
python Gateway.py
```

成功日志应包含：

```text
[CFG] Danfoss southbound: 850A XML POST endpoint=...
[DATA] ...
```

如果使用 EXE，把确认后的 `config.customer.json` 内容复制到 EXE 同目录的 `config.json`，然后运行：

```bat
DanfossGatewayIR.exe
```

## 8. 常见故障

- TCP 失败：850A IP、端口、防火墙、网卡路由不通。
- `401/403`：认证模式或账号密码不对，确认 `basic_header` 还是 `session`。
- HTTPS 证书失败：联调可临时关闭 TLS 校验，正式应配置 CA 证书。
- `read_devices` 无设备：API 权限不足、命令不支持，或 endpoint 不是 850A XML API。
- `read_val` 无值：点位映射不对，先回到 `read_device_history_cfg` 找真实 `cid/vid/tag`。
- Gateway 有仿真值：说明 850A 读取失败后走了缓存或 simulation，先看诊断脚本结果。
