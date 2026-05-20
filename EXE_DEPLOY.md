# Build EXE (Windows)

Use Windows to build Windows `.exe` (PyInstaller cannot reliably cross-build Windows exe on macOS).

## Steps

1. Open terminal in project folder.
2. Run (Intrinsic Alarm version):

```bat
build_gateway_exe.bat
```

3. Output:

- `dist\DanfossGatewayIR.exe`
- `dist\config.json`

## Run

```bat
dist\DanfossGatewayIR.exe
```

Edit `dist\config.json` for IP/port/limits before running.

## Danfoss 850A XML Mode

The executable now supports two Danfoss southbound modes:

- `legacy_status_xml`: keeps the original GET `status.xml` demo behavior.
- `850a_xml`: uses the AK-SM800A XML POST API at `/html/xml.cgi`.

Use `config.850a.example.json` as a template when switching to the real 850A API. The required point identifiers are `nodetype/node` plus either `cid/vid` or `tag`.

## Priority Mapping (Dual NC)

This build uses two intrinsic alarm objects:

- `MotorFreqHighAlarm` -> `NC_HIGH` (`nc_high_instance`)
- `MotorFreqLowAlarm` -> `NC_LOW` (`nc_low_instance`)

Recommended `config.json` mapping:

```json
"priority_high_to_offnormal": 64,
"priority_high_to_fault": 64,
"priority_high_to_normal": 100,
"priority_low_to_offnormal": 180,
"priority_low_to_fault": 64,
"priority_low_to_normal": 100
```

Target effect in DesigoCC:

- High limit -> High
- Normal/Return to normal -> Medium
- Low limit -> Low
