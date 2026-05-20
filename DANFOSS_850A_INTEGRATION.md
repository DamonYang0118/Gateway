# Danfoss 850A XML 南向接入说明

依据客户提供的 `AU342138209420en-000501.pdf` 和 `Danfoss XML Postman guidance.pdf`，850A XML 接口不是直接 GET `status.xml`，而是向设备 XML 入口 POST 请求：

```text
http://<AK-SM800A IP>/html/xml.cgi
https://<AK-SM800A IP>/html/xml.cgi
```

## 实时点位读取

Demo 已在 `Gateway.py` 中补充 850A 实时采集路径：

```xml
<cmd action="read_val" num_only="1" valid_only="1" units="s" compress="0">
  <val nodetype="16" node="5" cid="43" vid="21" />
</cmd>
```

返回解析目标：

```xml
<resp action="read_val" error="0">
  <val node="5" vid="21" cid="43" nodetype="16" display="Cutout Temp" name="Cutout Temp" stat="Online" statcode="2">-80.0 C</val>
</resp>
```

解析逻辑会提取 `<val>` 的文本值，自动从带单位字符串中取数值，并输出给现有 BACnet `MotorFreq`、高低限报警和 `LastLogText`。

## 配置方式

当前 `config.json` 默认仍保持旧的 `legacy_status_xml` 模式，避免影响现有演示。

切换到 850A XML POST 模式时，将配置调整为：

```json
"danfoss_api_mode": "850a_xml",
"danfoss_endpoint_url": "http://<AK-SM800A IP>/html/xml.cgi",
"danfoss_auth_mode": "basic_header",
"danfoss_username": "<userName>",
"danfoss_password": "<passWord>",
"danfoss_primary_point": "MotorFreq",
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

也可以参考 `config.850a.example.json`。

点位可以用两种方式配置：

```json
{ "key": "MotorFreq", "nodetype": 16, "node": 5, "cid": 43, "vid": 21 }
```

```json
{ "key": "MotorFreq", "nodetype": 16, "node": 5, "tag": "SomeXmlTag" }
```

`cid/vid/tag` 需要从客户实际点表、`read_parm_info` 或设备历史配置中确认。

## 认证模式

`basic_header`：适用于启用 Header Authentication 且非 Strict Session Control 场景。

```text
AKSM-Auth: Basic base64(userName:passWord)
```

`session`：适用于 HTTPS + Strict Session Control 场景。程序会先发送：

```xml
<cmd action="getauth" user="userName" password="passWord" />
```

然后把返回的 `session_token` 放到后续请求 Header：

```text
AKSM-Auth: <session_token>
```

如果设备使用自签名 HTTPS 证书，可把证书路径配置到 `danfoss_ca_cert`；联调阶段也可临时设置 `danfoss_verify_tls=false`。

## 异常缓存

本次补充了最后一次成功值缓存：

```json
"danfoss_cache_file": "danfoss_last_good.json",
"use_cached_value_if_http_fail": true
```

当 850A 调用失败、XML 解析失败、点位无数值时，程序会优先使用最近一次成功值；如果没有缓存，再按原逻辑使用仿真值或跳过本轮。

## 历史数据接口

`danfoss_850a.py` 已预留历史查询能力：

```text
start_history_query -> read_query_status -> read_query_data
```

注意 `read_query_data` 返回二进制数据，不是普通 XML。需要用 `read_query_status` 返回的 `field_count`、`field_size`、`exp`、`actual_sample_rate` 解析。

历史区间查询需要先通过 `read_device_history_cfg` 找到目标点位的 `hist_index`。

## 本地 Mock 验证

客户暂时无法开放 850A 设备访问时，可以先用本地 mock 验证南向逻辑：

```bash
python3 mock_850a_server.py --host 127.0.0.1 --port 8088 --points mock_850a_points.json
```

然后用本地配置运行：

```bash
GATEWAY_CONFIG_PATH=config.localmock.json python3 Gateway.py
```

详细步骤见 `LOCAL_MOCK_DEBUG.md`。
