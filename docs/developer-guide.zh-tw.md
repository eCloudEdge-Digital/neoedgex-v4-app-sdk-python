# NeoEdgeX App SDK Python v4 第三方開發指南

> 最新版本變更見[文末版本變更紀錄](#版本變更紀錄)。

## 這個 SDK 是什麼

NeoEdgeX App SDK Python v4 是用來開發 NeoEdgeX 節點應用程式的 Python SDK，支援 driver、protocol adapter、forwarder、processor 等節點類型。SDK 提供統一的執行模型：

- 透過 `ctx.messages()` 接收來自 NeoFlow 的上游訊息
- 透過 `ctx.node_config()` 讀取節點設定
- 透過 `ctx.publish(handle, ...)` 發布下游輸出
- 透過 `ctx.report_error(...)` 回報執行錯誤

節點生命週期、訊息傳輸、心跳、錯誤回報、關閉流程，以及 mock 模式，由 SDK 統一處理。

Python SDK 2.x 講的是 NeoFlow 的 CBOR 訊息格式：任何實作同一份格式的節點都能與 Python 節點逐位元組相容地交換 NeoFlow 訊息，`tests/testdata/golden/` 裡的 golden fixture 由實際運行的對端節點錄製而來，每次測試都會回放到 Python 的編解碼路徑上。

## 公開可依賴邊界

第三方應用程式只應依賴以下公開套件：

- `neoedgex`：app 進入點、handler 介面，以及 handler 會用到的型別
- `neoedgex.contract`：schema 型別，即 `DataType`、`PortFieldSchema`、`NodeData`
- `neoedgex.mock`：本地 mock 執行用的設定格式
- `neoedgex.testutil`：`NodeEnv` 測試替身與訊息建構器，供單元測試使用；正式 app entrypoint 不需要 import

只讀值、發布值的 app 完全不需要 import `neoedgex.contract`——`DataType`、`Node`、`Message`、`Logger`、`ErrorCode` 都由 `neoedgex` 直接匯出。一旦要在 Python 程式碼裡指名 schema 型別就需要它，例如在測試裡組出節點設定，或走訪 `node_config().data.inputs`。

本指南涵蓋的公開入口：

- `neoedgex.new(handler)`
- `App.run()`
- `App.enable_mock(...)`
- `App.disable_sdk_log()`
- `neoedgex.load_mock_config(...)`
- `neoedgex.NodeHandler`
- `neoedgex.NodeEnv`
- `neoedgex.Node`
- `neoedgex.Message`，含 `to_dict()` 與 `to_dataclass(...)`
- `neoedgex.Logger`
- `neoedgex.ErrorCode`，含 `neoedgex.CodeInitializationError` / `CodeNetworkError` / `CodeProcessError` 別名
- `neoedgex.DataType`
- `neoedgex.convert_to_typed_value(...)`——`publish` 使用的轉換引擎，可直接呼叫
- `neoedgex.PortFieldData`——mock 設定與 device-facing 程式碼使用的 `{type, value}` 字串形式欄位值
- `neoedgex.convert_any_value(...)`——把原生 Python 值轉成 `PortFieldData` 的字串形式與推得的型別
- `neoedgex.convert_value_by_type(...)`——把 `PortFieldData` 字串依其型別解析回原生 Python 值
- `neoedgex.contract.PortFieldSchema`、`neoedgex.contract.NodeData`
- `neoedgex.mock.load_config(...)`
- `neoedgex.testutil.MockNodeEnv`，含 `new_message(...)`
- `neoedgex.testutil.new_message(...)`、`testutil.UNDECLARED`、`testutil.Single`、`testutil.PublishedMessage`

repo 裡的其餘路徑一律不可依賴，`neoedgex._internal` 尤其如此：不屬於 SDK 契約，任何版本都可能改動。

## 最小可用範例

實作 `neoedgex.NodeHandler`，透過 `neoedgex.new(...).run()` 啟動。

> 本 handler 由 [`tests/test_guide_examples.py`](../tests/test_guide_examples.py) 原樣執行驗證。

```python
import neoedgex


class ExampleApp:
    def handle(self, ctx: neoedgex.NodeEnv) -> None:
        for _msg in ctx.messages():
            try:
                ctx.publish("output1", {"power": 42.0})
            except Exception as err:
                ctx.report_error(neoedgex.CodeProcessError, err)


if __name__ == "__main__":
    neoedgex.new(ExampleApp()).run()
```

handle 與 key 不能隨意命名：`publish` 依節點的 output schema 建構 payload，所以這段範例只有在節點的 `output1` 有宣告 `power` 欄位時才會真的送出東西。schema 未宣告的 key 會被丟棄——下游收不到東西時，這是第一個該檢查的地方。詳見下方 Output Schema。

停用 SDK 內部 log，在 `run()` 前呼叫 `disable_sdk_log()`：

```python
app = neoedgex.new(ExampleApp()).disable_sdk_log()
app.run()
```

它只會關掉 SDK 自己輸出的 log。handler 透過 `ctx.logger()` 寫出的內容照常輸出。

## 如何設定 Custom App

SDK 從固定根路徑 `/opt/neoedgex` 讀取平台掛載的檔案：

- `/opt/neoedgex/config/messenger.json`：平台產生的 MQTT 帳號密碼；broker 依此帳號套用 topic 權限。以唯讀掛載，app 無法也不需修改。
- `/opt/neoedgex/config/config.json`：平台下發的節點設定，SDK 透過 `ctx.node_config()` 提供給 handler

Custom App node 的設定來自 `ctx.node_config()` 回傳的節點定義，分三個區塊：

1. `config.data.inputs`
2. `config.data.outputs`
3. `config.data.settings`

### Input Schema

input schema 定義在 `config.data.inputs`：

```json
{
  "inputs": {
    "input1": [
      { "key": "temperature", "type": "double" }
    ],
    "input2": [
      { "key": "running", "type": "bool" }
    ],
    "input3": [
      { "key": "capturedAt", "type": "string" }
    ]
  }
}
```

可以同時定義多個 input handle，每個 handle 各自帶獨立的欄位 schema；handler 透過 `msg.handle` 判斷訊息來自哪一個 input。

input schema 描述 handler 從 `ctx.messages()` 讀到的欄位。每個欄位包含：

- `key`：handler 從解碼後的訊息讀到的欄位名稱
- `type`：欄位的資料型態，完整決定解碼後的 Python 值

調整 input schema，就是在改變 handler 對該 handle 呼叫 `msg.to_dict()` 時，SDK 會依 schema 型別解碼哪些 key——也是在改變 `msg.to_dataclass(...)` 遇到上游送來的值與欄位標註不符時退回 schema 解碼的那些 key。handler 讀取的 key 應與這份定義保持一致，實際的 Python 型別則由 SDK 依 `type` 解碼決定。

<img width="200" height="102" src="./assets/node-input-config.png" />

### Output Schema

output schema 定義在 `config.data.outputs`：

```json
{
  "outputs": {
    "output1": [
      { "key": "power", "type": "double" },
      { "key": "status", "type": "string" }
    ]
  }
}
```

可以同時定義多個 output handle，每個 handle 各自帶獨立的欄位 schema；handler 透過 `ctx.publish(handle, data)` 的第一個引數選擇要送往哪一個 output。

這份 schema 決定 `ctx.publish(handle, {...})` 的驗證與轉換行為：

- publish 的 dict key 需和該 `handle` 所定義的 key 一致
- destination `type` 決定可接受哪些 Python 值，以及如何轉換
- schema 中被省略的欄位，SDK 送出 CBOR null（= undefined）
- 明確傳入 `None` 的欄位，同樣送出 CBOR null

新增、刪除或改名欄位後，需同步更新 `ctx.publish(...)` 的呼叫。

<img width="200" height="87" src="./assets/node-output-config.png" />

### Settings

執行設定定義在 `config.data.settings`，對應的 `docker-compose.yml` 欄位如下：

- `containerName`：同時影響 service key 與 `container_name`
- `image`：service 的 `image`
- `envVars`：service 的 `environment`
- `files`：`volumes` 下的額外 bind mounts
- `devices`：service 的 `devices`
- `gpu.enabled=true`：為 service 加上 `gpus`
- `portBindings`：service 的 `ports`

以下欄位屬於 node settings，不直接出現在 compose service 中：

- `credentials`：`neoedgex-agent` 用這組 credential 登入 docker registry，拉取 `image` 指定的 image

對應的 `docker-compose.yml` 範例：

```yaml
name: neoedgex
services:
  7719d4f0cc984dd6:
    container_name: 7719d4f0cc984dd6
    depends_on:
      neoedgex-messenger:
        condition: service_started
        required: true
    devices:
      - source: /dev/ttyUSB0
        target: /dev/ttyUSB0
        permissions: rw
    environment:
      a: b
    gpus:
      - capabilities:
          - gpu
        driver: nvidia
        count: -1
    image: 192.168.64.202:5001/busybox:stable
    networks:
      neoedgex-network: null
    restart: always
    ports:
      - target: 80
        published: "8080"
        protocol: tcp
    volumes:
      - type: bind
        source: ...
        target: /opt/neoedgex/config
        read_only: true
      - type: bind
        source: ...
        target: /var/myfile/ca-copy.crt
        read_only: true
```

### 傳遞 App Config

app 從環境變數或掛載檔案讀取業務設定；SDK 負責把這些內容帶進容器，但不解析 app 的 business config。

#### 模式 A：用固定 key 的 env var 當 config

適合：

- 小型設定
- 單一字串或 JSON blob
- 少量、容易直接放進 environment 的設定

在 `settings.envVars` 定義固定 key：

```json
"envVars": [
  {
    "key": "HTTPCLIENT_CONFIG_JSON",
    "value": "{\"endpoint\":\"https://api.example.com/ingest\",\"method\":\"POST\"}",
    "note": "app business config"
  }
]
```

app 讀取固定 key：

```python
import os

raw = os.getenv("HTTPCLIENT_CONFIG_JSON", "")
if not raw:
    raise ValueError("HTTPCLIENT_CONFIG_JSON is required")
```

也可以拆成多個獨立的 env var：

```json
"envVars": [
  {
    "key": "HTTPCLIENT_ENDPOINT",
    "value": "https://api.example.com/ingest",
    "note": "HTTP endpoint"
  },
  {
    "key": "HTTPCLIENT_METHOD",
    "value": "POST",
    "note": "HTTP method"
  },
  {
    "key": "HTTPCLIENT_TIMEOUT_SECONDS",
    "value": "10",
    "note": "request timeout"
  }
]
```

```python
import os

endpoint = os.getenv("HTTPCLIENT_ENDPOINT", "")
method = os.getenv("HTTPCLIENT_METHOD", "")
timeout_raw = os.getenv("HTTPCLIENT_TIMEOUT_SECONDS", "")
```

#### 模式 B：用固定路徑的檔案當 config

適合：

- 較大的 JSON / YAML
- 結構化設定
- 憑證、key、secret file
- 需要以檔案形式人工替換或掛載的內容

在 `settings.files` 宣告固定路徑：

```json
"files": [
  {
    "uuid": "app-config-file",
    "path": "/myconfig.json",
    "secret": "false"
  }
]
```

app 直接讀該路徑：

```python
from pathlib import Path

payload = Path("/myconfig.json").read_text(encoding="utf-8")
```

#### 選擇 env var 或 file

- 小型、單值、少量 JSON：優先用 env var
- 較大或結構化 config：優先用 file
- 憑證、key、secret file：通常用 file
- 同時支援 env 與 file 時，在 app 內明確定義固定優先順序，例如 env 先、file 後

SDK 不決定這個優先順序；這是 app 自身的 contract，應在 app 文件中明確說明。

## 訊息模型

NeoFlow 節點之間透過 MQTT 互相傳訊息：一則資料訊息就是一個 MQTT payload，內容以 CBOR（一種精簡的二進位格式）編碼。這個 payload 不需要你自己組出來或自己拆解——`msg.to_dict()` / `msg.to_dataclass(...)` 負責解碼收到的內容，`ctx.publish(...)` 負責編碼要送出的內容。本節說明的就是這兩端：handler 從 `ctx.messages()` 收到什麼，以及 `publish` 送出什麼。

訊息格式一段話講完：一則資料訊息是最外層有三個 key 的 CBOR map——`source`（文字）、`timestamp`（RFC3339 文字）與 `data`。`data` 是平面 map，每個欄位 key 直接對應原生 CBOR 值，沒有 per-field type 包裝。undefined 欄位是 CBOR null（或 key 直接缺席）。`raw` 欄位是 CBOR 原生 byte string，不是 base64 文字。改用 CBOR 只涵蓋資料訊息：error topic payload 仍是 JSON，heartbeat 仍是空 payload——兩者都由 `tests/test_runtime.py` 守門。`tests/test_golden.py` 把從實際運行的對端節點錄下的 fixture 回放到 Python 的編解碼路徑上，確保這份格式不悄悄漂移。

### 術語

- `node`：一個被這個 app 匹配到的 NeoEdgeX 節點設定
- `handle`：input 或 output port 名稱，例如 `input1`、`output1`
- `tag`：input 或 output schema 裡的一個具名欄位，即 `key` / `type` 這一組，例如 `{ "key": "temperature", "type": "double" }`
- `mock mode`：SDK 的本地模擬模式，不需要真實平台就能注入假訊息
<img width="200" height="61"  src="./assets/node-diagram.png" />

### NodeEnv 與 Message

每個 handler 會收到一個 `neoedgex.NodeEnv`。

`NodeEnv` 提供：

- `node_config()`：原始節點設定，含 `data.settings`、`data.inputs`、`data.outputs`
- `messages()`：接收進來的 `neoedgex.Message`
- `context()`：這個 node 的生命週期訊號——一個 `threading.Event`，node 該停止時會被 set；把它傳給 worker、HTTP、DB、gRPC 等長生命週期工作
- `logger()`：node-scoped logger
- `publish(handle: str, data: dict[str, object])`：送出到指定的 output handle
- `report_error(code, err)`：回報平台可見的 node error
- `stop()`：要求 SDK 停止這個 node，用於 handler 遭遇無法繼續的 fatal error

`neoedgex.Message` 包含：

- `handle`：觸發此訊息的 input handle 名稱
- `raw`：原樣持有這則訊息的 `data` 段，內容仍是 CBOR 編碼；不直接讀取，而是以 `msg.to_dict()` 或 `msg.to_dataclass(...)` 解碼取值
- `source`：來源節點 ID
- `timestamp`：上游節點 publish 的時間，RFC3339 格式。本版 SDK 的節點以 UTC 寫入毫秒精度，因此結尾為 `Z`（`2026-03-22T10:30:00.123Z`）。SDK 原封不動地傳遞這個字串且從不驗證，因此其形式取決於發送端：舊版 SDK 的節點寫入秒精度、不帶小數位；時鐘不在 UTC 的節點寫入本地時區偏移（例如 `+08:00`）。請以 `datetime.fromisoformat` 解析（上述形式皆可讀取），不要做字面比對。上游 payload 完全未帶時間時為空字串

### 讀取 Input 值

`msg.raw` 持有這則訊息的 `data` 段，內容仍是 CBOR 編碼。呼叫 `msg.to_dict()` 解碼成含 Python 原生值的 `dict[str, Any]`：

- input schema 宣告的每個欄位，都以該 tag 所宣告的 `type` 對應的 Python 型別解碼（見下方表格）
- 欄位為 `None` 代表 **undefined**：上游節點未輸出該欄位（CBOR null）、收到的訊息裡沒有這個 key，或收到的值無法讀取或轉換成 schema 型別
- 收到的值型別與 schema 型別不符時，SDK 以與 publish 側相同的跨型別轉換規則轉換（整數範圍檢查、float→int 截斷、string→number parse、拒絕 NaN/Inf）；規則不允許或轉換失敗時，該欄位交付 `None`
- 出現在收到的訊息裡、但**未**在 input schema 宣告的 key，直接交付解碼器產生的 Python 值（見下方表格），並輸出 debug log；SDK 不交付的值型別則以 `None` 交付

`to_dict()` 只解碼一次並在內部快取：每次呼叫回傳**等值的新** dict，就地修改任何一份回傳值都不會被同一則訊息的其他讀取者看到。

讀取時需先依 `msg.handle` 判斷訊息來自哪一個 input，再判斷欄位 key 是否存在，以及 value 是否為 `None`。

以 input schema 宣告一個 `double` 欄位與一個 `string` 欄位為例，解碼結果如下：

> 本範例由 [`tests/test_guide_examples.py`](../tests/test_guide_examples.py) 原樣執行驗證。

```python
from neoedgex import DataType
from neoedgex import testutil

# handler 從 ctx.messages() 會收到的一則訊息；
# 測試裡由 testutil 建出同樣的東西。
msg = testutil.new_message("input1", {
    "temperature": (25.5, DataType.DOUBLE),
    "deviceName": ("sensor-1", DataType.STRING),
})
# msg.handle == "input1"、msg.source == "upstream-node"

data = msg.to_dict()
# data == {"temperature": 25.5, "deviceName": "sensor-1"}
```

訊息一律從 `ctx.messages()` 取得。自己建構的 `neoedgex.Message` 既沒有資料也沒有 input schema，對它呼叫 `to_dict()` 只會得到 `{}`；要在測試裡建立訊息，請用 `testutil.new_message`（見「單元測試輔助」）。

讀取時對每個欄位套用相同的防禦式流程：先判斷 key 是否存在，再判斷 value 是否為 `None`，最後檢查型別。以 `temperature` 為例：

> 本 handler 由 [`tests/test_guide_examples.py`](../tests/test_guide_examples.py) 原樣執行驗證。

```python
import neoedgex


class TemperatureApp:
    def handle(self, ctx: neoedgex.NodeEnv) -> None:
        for msg in ctx.messages():
            if msg.handle != "input1":
                # 未在 schema 中定義的 handle，忽略即可
                continue

            data = msg.to_dict()
            if "temperature" not in data:
                ctx.report_error(
                    neoedgex.CodeProcessError,
                    RuntimeError("internal error: input1 schema does not define tag temperature"),
                )
                continue
            value = data["temperature"]
            if value is None:
                ctx.report_error(
                    neoedgex.CodeProcessError,
                    RuntimeError("temperature was not successfully produced by the upstream node"),
                )
                continue
            if not isinstance(value, float):
                ctx.report_error(
                    neoedgex.CodeProcessError,
                    RuntimeError("internal error: tag temperature has an unexpected type, expected float"),
                )
                continue

            ctx.publish("output1", {"power": value * 2})
```

其他型別只是把 `isinstance` 的目標型別換掉，流程相同。tag 是整數型別時，記得 Python 的 `bool` 是 `int` 的子類別——app 不想把 `True` 當成 `1` 收下時，要先檢查 `isinstance(value, bool)`。

解碼後 dict 的語意：

- `key not in data`：可能是 app 讀取了 input schema 未定義的 tag（屬於 internal error），也可能是整段 `data` 讀不出來——此時 `to_dict()` 回傳 `{}`，所有 key 都會不存在
- `key in data and data[key] is None`：欄位為 undefined——前一個 node 未成功輸出該 tag、收到的訊息裡沒有這個 key，或值無法讀取或轉換成 schema 型別；由 app 決定套預設值、跳過或回報 error
- `key in data and data[key] is not None`：可進行型別判斷，Python 型別依下方兩張表決定

#### 值會以什麼 Python 型別交付

**tag 已在 input schema 宣告。** 有值時，以宣告 `type` 對應的 Python 型別交付；沒有值時交付 `None`，如表格下方兩條規則所述：

| type | handler 讀到的 Python 型別 |
| --- | --- |
| `bool` | `bool` |
| `int16` | `int` |
| `int32` | `int` |
| `int64` | `int` |
| `uint16` | `int` |
| `uint32` | `int` |
| `uint64` | `int` |
| `float` | `float` |
| `double` | `float` |
| `string` | `str` |
| `raw` | `bytes` |

> 本節的交付規則由 [`tests/test_type_table.py`](../tests/test_type_table.py) 逐值執行驗證——實作與規則漂移時，該測試會紅。

Python 只有一種無上界的 `int` 與一種 `float`（64 位 double），所以宣告型別挑的不是 Python 型別，而是收訊時的**範圍檢查**與發送時的**CBOR 編碼**。宣告 `float` 不是標籤——它決定收窄、範圍檢查與還原行為。

表格之外還有兩條規則：

- 上游以單精度送出的小數進到 `double` tag，或以雙精度送出的小數進到 `float` tag 時，SDK 以轉換規則正規化，並還原最短小數：以單精度送出的 `25.34` 進到 `double` tag，交付為 `25.34`，不是 `25.34000015258789`
- 值放不進宣告的型別時——例如 `1e300` 進到 `float` tag——轉換失敗，該欄位交付 `None`

**tag 未在 input schema 宣告。** 值直接以解碼器產生的樣子交付，不做 schema 轉換。以這種方式交付的 Python 型別只有下列幾種：

| 上游送出的內容 | handler 讀到的 Python 型別 |
| --- | --- |
| 小數，不分精度 | `float` |
| -9223372036854775808 到 18446744073709551615 的整數 | `int` |
| 文字 | `str` |
| `true` / `false` | `bool` |
| 二進位資料 | `bytes` |
| 其他任何值——清單、巢狀結構，或超出上述範圍的整數 | `None`（undefined） |

上游以單精度送出的值，在宣告為 `float` 或 `double` 的 key 上交付還原後的最短小數（`25.34`）；未宣告的 key 沒有宣告的精度可還原，交付的是拓寬殘影（`25.34000015258789`）——這不是資料損毀，是 32 位資訊量的事實。

最後一列是一條封閉規則：上表列出的 Python 型別就是 SDK 會交付的全部，其餘一律交付 `None`，key 仍在，並輸出 warning log。清單與巢狀結構一律不交付——沒有任何 tag type 可以宣告它們，只會來自非本 SDK 的發送端，而且是整個值交付成一個 `None`，不會只交付其中一部分。實務上會遇到的是整數——即使 Python 本身裝得下更大的數，也只有落在上表範圍內的整數才會以數字交付。

還有兩種 CBOR tag 情況補完外來發送端的規則，兩者都釘在 `tests/test_golden.py`：

- **時間 tag（tag 0 / tag 1）。** 沒有任何可宣告型別會產生它們。宣告欄位收到時，交付成 undefined（`None`）：解碼器產出的是 `datetime`，而 `datetime` 不是資料訊息承載的型別。
- **bignum tag（tag 2 / tag 3）。** SDK 永不發送 bignum。解碼器在任何 schema 規則之前就把 tag 吃掉，所以 bignum 包著普通 CBOR 整數裝得下的值時，在未宣告路徑交付該 `int`；宣告的數字欄位則對包著的整數套用一般的範圍規則。

**任何 SDK 無法解碼、轉換或表達的值，一律交付 `None`。** key 仍留在 dict 裡、值為 `None`。所有「沒值」的情況都是同一條規則：上游未輸出值、收到的訊息裡沒有這個 key、值放不進宣告的型別、值根本讀不出來，或該值沒有 SDK 會交付的 Python 型別。

只有一種情況 key 根本不在 dict 裡：整段 `data` 讀不出來的訊息，也就是 payload 損毀的樣子。此時 `to_dict()` 回傳 `{}`——連 schema 宣告的 key 都沒有——log 裡有一行 warning。

### 解碼成 Dataclass

`msg.to_dataclass(SomeDataclass)` 把 data 段直接解成一個 dataclass 實例。它扮演的是驗證函式庫原本會扮演的角色：讀 NeoFlow 訊息不需要 pydantic，SDK 內建的解碼器已涵蓋 schema 驅動的部分。

規則如下，全部釘在 `tests/test_message.py`：

- **目標。** 只接受 dataclass *型別*——傳入實例、普通 class 或 `dict` 會 raise `TypeError`。每次呼叫都建構並回傳一個**新**實例。
- **key 對應。** data map 的 key 預設等於欄位名稱；`field(metadata={"key": "deviceName"})` 可覆寫。
- **undefined 欄位。** key 缺席或值為 CBOR null 時，欄位宣告的 default（或 `default_factory`）生效。沒有 default 的欄位填 `None`。
- **宣告勝過 schema。** 送來的值其 CBOR 型別與欄位標註相符時，直接照標註解碼，input schema 完全不參與。`int` 涵蓋整個 int64 值域：值域內送來的整數不管 schema 說什麼都收，超出值域則 raise。`float` 跟著送來的精度走：單精度值還原成最短十進位形式（`25.34`，不是 `25.34000015258789`），雙精度值原樣收下；送來的整數也能直接解進 `float` 標註。`str`、`bytes`、`bool` 各自直接收下對應的 CBOR 型別。
- **值存在但是壞值。** 送來的值型別與標註不符時，改由 input schema 解碼，此時兩種情況走這條路：上游送來非 null、卻無法解碼成 schema 型別的值（`to_dict()` 交付 `None` 並輸出 warning），以及 schema 解碼成功但放不進欄位標註的值。無論哪一種，只要標註是具體型別——`float` 和 `float | None` 一視同仁——整個呼叫就 raise 帶欄位名稱的 `ValueError`。只有「原樣收下」的標註（`Any`、`object`、未標註）保持安靜：無法解碼的值讓這種欄位留 `None` 並輸出一行 log。壞值永遠不會讓 default 生效——default 只涵蓋 key 缺席或 null。欄位可能正當地沒有值時，請宣告 `X | None`：`None` 從此精確代表「null 或缺席」，也就保住「真正的零值 vs 沒值」的分辨力——真正的 `0.0` 交付 `0.0`，undefined 欄位交付 `None`，壞值則 raise。
- **schema 值的標註處理。** 在上述 schema 路徑上，值的型別必須就是標註的型別——`int` 值不會拓寬進 `float` 標註。`int` 標註絕不接受 `bool`（Python 的 `bool` 是 `int` 子類別，但兩種 CBOR 型別毫無關係）。`Any`、`object`、未標註、容器與多型別 union 一律原樣收下 schema 解碼值——它們沒有可以獲勝的宣告。
- **`init=False` 的欄位跳過**——它們無法透過建構子傳入，保持自身初始化產生的值。
- **全有或全無。** 失敗的呼叫不會留下寫到一半的目標：`to_dataclass` 要嘛回傳完整的新實例，要嘛 raise。

兩個讀取介面的分工清楚：`to_dict()` 信 schema，`to_dataclass` 信你的宣告。Python 的 type hint 沒有位寬——`int16` 和 `int64` 都是 `int`，`float32` 和 `float64` 都是 `float`——所以宣告挑的是型別種類，永遠不是寬度：`int` 的行為等於宣告 `int64`，`float` 則如上所述跟著送來的精度走。

下面的範例可以直接執行。`testutil.new_message(...)` 建出的就是 handler 會從 `ctx.messages()` 收到的那則訊息；在自己的 app 裡，`msg` 來自該 stream，解碼錯誤也應交給 `ctx.report_error(...)` 而不是讓它往外拋。

> 本範例由 [`tests/test_guide_examples.py`](../tests/test_guide_examples.py) 原樣執行驗證。

```python
from dataclasses import dataclass, field
from typing import Any

from neoedgex import DataType
from neoedgex import testutil

# msg 是從 ctx.messages() 收到的一則訊息，這裡由 testutil 在測試中建出同樣
# 的東西。每個值旁邊寫的是接收端節點 input schema 對該 key 宣告的型別；
# testutil.UNDECLARED 則標記 schema 根本沒有宣告的 key。
msg = testutil.new_message("input1", {
    "temperature": (None, DataType.DOUBLE),   # 上游未輸出值
    "offset": (0.0, DataType.DOUBLE),         # 上游輸出了真正的 0
    "count": (None, DataType.INT64),          # 上游未輸出值
    "ratio": (testutil.Single(25.34), DataType.FLOAT),
    "level": (25.34, DataType.DOUBLE),
    "restored": (testutil.Single(25.34), testutil.UNDECLARED),
    "seq": (5, testutil.UNDECLARED),
    "total": (18446744073709551615, testutil.UNDECLARED),
    "deviceName": ("sensor-1", testutil.UNDECLARED),
    "running": (True, testutil.UNDECLARED),
    "payload": (b"\x01\x02", testutil.UNDECLARED),
})


@dataclass
class Reading:
    temperature: float | None = None  # 沒值 -> default None 生效
    offset: float | None = None       # 真正的 0 -> 0.0
    count: int = 0                    # 沒值 -> default 0：與真正的 0 看起來相同
    ratio: float = 0.0                # 以單精度送來 -> 還原成 25.34
    level: float = 0.0                # 以雙精度送來 -> 25.34
    restored: float = 0.0             # schema 未宣告，但標註照樣獲勝 ->
                                      # 還原成 25.34（信 schema 的 to_dict()
                                      # 會交付拓寬值）
    seq: int = 0                      # 未宣告 -> 5
    total: Any = 0                    # 超過 int64 值域：`int` 標註會 raise，
                                      # Any 收下自然值 int
    device_name: str = field(default="", metadata={"key": "deviceName"})
    running: bool = False             # 未宣告 -> True
    payload: bytes = b""              # 未宣告 -> b"\x01\x02"


reading = msg.to_dataclass(Reading)
assert reading == Reading(
    temperature=None,
    offset=0.0,
    count=0,
    ratio=25.34,
    level=25.34,
    restored=25.34,
    seq=5,
    total=18446744073709551615,
    device_name="sensor-1",
    running=True,
    payload=b"\x01\x02",
)
```

不相容規則的兩面：

> 本範例由 [`tests/test_guide_examples.py`](../tests/test_guide_examples.py) 原樣執行驗證。

```python
from dataclasses import dataclass
from typing import Any

import pytest

from neoedgex import DataType
from neoedgex import testutil

# 上游送來的是文字，dataclass 期望的是數字。
msg = testutil.new_message("input1", {"count": ("not-a-number", DataType.STRING)})


@dataclass
class Strict:
    count: int = 0


@dataclass
class Pointer:
    count: int | None = None


@dataclass
class Loose:
    count: Any = None


with pytest.raises(ValueError):   # 裸 int：整個呼叫中止
    msg.to_dataclass(Strict)

with pytest.raises(ValueError):   # int | None：同樣中止
    msg.to_dataclass(Pointer)

assert msg.to_dataclass(Loose).count == "not-a-number"  # Any：原樣交付
```

### Publish 規則

`publish` 的行為：

- 依 `handle` 引數對應的 output schema 建構 payload；`handle` 須已在 `config.data.outputs` 中定義，否則 raise `ValueError`
- schema 中的欄位若未出現在 `data` 裡，SDK 送出 CBOR null（= undefined）
- 明確提供但值為 `None` 的欄位，同樣送出 CBOR null
- `data` 裡不在該 output schema 中的 key 一律丟棄（log warning），不會出現在送出的 payload 裡
- `ctx.publish(handle, {...})` 接受一般的 Python 值；handler 從 `msg.to_dict()` 讀到的同樣是一般的 Python 值
- 宣告為 `float` 的欄位收窄成單精度送出；絕對值超出單精度範圍的值（例如 `1e300`）該欄位送出 CBOR null，並回報 node error
- `publish` 只有兩種情況會 raise：`handle` 不在 `config.data.outputs` 中，或 MQTT 發送失敗。欄位轉換失敗**不在其中**：該欄位以 CBOR null 送出，SDK 代為向平台回報，`publish` 正常返回。不要把「沒有例外」當成「每個欄位都照我的意思送出去了」

以上規則釘在 `tests/test_runtime.py`。

缺少 output 欄位時的具體例子：

```python
# output1 schema:
# - power: type=double
# - status: type=string

ctx.publish("output1", {"power": 42.0})
```

SDK 用傳入的值建立 `power`，`status` 因未在 `data` 中出現而送出 CBOR null（下游 handler 讀到 `None`＝undefined）。明確傳 `status=None` 結果相同：

```python
try:
    ctx.publish("output1", {"power": 42.0, "status": None})
except Exception as err:
    ctx.report_error(neoedgex.CodeProcessError, err)
```

### Python 值轉換

`ctx.publish` 的轉換行為由傳入 handle 對應 schema 的 destination type 決定。轉換後的值以 destination type 的原生 CBOR 值送出。引擎是 `neoedgex.convert_to_typed_value(value, dest_type)`，也可以直接呼叫。

<table>
  <thead>
    <tr>
      <th>Destination type</th>
      <th>Python 值類別</th>
      <th>轉換規則</th>
      <th>例子</th>
      <th>不接受 / 備註</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="2"><code>bool</code></td>
      <td><code>bool</code></td>
      <td>原樣保留。</td>
      <td><code>True -&gt; True</code></td>
      <td rowspan="2">不接受普通 <code>str</code>。</td>
    </tr>
    <tr>
      <td><code>int</code>、<code>float</code></td>
      <td>採用 zero / non-zero 規則：<code>0</code> 或 <code>0.0</code> 轉成 <code>False</code>；其他值都轉成 <code>True</code>。</td>
      <td><code>0 -&gt; False</code>；<code>3.14 -&gt; True</code></td>
    </tr>
    <tr>
      <td rowspan="4"><code>int16</code>、<code>int32</code>、<code>int64</code></td>
      <td><code>int</code></td>
      <td>依宣告位寬做 range check。</td>
      <td>destination <code>int64</code> + <code>42 -&gt; 42</code></td>
      <td rowspan="4"><code>NaN</code>、<code>Inf</code>、超出範圍的值、<code>datetime</code>、<code>bytes</code> 都會失敗。</td>
    </tr>
    <tr>
      <td><code>float</code></td>
      <td>先截斷小數部分，再做 range check。</td>
      <td>destination <code>int64</code> + <code>12.9 -&gt; 12</code></td>
    </tr>
    <tr>
      <td><code>bool</code></td>
      <td><code>True</code> 轉成 <code>1</code>、<code>False</code> 轉成 <code>0</code>。</td>
      <td>destination <code>int32</code> + <code>True -&gt; 1</code></td>
    </tr>
    <tr>
      <td>數字 <code>str</code></td>
      <td>以嚴格整數格式 parse（不接受底線與前後空白），再做 range check。</td>
      <td>destination <code>int16</code> + <code>"42" -&gt; 42</code></td>
    </tr>
    <tr>
      <td rowspan="4"><code>uint16</code>、<code>uint32</code>、<code>uint64</code></td>
      <td><code>int</code></td>
      <td>只有非負且落在宣告位寬內才接受。</td>
      <td>destination <code>uint32</code> + <code>42 -&gt; 42</code></td>
      <td rowspan="4">負值、<code>NaN</code>、<code>Inf</code>、超出範圍的值都會失敗。</td>
    </tr>
    <tr>
      <td><code>float</code></td>
      <td>先截斷小數部分，再做 range check。</td>
      <td>destination <code>uint64</code> + <code>12.9 -&gt; 12</code></td>
    </tr>
    <tr>
      <td><code>bool</code></td>
      <td><code>True</code> 轉成 <code>1</code>、<code>False</code> 轉成 <code>0</code>。</td>
      <td>destination <code>uint32</code> + <code>True -&gt; 1</code></td>
    </tr>
    <tr>
      <td>數字 <code>str</code></td>
      <td>以嚴格整數格式 parse，再做 range check。</td>
      <td>destination <code>uint32</code> + <code>"42" -&gt; 42</code></td>
    </tr>
    <tr>
      <td rowspan="4"><code>float</code>、<code>double</code></td>
      <td><code>int</code></td>
      <td>轉成目標浮點精度。</td>
      <td>destination <code>double</code> + <code>42 -&gt; 42.0</code></td>
      <td rowspan="4">不接受 <code>NaN</code>、<code>Inf</code>、<code>datetime</code> 與 <code>bytes</code>。destination <code>float</code> 拒絕超出 float32 範圍的絕對值。</td>
    </tr>
    <tr>
      <td><code>float</code></td>
      <td>轉成目標精度：destination <code>float</code> 收窄成單精度（最短小數），destination <code>double</code> 保留原值。</td>
      <td>destination <code>float</code> + <code>25.5 -&gt; 25.5</code></td>
    </tr>
    <tr>
      <td><code>bool</code></td>
      <td><code>True</code> 轉成 <code>1.0</code>、<code>False</code> 轉成 <code>0.0</code>。</td>
      <td>destination <code>double</code> + <code>True -&gt; 1.0</code></td>
    </tr>
    <tr>
      <td>數字 <code>str</code></td>
      <td>以嚴格小數格式 parse。</td>
      <td>destination <code>double</code> + <code>"3.14" -&gt; 3.14</code></td>
    </tr>
    <tr>
      <td rowspan="4"><code>string</code></td>
      <td><code>str</code></td>
      <td>原樣保留。</td>
      <td><code>"neoedgex" -&gt; "neoedgex"</code></td>
      <td rowspan="4">不接受 <code>datetime</code> 與 <code>bytes</code>。</td>
    </tr>
    <tr>
      <td><code>int</code></td>
      <td>轉成十進位字串。</td>
      <td><code>42 -&gt; "42"</code></td>
    </tr>
    <tr>
      <td><code>float</code></td>
      <td>轉成定點十進位字串，取可還原回原值的最短位數；整數值不帶 <code>.0</code>，且永不切換成指數寫法。</td>
      <td><code>25.5 -&gt; "25.5"</code></td>
    </tr>
    <tr>
      <td><code>bool</code></td>
      <td>轉成 <code>"true"</code> 或 <code>"false"</code>。</td>
      <td><code>True -&gt; "true"</code></td>
    </tr>
    <tr>
      <td><code>raw</code></td>
      <td><code>bytes</code></td>
      <td>以 CBOR 原生 byte string 送出（不做 base64）。只允許 <code>raw</code> 轉 <code>raw</code>；沒有其他型別能轉入或轉出 <code>raw</code>。<code>bytearray</code> 也接受，送出時轉為 <code>bytes</code>。</td>
      <td><code>b"hello"</code> 逐 byte 保留。</td>
      <td>其他 Python 型別都不支援。</td>
    </tr>
  </tbody>
</table>

表格之外的三條 Python 專屬注意事項：

- **Python 的 `int` 沒有上界。** 擋住大數的是宣告型別的範圍檢查：`70000` 送進 `int16` 欄位照樣失敗，超出 `[-2**63, 2**64-1]` 的值對*所有*數字 destination 都失敗——資料訊息裝不下它，SDK 永不送出 CBOR bignum。釘在 `tests/test_golden.py` 與 `tests/test_contract.py`。
- **`datetime` 對所有 destination type 一律拒絕。** 時間值請先在 app 內轉成字串（如 `value.isoformat()` 或 `value.strftime(...)`），並把欄位宣告為 `string`。
- 不是單一純量的值——dict、list、set、物件——對所有 destination type 一律拒絕：SDK 回報 error，該欄位送出 CBOR null。

表格中幾列的可執行版本：

> 本範例由 [`tests/test_guide_examples.py`](../tests/test_guide_examples.py) 原樣執行驗證。

```python
import pytest

from neoedgex import DataType, convert_to_typed_value

assert convert_to_typed_value(9527, DataType.BOOL) is True
assert convert_to_typed_value(12.9, DataType.INT64) == 12
assert convert_to_typed_value("42", DataType.INT16) == 42
assert convert_to_typed_value(25.5, DataType.STRING) == "25.5"
with pytest.raises(ValueError):
    convert_to_typed_value(70000, DataType.INT16)
with pytest.raises(ValueError):
    convert_to_typed_value(float("nan"), DataType.DOUBLE)
```

SDK 依據 Python 值與 schema 的 destination type 決定是否可轉換。身為第三方 app 開發者，通常只需要關心傳入的 Python 值能不能被目標 schema 型別接受。

假設 `output1` schema 定義了這個欄位：

```text
- enabled: type=bool
```

若這樣 publish：

```python
ctx.publish("output1", {"enabled": 9527})
```

SDK 套用 `bool` 的 zero / non-zero 規則，`enabled` 轉成 `True`。

但若改成這樣 publish：

```python
ctx.publish("output1", {"enabled": "true"})
```

`publish` 不因此 raise；SDK 把 `enabled` 以 CBOR null（undefined）送出，並代為呼叫 `report_error` 回報平台。

### Publish 流程

以下是完整的 end-to-end 範例，說明 Python 值如何從 handler 流向下游節點。

步驟 1：從 `output1` schema 開始。這個例子假設 `output1` 定義如下：

```text
- temperature: type=double
- running: type=bool
- capturedAt: type=string
```

步驟 2：handler 透過 `ctx.publish(...)` 發布一般的 Python 值。`string` 欄位預期收到 Python `str`：

```python
import neoedgex


class ExampleApp:
    def handle(self, ctx: neoedgex.NodeEnv) -> None:
        for _msg in ctx.messages():
            try:
                ctx.publish(
                    "output1",
                    {
                        "temperature": 25.5,
                        "running": True,
                        "capturedAt": "2026-03-22T10:30:00Z",
                    },
                )
            except Exception as err:
                ctx.report_error(neoedgex.CodeProcessError, err)
```

步驟 3：SDK 在 publisher 這一側把 Python 值轉成 schema 型別後，把整則訊息編碼成 CBOR。訊息最外層有三個欄位：`source`（發送的節點）、`timestamp`（發送當下的時間，RFC3339 格式、精度到毫秒，取自容器的時鐘並以 UTC 呈現，因此不論容器跑在哪個時區都以 `Z` 結尾）與 `data`（你 publish 的欄位）。以下以 CBOR diagnostic notation（CBOR 的人類可讀表示法）呈現——每個欄位直接帶原生值，沒有 per-field type 包裝：

```text
{
  "source": "publisher-node",
  "timestamp": "2026-03-22T10:30:00.123Z",
  "data": {
    "temperature": 25.5,
    "running": true,
    "capturedAt": "2026-03-22T10:30:00Z"
  }
}
```

步驟 4：下游 node 在 `input1` 收到後，handler 以 `msg.to_dict()` 解碼，每個欄位以下游 input schema 對應的 Python 型別交付：

```python
# msg.handle == "input1"、msg.source == "publisher-node"、
# msg.timestamp == "2026-03-22T10:30:00.123Z"

data = msg.to_dict()
# data == {
#     "temperature": 25.5,             # double -> float
#     "running": True,                 # bool -> bool
#     "capturedAt": "2026-03-22T10:30:00Z",  # string -> str
# }
```

## Mock 開發流程

mock mode 適合本地開發與整合測試，不需要真實 NeoEdgeX 平台。

```python
import neoedgex
from neoedgex import mock


class ExampleApp:
    def handle(self, ctx: neoedgex.NodeEnv) -> None:
        for _msg in ctx.messages():
            try:
                ctx.publish("output1", {"value": "ok"})
            except Exception as err:
                ctx.report_error(neoedgex.CodeProcessError, err)


if __name__ == "__main__":
    app = neoedgex.new(ExampleApp())

    config = mock.load_config("./mock-config.json")
    app.enable_mock(config)

    app.run()
```

最小 mock config：

> 本設定檔由 [`tests/test_guide_examples.py`](../tests/test_guide_examples.py) 原樣載入驗證。

```json
{
  "nodes": [
    {
      "id": "node-1",
      "type": "app",
      "data": {
        "name": "demo-node",
        "inputs": {
          "input1": [
            { "key": "temperature", "type": "double" }
          ]
        },
        "outputs": {
          "output1": [
            { "key": "value", "type": "string" }
          ]
        },
        "application": {
          "key": "demo-app",
          "version": "2.0.0"
        },
        "settings": {}
      }
    }
  ],
  "mock": {
    "messageInterval": "3s",
    "messages": [
      {
        "nodeID": "node-1",
        "handle": "input1",
        "data": {
          "temperature": {
            "type": "double",
            "value": "25.5"
          }
        }
      }
    ]
  }
}
```

這份檔案必須注意的地方：

- `mock.messages[].nodeID` 必須與某個 `nodes[].id` 完全相同，`handle` 也應是同一個節點在 `inputs` 中宣告過的。節點未宣告的 `handle` 仍會送達，但背後沒有 input schema，所有值都走 bypass 路徑、不帶型別。
- 訊息每個 tick 注入一則，從清單頭開始輪替；app 啟動後約半秒開始。要同時測多個 input，就每個 input 各列一則訊息，讓它們輪流注入。
- `messageInterval` 是 duration 字串：一至多個「數字＋單位」組合，可帶小數，單位為 `ns`、`us`、`ms`、`s`、`m`、`h`——例如 `"3s"`、`"500ms"`、`"1.5s"`、`"1m30s"`。未填、無法解析或非正值時，一律退回 3s，且不報錯。
- 注入的值維持字串化的 `type`/`value` 形式：浮點用定點十進位字串（`"25.5"`；科學記號如 `"2.55e+01"` 亦可解析）、`raw` 用 base64 文字、bool 用 `"true"` / `"false"`。SDK 在注入時把每筆值轉成原生值並編成真正的 CBOR 訊息，handler 讀到的解碼結果與正式環境一致。`type` 留空的欄位會注入成 undefined，這就是測試 `None` 路徑的方法。條目或 schema 欄位上殘留的舊 `format` 鍵會被容忍並忽略（釘在 `tests/test_mock.py`）。
- 注入的訊息一律帶 `source` `"mock"`，`timestamp` 則取自 publish 路徑同一個 UTC 時鐘，因此 mock 執行也看得到正式環境的格式。
- 沒有真實 broker，因此 handler publish 的內容只看得到 log：`[MOCK PUBLISH]` 行會帶出 topic 與解碼後的 payload。heartbeat 也以同樣形式出現，payload 為空。呼叫 `disable_sdk_log()` 會把這些全部關掉。

`neoedgex.load_mock_config(...)` 是 `neoedgex.mock.load_config(...)` 的便捷入口。mock main 已 import `neoedgex.mock` 時，建議直接用 `mock.load_config(...)`，讓 mock 設定的來源保持明確。

正式部署時不要開啟 mock mode。

## 單元測試輔助

`neoedgex.testutil` 讓你不需要平台、也不需要 broker 就能執行自己的 `NodeHandler`：

- `MockNodeEnv` 用來取代 SDK 傳給 `handle` 的 `NodeEnv`。把 `config` 設成要測試的節點設定，另可設定 `mock_logger`、`done_event`（set 它等於「取消」`ctx.context()`）與 `publish_error`（每次 `publish` 會 raise 的例外）。handler 結束後，從 `published_data`、`reported_errors`、`stop_called` 讀結果。`stop()` 只記錄 `stop_called`，不會 set `done_event`；測試若需要 handler 觀察到取消，請自行 set `done_event`。
- `env.new_message(handle, data)` 依 `config` 裡的 input schema 建立進來的訊息，欄位解碼出來的型別與正式環境完全一致。`handle` 未在 `config.data.inputs` 中宣告時會 raise。schema 有宣告、`data` 沒給的 key 交付 `None`，如同上游從未輸出。
- 指定 `env.message_iterable`（任何訊息 iterable）餵給 handler；iterable 耗盡後 `for msg in ctx.messages()` 迴圈結束，測試才能開始斷言。

> 本測試由 [`tests/test_guide_examples.py`](../tests/test_guide_examples.py) 原樣執行驗證。

```python
from neoedgex import DataType
from neoedgex.contract import Node, NodeData, PortFieldSchema
from neoedgex.testutil import MockNodeEnv, PublishedMessage


def test_example_app() -> None:
    env = MockNodeEnv(
        config=Node(
            id="node-1",
            data=NodeData(
                name="demo-node",
                inputs={"input1": [PortFieldSchema(key="temperature", type=DataType.DOUBLE)]},
                outputs={"output1": [PortFieldSchema(key="power", type=DataType.DOUBLE)]},
            ),
        )
    )
    env.message_iterable = [env.new_message("input1", {"temperature": 25.5})]

    ExampleApp().handle(env)

    assert env.published_data == [PublishedMessage(handle="output1", data={"power": 42.0})]
```

要記得的事：

- `published_data` 原樣記錄 handler 傳給 `publish` 的那個 dict：不會依 output schema 做型別轉換，也不會丟掉任何 key。因此要斷言的是 handler 產生的值，而不是實際會送到下游的內容。
- `testutil` 建出的訊息帶 source `"upstream-node"`、timestamp `"2026-01-01T00:00:00.000Z"`；建好後對 `msg.source` / `msg.timestamp` 賦值即可覆寫。

手邊沒有節點設定時——例如只想單獨測解碼邏輯——可用 `testutil.new_message(handle, {...})`，把宣告型別直接寫在值旁邊：`{"level": (testutil.Single(25.34), DataType.DOUBLE)}` 重現的是上游以單精度送出的 `double` tag，`testutil.UNDECLARED` 則標記 input schema 未宣告的 key。值依其 Python 型別編碼（裸 float 編成雙精度、`testutil.Single` 編成單精度），與宣告型別無關——正式環境裡兩端 schema 本來就互相獨立。

這個套件只建議用在測試；正式 app entrypoint 不需要 import `neoedgex.testutil`。

## 執行時行為

SDK 負責：

- SDK 初始化與關閉
- node instance 生命週期
- 訊息傳輸整合
- 定期 heartbeats
- 發布 handler 回報的 error
- process signal 處理
- handler 監控與重啟

handler 作者負責：

- 從 `ctx.messages()` 讀訊息
- 實作業務邏輯
- 正確發布 output 與回報錯誤
- 把 `ctx.context()`（停止事件）傳進 worker、HTTP、DB、gRPC 等長生命週期工作
- 需要 node-scoped log 時使用 `ctx.logger()`
- 在 `ctx.messages()` 關閉後正常 return

執行規則：

- 設定裡的每個 node 都會在自己的 thread 裡執行 `handle(ctx)`；SDK 不做任何篩選，而且所有 node 共用同一個 handler 物件，因此 handler 必須是併發安全的
- 若 handler raise，SDK 會攔下並把它視為 node failure
- 若 handler 在 node 還活著時提早 return，SDK 會視為異常並重啟
- 若是正常關閉，訊息 stream 關閉後 handler 應直接 return
- 若 handler 在初始化階段發現無法繼續執行的 fatal error，應先 `ctx.report_error(neoedgex.CodeInitializationError, err)`，再 `ctx.stop()`，最後 return
- 進站訊息 buffer 可容納 4096 則；handler 處理訊息的速度跟不上訊息進來的速度、buffer 塞滿時，後續進來的訊息會被 drop，SDK 同時呼叫 `report_error`，但被 drop 的訊息無法復原
- broker 與這個 buffer 之間還有一個小很多的佇列；瞬間爆量把它塞滿時，訊息只會被 drop 並留下一行 warning，不會回報 error，因此被回報的 drop 數只是下限
- 呼叫 `ctx.stop()` 同時 set `ctx.context()`；任何以這個 event 傳遞取消訊號的 worker 或長生命週期迴圈都應就此收尾
- `ctx.stop()` 只結束這一個 node：同一個 app 的其他 node 照常執行，`run()` 不會回傳，process 也會一直存活到平台停掉容器為止

例如：

```python
import neoedgex


class ExampleApp:
    def handle(self, ctx: neoedgex.NodeEnv) -> None:
        try:
            parse_settings(ctx.node_config())
        except Exception as err:
            ctx.report_error(neoedgex.CodeInitializationError, err)
            ctx.stop()
            return
```

## 常見錯誤

- `handle` 太早 return。正常 steady-state 寫法通常是持續讀 `ctx.messages()`。
- 把 `msg.to_dict()` 結果裡的 missing key 和 present-but-`None` 當成同一種情況。
- app 需要分辨「真正的零值 vs undefined」的欄位，卻在 `to_dataclass` 目標裡用裸標註（`float` 而不是 `float | None`）。
- 檢查 `isinstance(value, int)` 卻沒先排除 `bool`——Python 的 `bool` 是 `int` 子類別。
- import `neoedgex._internal` 底下的東西，而不是用公開套件。
- 正式版程式碼忘記拿掉 mock mode。
- 以為每個 input tag 都一定會有可直接使用的值；實際上某些欄位可能是 `None`（undefined），需要由 app 自己決定怎麼處理。

## 版本變更紀錄

本 SDK 遵循 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)。最新版本列在最前面。

### v2.1.0 — 2026-08-13

**本版變更資料訊息裡兩個文字值的實際內容，但不改變任何型別。** 最外層 `timestamp` 提高到毫秒精度並改以 UTC 表示；寫入 `string` 宣告 tag 的浮點值改用定點十進位，不再使用科學記號。兩者仍為 CBOR text string，原本能解析舊形式的解析器同樣能解析新形式，新舊版本節點雙向皆可交換訊息。對這些字串做精確比對、以固定長度樣式驗證、或原樣轉發至外部系統的 consumer，請依以下各條檢視。

- **訊息時間戳精度到毫秒。** 最外層 `timestamp` 由整秒改為 RFC3339 帶固定三位小數（`2026-03-22T10:30:00.123Z`），因此同一秒內採樣的資料不再被寫成相同時間。次毫秒的位數採截斷而非四捨五入，且同一瞬間與 Go SDK 產生的字串逐位元組相同。欄位仍為 CBOR text string，`datetime.fromisoformat` 可讀取兩種形式，故仍以秒精度發送的節點雙向皆可互通。以固定長度樣式驗證時間戳、或以未帶 `%f` 的 `strptime` 解析的 consumer 必須調整。
- **Publish 的時間戳一律為 UTC。** 最外層 `timestamp` 取自 UTC 時鐘，因此結尾一律為 `Z`，不再帶容器的本地時區偏移。接收端行為不變：收到的 timestamp 原封交給 handler 且從不驗證，無論其時區或精度。Mock 模式與 `testutil` 比照 publish 形式——mock 注入的訊息帶 UTC 毫秒時間戳，不再是空字串；`testutil` 的預設訊息時間戳由 `"2026-01-01T00:00:00Z"` 改為 `"2026-01-01T00:00:00.000Z"`。假設帶本地偏移的 consumer，以及對上述任一字面值做精確比對的測試，必須調整。判斷訊息是否來自 mock，請依來源 `"mock"`，而非依時間戳為空。
- **浮點字串改為定點十進位。** 浮點值轉入 `string` 宣告的 tag 時，publish 與接收兩側皆轉成定點十進位、取可還原回原值的最短位數：`25.34` 為 `"25.34"` 而非 `"2.534e+01"`；整數值不帶 `.0`（`500.0` 為 `"500"`）；任何量級皆不切換為指數形式。此寫法產生的字串與 Go SDK 逐位元組相同，亦與平台 formula 引擎、forwarder payload 既有的方式一致。解析側不變、兩種形式皆接受，故新舊版本節點可互通，帶科學記號值的 mock 設定檔亦照常載入。一項行為影響：經由 string tag 傳遞的整數值浮點，現在可解析進下游宣告為整數型別的 tag，而科學記號形式會被拒為 undefined——原本因此組合回報轉換錯誤的管線，現在會交付該值。以科學記號形式做樣式比對的 consumer 必須調整。
- **修正**字串轉 `float` 的捨入：改為如 Go 的 `strconv.ParseFloat(s, 32)` 一般，從字串一次直接捨入到 float32，不再經由 float64 中轉。原本的兩段捨入在字串落於捨入邊界時會選到相鄰的 float32（`"7.038531e-26"`），在 float32 溢位邊界上更可能拒絕一個 Go SDK 會接受為 MaxFloat32 的值——同一個 tag 值一端轉得過、另一端轉不過。預期結果已逐位元對照 Go 的輸出釘住；`double` 的轉換原本即為單次捨入，不受影響。

### v2.0.0 — 2026-08-10

**BREAKING 訊息格式與 API 變更。** schema 只留 type，資料訊息改為 CBOR。與 Go SDK v2.1.0 的 wire 契約相同。以 SDK 1.x 建置的 app 無法與 2.0.0 app 交換 NeoFlow 訊息，程式碼也無法不改就跑；沒有新舊格式雙讀的過渡機制——請以本版重新建置並遷移。

- **訊息格式。** 一則資料訊息是最外層有三個 key 的 CBOR map——`source`、`timestamp`、`data`；`data` 直接把每個欄位 key 對應到原生 CBOR 值，沒有 per-field `type`/`format`/`value` 包裝。undefined 欄位為 CBOR null。`raw` 欄位以 CBOR 原生 byte string 送出——不再有 base64。改用 CBOR 只涵蓋資料訊息：error topic payload 仍是 JSON，heartbeat 仍是空 payload。
- **讀取訊息。** 解碼好的 `Message.data` dict 屬性**已移除**——讀 `msg.data` 的程式碼現在會 raise `AttributeError`。`msg.raw` 持有仍是 CBOR 編碼的 `data` 段；以 `msg.to_dict()`（schema 驅動、只解碼一次——每次呼叫回傳等值的新 dict）或 `msg.to_dataclass(SomeDataclass)` 解碼。
- **handler 收到什麼。** 每個宣告的 input 欄位依該 tag 宣告的 `type` 解碼：整數交付 `int`、`float`/`double` 交付 `float`、`string` 交付 `str`、`raw` 交付 `bytes`、`bool` 交付 `bool`。收到的值型別不符時，以與 `publish` 側相同的跨型別轉換規則轉換；無法轉換的值交付 undefined（`None`）。未在 input schema 宣告的 key 直接交付解碼後的 Python 值，且僅限 SDK 會交付的那幾種型別——其他值（清單、巢狀結構、超出 `[-2**63, 2**64-1]` 的整數）交付 `None`。
- **新增** `Message.to_dataclass(...)`：把 data 段解成 dataclass——送來的值型別與欄位標註相符時直接照標註解碼（宣告獲勝），其餘走 input schema——支援 `field(metadata={"key": ...})` key 對應、undefined（缺席或 null）欄位讓 default 生效、值存在但具體標註裝不下時 raise `ValueError`——`X | None` 涵蓋 undefined 情況、`Any` 什麼都收。讀訊息這件事上，它取代了原本會用 pydantic 做的部分。
- **移除**整個 `DataFormat` 概念：schema 與 mock payload 只帶 `type`（設定檔裡殘留的舊 `format` 鍵會被容忍並忽略）。time format `second` / `millisecond` / `datetime` 已移除——時間值以 `string` 欄位傳遞，字串格式由應用自行決定；`base64` 由 `raw` 型別取代，`raw` 在兩個方向都是 `bytes`。型別只剩 11 種純量型別：`int16`、`int32`、`int64`、`uint16`、`uint32`、`uint64`、`float`、`double`、`bool`、`string`、`raw`。
- **移除**頂層 `datetime` 便利轉換：`publish` 現在對所有 destination type 拒絕 `datetime` 值；請在 app 內先轉成字串。
- **新增** NaN / ±Inf 拒絕：publish NaN 或無限大浮點值時該欄位轉換失敗，以 undefined 送出。
- **整數紀律。** Python 的 `int` 沒有上界；`publish` 依宣告型別做範圍檢查，超出 `[-2**63, 2**64-1]` 的值對所有數字 destination 一律拒絕，SDK 永不送出 CBOR bignum。
- **新增** `neoedgex.testutil.new_message(...)`、`testutil.UNDECLARED`、`testutil.Single` 與 `MockNodeEnv.new_message(...)`，在單元測試裡建構與上游節點送出形式一致的訊息。
- **遷移。** 把所有 `msg.data` 讀取改成 `msg.to_dict()`。移除 mock config 與 schema 裡的 `format` 鍵（留著也會被忽略）。時間欄位宣告為 `string` 並在 app 內轉字串。`raw` 欄位讀到的是 `bytes`（不再是 base64 `str`）。

### v1.1.1 — 2026-06-05

- 還原了 v1.1.0 引入的 JSON 資料格式（移除 JSON payload 轉換）。

### v1.1.0 — 2026-05-20

- 新增 input 與 output schema 的多 handle 支援。`ctx.publish` 改為必須明確指定目的 handle（`publish(handle, data)`），handler 以 `msg.handle` 進行分派。
- 新增可承載任意 JSON payload 的 JSON 資料格式（已於 v1.1.1 還原）。

### v1.0.0 — 2026-05-05

- 首次公開發行。
