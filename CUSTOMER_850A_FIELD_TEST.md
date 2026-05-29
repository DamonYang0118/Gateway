# 客户电脑本地 850A 现场测试方案

当前代码可以完成“850A API 连通性、设备名称读取、点位映射验证、单点 Gateway 轮询发布”的现场测试；但还不是最终完整交付版。`diagnose_850a.py` 能读设备列表和历史配置，`Gateway.py` / `DanfossGatewayIR.exe` 当前只发布一个主点位值和相关告警辅助对象，不会自动发布 10 个模块名称、168 个点和所有通讯状态。

现场测试分三层执行：先测 API，再测点位，再测 Gateway/EXE。不要一上来就跑 EXE，否则失败时很难判断是网络、认证、点位映射还是 BACnet 问题。

## 1. 环境确认

在客户电脑上打开命令行，进入项目目录：

```bat
cd /d C:\NOVO\NewGateway
```

确认 Python：

```bat
python --version
```

如果 Python 可用，安装依赖：

```bat
python -m pip install -r requirements.txt
```

如果客户电脑不能安装 Python，先不要判断代码失败；可以改用已打包 EXE 做 Gateway 测试，但 API 定位能力会弱很多。推荐仍然先准备 Python 来跑诊断工具。

## 2. 创建现场配置

复制现场模板：

```bat
copy config.customer.example.json config.customer.json
```

打开配置：

```bat
notepad config.customer.json
```

至少修改：

```json
"danfoss_endpoint_url": "http://<850A IP>/html/xml.cgi",
"danfoss_auth_mode": "basic_header",
"danfoss_username": "<客户给的用户名>",
"danfoss_password": "<客户给的密码>",
"danfoss_points": [],
"use_simulation_if_http_fail": false
```

如果是 HTTPS 自签名证书，联调阶段可临时设：

```json
"danfoss_verify_tls": false
```

第一轮测试保持 `"danfoss_points": []`，先验证网络、认证、设备名称和历史配置。

## 3. 850A 地址和认证测试

运行基础诊断，并保存证据报告：

```bat
run_850a_diagnostics.bat config.customer.json http://<850A IP>/html/xml.cgi field-report-api.json
```

如果 endpoint 已经写进配置，也可以：

```bat
run_850a_diagnostics.bat config.customer.json
```

成功标准：

```text
OK TCP ...
[DATE_TIME] OK ...
```

如果 TCP 失败，检查 850A IP、端口、防火墙、网卡路由。可按顺序试：

```text
http://127.0.0.1/html/xml.cgi
http://172.28.238.109/html/xml.cgi
http://<客户提供的850A IP>/html/xml.cgi
https://<客户提供的850A IP>/html/xml.cgi
```

如果 TCP 成功但 `401/403`，把认证方式从：

```json
"danfoss_auth_mode": "basic_header"
```

改成：

```json
"danfoss_auth_mode": "session"
```

再跑一次诊断。

## 4. 设备名称验证

查看诊断输出里的 `read_devices` 段，重点核对：

```text
TP-A1-7007A-01-01
TP-A1-7007A-01-02
TP-A1-7007A-01-03
TP-A1-7007A-01-04
TP-A1-7007A-01-05
TP-A1-7007A-01-06
TP-A1-7007A-01-07
TP-A1-7007A-01-08
水冷机组/Water CHILL 01
风冷机组 /Air CHILL 02
```

`diagnose_850a.py` 会自动输出 `OK` 或 `MISSING`。如果名称能读出，说明客户邮件里强调的“850A 定义设备名称”已经在 API 层验证成功。

如果 `read_devices` 读不到名称，保存输出和 `field-report-api.json`，后续需要确认账号权限、850A API 命令支持情况，或改用其它设备信息接口。

## 5. 节点和点位发现

运行更详细扫描：

```bat
python diagnose_850a.py --config config.customer.json --nodes 1-8,81,82 --history-sample-limit 30 --report-json field-report-history.json
```

对每个节点记录：

```text
node
设备名称
可读点名称
cid
vid
hist_index
状态/通讯/报警相关字段
```

`diagnose_850a.py` 会额外标出包含 `offline/online/comm/status/alarm/通信/通讯/状态/报警` 等关键词的候选项。

成功标准：

- 至少找到 1 个现场可核对的数值点。
- 最好找到 1 个通讯、Offline 或状态相关点。

## 6. 单点 read_val 验证

不要直接把 Excel 点表里的 `Point Address` 当正式 850A XML `vid`。只有当 850A API 输出或客户明确确认后，才写入 `danfoss_points`。

优先选择现场容易核对的点：

```text
控制温度
U57 RH level %
报警状态
通讯状态
```

如果确认是 tag 方式：

```json
"danfoss_primary_point": "ColdStagingControlTemp",
"danfoss_points": [
  {
    "key": "ColdStagingControlTemp",
    "nodetype": 16,
    "node": 1,
    "tag": "客户确认的tag"
  }
]
```

如果确认是 `cid/vid` 方式：

```json
"danfoss_primary_point": "TestPoint",
"danfoss_points": [
  {
    "key": "TestPoint",
    "nodetype": 16,
    "node": 1,
    "cid": 43,
    "vid": 21
  }
]
```

再运行：

```bat
run_850a_diagnostics.bat config.customer.json http://<850A IP>/html/xml.cgi field-report-readval.json
```

成功标准：

```text
[READ_VAL] TestPoint: value=...
```

返回值需要和 850A 屏幕、第三方大屏或客户确认值一致。

## 7. Gateway 源码运行测试

API 和至少一个点位成功后，再运行正式网关源码：

```bat
set GATEWAY_CONFIG_PATH=config.customer.json
python Gateway.py
```

成功标准：

```text
[CFG] Danfoss southbound: 850A XML POST endpoint=...
points=1
[DATA] ...
```

现场测试建议保持：

```json
"use_simulation_if_http_fail": false
```

这样 850A 失败时不会用仿真值掩盖问题。如果看到 `[DANFOSS][WARN]`、`[CACHE]` 或 `simulation`，先停止 Gateway，回到诊断脚本排查 API 和点位。

## 8. EXE 打包与运行测试

在 Windows 客户电脑或 Windows 构建机运行：

```bat
build_gateway_exe.bat
```

输出应为：

```text
dist\DanfossGatewayIR.exe
dist\config.json
```

把验证过的 `config.customer.json` 内容复制到 `dist\config.json`，然后运行：

```bat
dist\DanfossGatewayIR.exe
```

成功标准和源码运行一致：持续输出 `[DATA]`，并且不走 simulation。

## 9. BACnet 和告警测试

只有 Gateway 读 850A 稳定后再测 BACnet。

设置：

```json
"recipient_ip": "<DesigoCC 或测试 BACnet 客户端 IP>",
"recipient_port": 47808,
"recipient_process_id": 600,
"local_port": 47808
```

用 YABE 或 DesigoCC 检查：

```text
AI:1 MotorFreq
AI:11 MotorFreqHighAlarm
AI:12 MotorFreqLowAlarm
AV:1 Setpoint
AV:2 LastEventState
AV:3 LastLogTs
CSV:1 LastLogText
```

告警触发只通过临时调整测试限值完成，测完恢复正式限值。不要通过影响真实设备工况来触发告警。

## 10. 必须保存的测试证据

850A API：

- endpoint
- auth mode
- `read_date_time` 输出
- `read_devices` 输出
- `read_device_history_cfg` 关键节点输出
- `read_val` 成功点位和值
- `field-report-*.json`

Gateway：

- `config.customer.json` 的非敏感版本
- `[CFG] Danfoss southbound...`
- `[DATA] ...`
- 是否出现 `[DANFOSS][WARN]`、`[CACHE]`、`simulation`

EXE：

- 打包是否成功
- EXE 运行日志
- EXE 使用的 `dist\config.json` 是否和源码测试一致

BACnet：

- YABE/DesigoCC 扫描截图
- 对象列表
- 告警触发和恢复截图

## 11. 代码完整性判断

如果本次目标是验证 850A API 和单点 Gateway 链路：当前代码基本完整。

如果本次目标是最终交付给 BMS，展示完整设备树、10 个模块名称、168 个点、通讯状态和所有告警：当前代码不完整，需要后续开发。

现场测试结束后，根据结果决定下一版开发范围：

- 多点 BACnet 对象自动生成
- 设备名称映射到 BACnet `objectName` / `description`
- 通讯状态点单独发布
- 从 Excel 点表生成配置
- EXE 内置诊断模式，或将诊断工具一起打包
