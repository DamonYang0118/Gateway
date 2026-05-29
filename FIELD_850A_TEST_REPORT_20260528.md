# 850A XML API 现场测试汇报

测试日期：2026-05-28  
测试对象：客户远程笔记本连接的 Danfoss 850A  
测试范围：仅验证 850A 北向 XML API 访问、设备列表、点位配置、实时点读取和告警接口。由于客户笔记本当前不能向 BACnet 网络发送数据，本次不验证 BACnet 发布功能，BACnet 侧仍按本地 mock 环境验证。

## 1. 测试背景

客户提供了一台可远程控制的笔记本，该笔记本已连接 850A 设备，并可访问 850A 的 XML API。测试目标是确认我方 Gateway/Demo 中的 850A XML 调用逻辑是否能够在现场网络中访问真实 850A，并读取客户邮件中提到的设备名称、点位配置和告警信息。

本次测试使用的 850A 地址为：

```text
http://169.254.112.229/html/xml.cgi
```

认证方式使用：

```text
cmd_credentials
```

即用户名和密码随 XML cmd 请求发送。账号密码为客户提供，本汇报不记录明文密码。

## 2. 测试步骤

### 2.1 网络连通性测试

在客户笔记本上运行 850A API 诊断程序，首先验证 TCP 连接是否可达。

测试结果：

```text
TCP 169.254.112.229:80 reachable
```

结论：客户笔记本到 850A 的 HTTP 端口网络连通正常。

### 2.2 850A 时间接口测试

调用 850A 的 `read_date_time` XML API。

测试结果：

```text
[DATE_TIME] OK 2026-05-28 22:07:39
```

结论：850A XML API 可正常响应，请求格式和认证方式有效。

### 2.3 设备列表读取测试

调用 850A 的 `read_devices` XML API，验证能否读取客户邮件中强调的设备名称。

测试结果：成功读取 14 个设备/模块条目，其中包括：

```text
水冷机组/Water CHILL 01
风冷机组 /Air CHILL   02
TP-A1-7007A-01-01
TP-A1-7007A-01-02
TP-A1-7007A-01-03
TP-A1-7007A-01-04
TP-A1-7007A-01-05
TP-A1-7007A-01-06
TP-A1-7007A-01-07
TP-A1-7007A-01-08
```

同时，诊断程序对预期名称进行了核对，以上 10 个现场重点设备名称均显示 `OK`。

结论：850A 内部定义的设备名称可以通过 XML API 读取到，第三方大屏显示的设备名称来源可以通过 API 层验证。

### 2.4 历史配置/点位配置读取测试

调用 `read_device_history_cfg`，对节点 `1-8, 81, 82` 逐个读取历史配置点位。

测试结果：10 个节点均成功返回历史配置。

典型点位包括：

```text
node=1  TP-A1-7007A-01-01
cid=0   vid=2532   u17 Ther. air
cid=0   vid=2687   U57 RH level %
cid=0   vid=2530   u12 S3 air temp.
cid=0   vid=2531   u16 S4 air temp.
cid=0   vid=1011   u09 S5 temp.
cid=0   vid=2537   u20 S2 temp.

node=82 风冷机组 /Air CHILL   02
cid=170 vid=42     Condenser Status
cid=170 vid=53     Fan 1 Status
cid=170 vid=54     Fan 2 Status
cid=120 vid=22     Po Pressure

node=81 水冷机组/Water CHILL 01
cid=340 vid=31     Thermostat 1
cid=341 vid=31     Thermostat 2
cid=342 vid=31     Thermostat 3
cid=120 vid=22     Po Pressure
```

结论：850A 能返回真实可用于 API 查询的 `cid/vid` 点位信息。Excel 点表不能直接等同于 XML API 的 `cid/vid`，后续点位映射应以本次 API 发现结果为准。

### 2.5 参数表读取测试

调用 `read_parm_info`，进一步读取设备参数表。

测试结果：

部分设备成功返回完整参数表，例如：

```text
TP-A1-7007A-01-01: 323 parameter(s)
Suction MT1:        804 parameter(s)
Suction MT2:        804 parameter(s)
```

典型参数包括：

```text
cid=0 vid=2532 unit=degc    rw=R name=u17 Ther. air
cid=0 vid=2682 unit=percent rw=R name=U45 Comm. status
cid=0 vid=2541             rw=R name=--- Sum alarm
cid=0 vid=2578 unit=degc    rw=R name=u57 Alarm air
```

有些虚拟分组或父级模块没有 `device_id`，例如：

```text
水冷机组/Water CHILL 01
风冷机组 /Air CHILL   02
```

这类条目不适合直接调用 `read_parm_info`，但其下级节点仍可读取参数和历史配置。

结论：参数表读取逻辑有效，可用于后续梳理真实点位映射、通讯状态点和报警状态点。

### 2.6 实时值读取测试

选择已确认的 `cid/vid` 点位进行 `read_val` 测试。

测试点 1：

```text
TP_A1_7007A_01_01_TherAir
node=1 cid=0 vid=2532
```

返回：

```text
value=None raw='*' status='Offline' error=63
```

测试点 2：

```text
Air_CHILL_02_CondenserStatus
node=82 cid=170 vid=42
```

返回：

```text
value=None raw='*' status='Offline' error=63
```

同时，设备列表中实际设备的 `online` 字段均为 `0`。

结论：`read_val` 请求格式、节点和 `cid/vid` 能被 850A 识别，但当前备用 850A 上这些现场模块处于离线/通讯中断状态，因此实时值返回 `*`，状态为 `Offline`，错误码为 `63`。这与客户之前提到“备用 850A 可以读到模块通讯中断”的现象一致。

### 2.7 设备告警读取测试

调用 `read_device_alarms` 查询各实际设备告警。

测试结果：

```text
Condenser A:        active=0 acked=0 cleared=0
Suction MT1:        active=0 acked=0 cleared=0
TP-A1-7007A-01-01:  active=0 acked=0 cleared=0
TP-A1-7007A-01-02:  active=0 acked=0 cleared=0
TP-A1-7007A-01-03:  active=0 acked=0 cleared=0
TP-A1-7007A-01-04:  active=0 acked=0 cleared=0
TP-A1-7007A-01-05:  active=0 acked=0 cleared=0
TP-A1-7007A-01-06:  active=0 acked=0 cleared=0
TP-A1-7007A-01-07:  active=0 acked=0 cleared=0
TP-A1-7007A-01-08:  active=0 acked=0 cleared=0
Condenser B:        active=0 acked=0 cleared=0
Suction MT2:        active=0 acked=0 cleared=0
```

结论：设备级告警接口可调用，当前未返回活动告警。

### 2.8 系统级告警读取测试

调用 `read_generic_alarms` 查询系统级告警。

测试结果：

```text
read_generic_alarms returned error=10: Error: Data Access
```

结论：系统级告警接口当前返回 Data Access 错误，可能与 850A 权限、该命令在当前设备上的支持情况、查询参数或系统告警访问范围有关。该失败不影响本次核心 API 链路验证，因为设备列表、参数表、历史配置和设备级告警均已成功读取。

## 3. 本次测试结论

1. 客户笔记本到 850A 的网络连接正常。
2. 850A XML API 可访问，认证方式 `cmd_credentials` 可用。
3. 850A 中定义的设备名称可以通过 API 成功读取，包括 8 个 `TP-A1-7007A-01-xx` 蒸发器模块，以及水冷机组、风冷机组名称。
4. `read_device_history_cfg` 和 `read_parm_info` 可以提供真实的 `cid/vid` 点位映射，后续开发应以 API 返回的点位为准。
5. 当前现场备用 850A 的下级模块显示 `online=0`，实时值读取返回 `Offline/error=63/raw='*'`，说明点位被识别但设备处于离线/通讯中断状态。
6. 设备级告警接口可调用，但当前未发现活动告警。
7. 系统级告警接口 `read_generic_alarms` 当前返回 Data Access，需要后续结合账号权限或 850A 支持范围进一步确认。
8. 由于客户笔记本不能发送 BACnet，本次未验证 BACnet 发布功能；BACnet 侧仍需在可访问 BACnet 网络的环境或本地 mock 环境中验证。

## 4. 对 Demo/网关代码完整性的判断

本次测试证明：当前 Demo 中 850A XML API 客户端和诊断脚本已经具备现场接入验证能力，能够完成：

```text
网络连通 -> 时间读取 -> 设备名称读取 -> 参数/点位发现 -> 单点实时值读取 -> 设备告警查询
```

但当前代码还不是最终交付版本，原因是：

1. 当前 Gateway 主程序仍以单点轮询发布为主，尚未自动发布全部设备和全部点位。
2. 当前还没有把 10 个现场模块名称自动映射为 BACnet 对象名称或设备树结构。
3. 当前还没有把通讯状态、Offline 状态、告警状态按最终 BMS 点表完整发布。
4. 当前现场备用 850A 下级设备离线，无法验证真实在线数值与现场屏幕值的一致性。

因此，如果本阶段目标是验证 850A API 可用性和点位发现能力，当前代码基本满足；如果目标是最终交付给 BMS，仍需继续开发多点映射、设备名称映射、通讯状态和告警发布。

## 5. 建议下一步

1. 保留本次诊断输出和 `field-report-api.json`、`field-report-full.json` 作为现场证据。
2. 请客户确认备用 850A 是否本来就不连接实际下级模块，或是否可以切换到在线设备环境做一次实时值验证。
3. 如需验证实时值，请选择至少一个在线模块，再读取以下点位：

```text
u17 Ther. air
U57 RH level %
U45 Comm. status
--- Sum alarm
Condenser Status
Po Pressure
```

4. 我方下一版建议开发：

```text
从 850A API 自动/半自动生成点位配置
多点 BACnet 对象发布
设备名称映射到 BACnet objectName/description
通讯状态和 Offline 状态单独发布
告警点位和设备级告警发布
将 read_generic_alarms 失败降级为可选告警，不影响主诊断结论
```

