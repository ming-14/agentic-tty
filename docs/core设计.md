# core 设计

> 核心层：纯终端状态与语义。不含连接、推送、订阅者、线程、网络、sid、
> **返回条件与等待引擎**（那是命令层的）。

## 1. 边界

core 只做三件事：

1. **Pty 会话核心** —— 单流 + 终端模型。
2. **子进程会话核心** —— 双流、无终端模型。
3. **生命周期管理** —— 状态机、注册表、退出与排空判定。

配套的支撑件：端口定义、字节日志、字节视图、边界扫描、错误。

core **不做**：`sid` 解析、tags、连接、订阅者、推送、线程、协议帧、认证、**返回条件与等待引擎**。

## 2. 身份模型

- **`uid`**：uuid4，会话在核心层内的唯一标识，不可变。
- core **不知道 `sid`**。`sid` 是命令层（service）的语义，映射在 service 的会话目录。

## 3. 模块结构

```
core/
  ports.py           端口（依赖倒置接缝）+ 内置模式标签 / Stream
  errors.py          领域错误
  scan.py            转义序列与多字节字符的边界扫描
  journal.py         输出字节日志 + 对齐纯函数 plan_attach
  views.py           字节视图（全量 / 切片 / 行 / offset / grep）
  session/
    state.py         SessionState / 状态机
    base.py          Session 抽象（身份 + 生命周期 + 摄入 + 视图）
    registry.py      会话注册表（uid 主键）
  terminal/
    session.py       TerminalSession（Pty 特化）
  process/
    session.py       ProcessSession（子进程特化）
```

## 4. 端口（依赖倒置）

核心层定义它需要宿主提供什么，运行时层提供实现：

**模式是开放字符串**：标签由接入方（会话实现 / 宿主）自己定义，核心层不校验，
也不把 pty / subprocess 当作封闭集合。下面两个只是**内置**形态所用的标签。

```python
PTY = "pty"
SUBPROCESS = "subprocess"


class Stream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True, slots=True)
class SessionSpec:
    mode: str
    argv: Sequence[str]
    cols: int = 80
    rows: int = 24
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    encoding: str = "utf-8"


@dataclass(frozen=True, slots=True)
class HostMetadata:
    title: str | None = None
    cwd: str | None = None


class HostLifecycle(Protocol):
    pid: int | None

    def read(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes: ...
    def write(self, data: bytes) -> None: ...
    def try_wait(self) -> int | None: ...
    def kill(self) -> None: ...
    def close(self) -> None: ...


class TerminalHost(HostLifecycle, Protocol):
    def ingest(self, data: bytes) -> bytes: ...          # 返回要回写应用的应答
    def resize(self, cols: int, rows: int) -> None: ...
    def snapshot(self) -> bytes: ...
    def metadata(self) -> HostMetadata: ...


class ProcessHost(HostLifecycle, Protocol):
    def read_stderr(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes: ...
    def close_stdin(self) -> None: ...


HostFactory = Callable[[SessionSpec], HostLifecycle]
```

## 5. 字节日志与对齐

日志是唯一真源：offset 从 0 起、单调递增、永不回退；区间语义为半开 `[start, end)`。

```python
@dataclass(frozen=True, slots=True)
class Resume:
    from_offset: int


@dataclass(frozen=True, slots=True)
class Rebuild:
    reason: str


class OutputJournal:
    def __init__(self, budget_bytes: int) -> None: ...

    @property
    def start_offset(self) -> int: ...        # 保留区间的起点（裁剪后 > 0）
    @property
    def end_offset(self) -> int: ...

    def append(self, data: bytes) -> None: ...
    def trim_to_budget(self) -> int: ...      # 返回裁剪字节数；裁剪点对齐转义边界
    def read(self, from_offset: int, length: int | None = None) -> bytes: ...

    def replay_offset(self) -> int: ...              # 尾部之前的最后一个干净边界
    def clean_offset_at_or_after(self, offset: int) -> int: ...


def plan_attach(journal: OutputJournal, cursor: int | None, end: int) -> Resume | Rebuild: ...
```

`plan_attach` 是**纯函数**，不碰连接、不碰订阅者：

| 情形 | 决策 |
|---|---|
| 全新订阅者（`cursor is None`） | `Resume(0)` |
| 断点在日志覆盖范围内 | `Resume(cursor)` |
| 断点早于 `start_offset` | `Rebuild(reason="trimmed")` |
| 断点晚于 `end` | 抛 `OffsetAhead`（协议不一致，不静默重同步） |

裁剪与对齐点都必须落在**解析状态干净**的位置（不在转义序列或多字节字符中间），否则重放会从序列中间切断、产生可见乱码。

## 6. 视图

字节视图直接切片日志，按需生成、不额外维护状态：

```python
def read_all(data: bytes) -> bytes: ...
def read_range(data: bytes, start: int, end: int | None = None) -> bytes: ...
def last_bytes(data: bytes, n: int) -> bytes: ...
def last_lines(data: bytes, n: int, *, encoding: str = "utf-8") -> str: ...
def line_range(data: bytes, start: int, end: int | None, *, encoding: str = "utf-8") -> str: ...
def grep(data: bytes, pattern: str, *, encoding: str = "utf-8", limit: int | None = None) -> list[str]: ...
```

屏幕视图（快照 / SVG / 行列 / 光标）只存在于 Pty 会话，由终端模型渲染，是派生视图。

## 7. 会话

```python
@dataclass(frozen=True, slots=True)
class IngestResult:
    stream: Stream
    start_offset: int
    end_offset: int
    response: bytes          # 终端模型要回写给应用的应答（子进程恒为空）


class Session:
    uid: str
    mode: str
    state: SessionState
    exit_code: int | None
    error: str | None
    start_time: float

    # 生命周期（所有者线程）
    def start(self) -> None: ...
    def stop(self, timeout: float = 5.0) -> None: ...
    def close(self) -> None: ...
    def refresh(self) -> None: ...                              # 同步宿主退出码与状态
    def expect_eof(self) -> None: ...                            # 声明有外部驱动
    @property
    def drained(self) -> bool: ...                               # 退出且不再有输出

    # 输出流（按流统一）
    def streams(self) -> tuple[Stream, ...]: ...
    def read_stream(self, stream: Stream, timeout=0.2, max_bytes=65536) -> bytes: ...
    def ingest_stream(self, stream: Stream, data: bytes) -> IngestResult: ...
    def mark_eof(self, stream: Stream) -> None: ...

    # 输入（写线程）
    def send(self, data: bytes) -> None: ...
    def close_stdin(self) -> None: ...                           # 子进程专用

    # 视图（所有者线程）
    @property
    def journal(self) -> OutputJournal: ...
    def journal_for(self, stream: Stream) -> OutputJournal: ...
    def read_all(self, stream: Stream = Stream.STDOUT) -> bytes: ...
    def read_range(self, start: int, end: int | None = None, stream: Stream = Stream.STDOUT) -> bytes: ...
    def attach_plan(self, cursor: int | None, stream: Stream = Stream.STDOUT) -> Resume | Rebuild: ...
    def snapshot(self) -> bytes: ...                             # Pty 专用
    def resize(self, cols: int, rows: int) -> None: ...           # Pty 专用
```

`TerminalSession` 与 `ProcessSession` 只实现两处差异：

| 差异 | TerminalSession | ProcessSession |
|---|---|---|
| 流数 | `(STDOUT,)` | `(STDOUT, STDERR)` |
| 摄入 | `host.ingest(data)` 喂终端模型并取回应答 | 无模型 |

### 7.1 退出与排空是两件事

- `exit_code` 一拿到就记（`refresh()` 从宿主取）。
- `drained` = "进程已退出 **且** 不再会有输出到达"。有外部驱动（读线程 / 泵）时，要等所有流都 EOF；没有外部驱动的会话则退出即结束。
- **EOF 不等于退出**：程序可以先关掉 stdout/stderr 而继续运行（守护进程、`exec`），所以每个流单独记 EOF。

进程退出与尾部输出到达之间有竞态：把两者混为一谈会丢掉最后一段输出。

## 8. 注册表

```python
class SessionRegistry:
    def __init__(
        self,
        host_factory: HostFactory,
        *,
        session_classes: Mapping[str, type[Session]] | None = None,
        journal_budget_bytes: int = 8 << 20,
    ) -> None: ...
    # session_classes：`模式标签 → 会话类`，缺省给内置两种；接入方可自带映射

    def create(self, spec: SessionSpec) -> Session: ...   # 查映射；未知标签报错
    def get(self, uid: str) -> Session: ...               # 不存在抛 SessionNotFound
    def find(self, uid: str) -> Session | None: ...
    def list(self) -> list[Session]: ...
    def close(self, uid: str) -> None: ...
    def close_all(self) -> None: ...
```

## 9. 线程归属（不变量）

| 方法 | 唯一允许的调用者 |
|---|---|
| `read_stream` | 读线程 |
| `ingest_stream` / `refresh` / `snapshot` / `resize` / `mark_eof` | 所有者线程 |
| `send` | 写线程 |

两条硬不变量：

1. `ingest_stream()` 内部 `_feed_model()` 与 `journal.append()` **相邻执行**（同线程、中间无 IO 与 await）→ `fed_offset == journal.end` 恒成立；
2. 因此 `snapshot()` 渲染期间不可能有 `ingest_stream()` 插入 → 快照恰是"重放 `[0, end)` 之后"的状态，与对齐点同源。

core 里因此**没有任何锁**。

## 10. 上层的调用序列

core 不驱动自己，由上层（命令层的驱动循环）驱动：

```python
session = registry.create(spec)
session.start()
session.expect_eof()                  # 由本驱动负责读空

while not session.drained:
    for stream in session.streams():
        chunk = session.read_stream(stream, timeout=0.02)
        if chunk:
            session.ingest_stream(stream, chunk)
            # ← 命令层在这里把 chunk 喂给它的等待引擎做条件匹配
    session.refresh()
    if session.exit_code is not None and <本轮两路都读空>:
        for stream in session.streams():
            session.mark_eof(stream)

session.close()
```

真实实现里这个循环由运行时层的 `SessionRunner` 承担：`read_stream` 交给每路一个读线程（经有界桥送到所有者线程），`send` 交给唯一的写线程（`submit_input` 入队）。**核心层的调用序列完全不变**。

## 11. 子进程核心

`ProcessSession` 与 `TerminalSession` 共享同一套 `Session` 抽象，差异只有两处：**没有终端模型**、**有两个输出流**。以下只写差异部分。

### 11.1 双流模型

- stdout 与 stderr 各自维护一份独立的 `OutputJournal`，**各自的 offset 从 0 起、单调递增**。
- **不合并**：两个管道本来就无法可靠交织——读到的先后取决于调度，不是程序的真实写出顺序。合并等于伪造一个假的顺序。
- 增量游标也是每流一个，互不影响；每流各自按预算裁剪，裁剪规则与对齐点同 5。

### 11.2 视图

只有字节视图（全量 / 最后 N 行 / 最后 N 字节 / 指定范围 / offset 增量 / 正则），**每流独立**。没有屏幕视图、没有 `snapshot()`、没有 `resize()`。

### 11.3 输入与 stdin

- `send(data)` 写裸字节到 stdin：**不做控制字符展开、不做键盘/鼠标编码**（那是终端输入编码的事）。
- **必须能显式关掉 stdin**（`close_stdin()`）：`cat`、`python -` 这类程序在等 EOF，不给 EOF 就永远不结束。

### 11.4 宿主端口

```python
class ProcessHost(HostLifecycle, Protocol):
    def read_stderr(self, max_bytes: int = 65536, timeout: float | None = 0.2) -> bytes: ...
    def close_stdin(self) -> None: ...
```

数据怎么来（读线程阻塞读、还是事件循环的管道回调）是运行时层的事；核心层只负责把拿到的字节喂进日志（`ingest_stream`），并保证"摄入与日志追加相邻执行"这条不变量对每一路都成立。

### 11.5 线程

子进程会话的每一路输出各一个读线程（stdout / stderr）；写线程随客户端输入接入。

### 11.6 易错点

- **EOF ≠ 退出**：程序可以先关掉 stdout / stderr 而继续运行（守护进程、`exec`）。所以**每个流单独记 `eof`**，绝不能把"流读完"当成"进程退出"。`ended` / `crashed` 只由退出码判定。
- **退出码语义**：`0 → ended`，非 `0 → crashed`（由命令层的等待引擎解释）。
- **"查到子进程启动 → 子进程终止"** 那条返回条件属于**终端会话**（shell 里跑的命令何时结束），依赖进程树追踪——它既不是核心层的判定，也不是子进程会话的概念。

## 12. 不属于核心层的东西

明确划出去，避免又混回来：

| 东西 | 归属 | 理由 |
|---|---|---|
| 返回条件词汇与等待引擎 | 命令层（service） | 它是"命令该等到什么时候返回"的判定，不是终端状态 |
| 正则匹配 / 回显抑制 | 命令层（service） | 匹配是命令层的语义 |
| `sid` / tags / 会话级配置 | 命令层（service） | 用户标识与元数据 |
| 订阅者、游标、出站队列、推送 | 命令层（service） | 传输编排 |
| 宿主实现（PTY / 子进程 / 进程树） | 运行时层（runtime） | 唯一碰原生扩展与平台 API |
| 线程、事件循环、桥 | 运行时层（runtime） | 并发与阻塞 I/O |

核心层只把**原料**交给上层：字节日志（`read_all` / `read_range` / `journal_for`）、offset 区间（`ingest_stream` 的返回值）、对齐决策（`attach_plan`）、退出与排空（`exit_code` / `drained` / `eof_streams`）、生命周期状态。
