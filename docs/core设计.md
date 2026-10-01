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
    def descendants(self) -> tuple[int, ...]: ...
    def close(self) -> None: ...


class TerminalHost(HostLifecycle, Protocol):
    def ingest(self, data: bytes) -> bytes: ...          # 返回要回写应用的应答
    def resize(self, cols: int, rows: int) -> None: ...
    def rebuild_bytes(self) -> bytes: ...                # 重建字节（重同步用），不是屏幕内容
    # 屏幕视图（派生，按需渲染）
    def screen_text(self) -> str: ...                    # 可见屏幕纯文本
    def full_text(self) -> str: ...                      # 全量输出：含滚动历史的可见文本
    def screen_cells(self) -> tuple[tuple[str, ...], ...]: ...   # 字符格栅（宽字符续格为空串）
    def render_svg(self) -> str: ...
    def render_image(self, *, scale: float = 1.0, fmt: str = "png") -> bytes: ...
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
| 全新订阅者（`cursor is None`） | 视作游标 `0`：未裁剪时 `Resume(0)`，已裁剪时同下行走重建 |
| 断点在日志覆盖范围内 | `Resume(cursor)` |
| 断点早于 `start_offset` | `Rebuild(reason="trimmed")` |
| 断点晚于 `end` | 抛 `OffsetAhead`（协议不一致，不静默重同步） |

裁剪与对齐点都必须落在**解析状态干净**的位置（不在转义序列或多字节字符中间），否则重放会从序列中间切断、产生可见乱码。

**边界扫描的实现分两层**：`iter_boundaries` 是逐字节的参考实现（直白、慢），只作为差分测试的对照物；热路径把"整段普通字节"用正则一次吃掉、把序列结尾交给 C 层 `search`。`clean_offset_at_or_after` / `replay_offset` 与参考实现**逐字节一致**；`trim_cut_offset`（`trim_to_budget` 用的那个）只求"≥ 超出量的**某个**干净边界"，**不保证最小**（上限晚 64 字节），换来一次 C 速扫描就定界——日志满载后每次摄入都会裁剪，逐字节解析会成为主人侧循环的主要开销。局部判断不成立时（多字节字符跨过候选、区间里有字符串序列开头、候选紧跟 ESC 定不了归属）一律退回精确实现。

**未收尾的字符串序列**（截断的 OSC 52、二进制垃圾里的 `ESC ]`）不存在任何干净边界，此时 `trim_to_budget` 只能整段裁掉——所有订阅者随之重建、历史全丢。这是刻意的，不是缺陷。

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

屏幕视图只存在于 Pty 会话，由终端模型渲染，是派生视图：

- `screen_text()`：可见屏幕纯文本。
- `full_text()`：**全量输出**——含滚动历史的可见文本（历史区 + 可见区）。
- `screen_cells()`：可见屏幕的字符格栅，宽字符的续格为空串；按格取用（如"可见屏幕第 N 列"）由上层处理。
- `render_svg()` / `render_image()`：可见屏幕的矢量 / 位图。

`rebuild_bytes()` **不在返回数据之列**：它返回的是"喂进一个空终端模型即可还原当前状态"的重建字节，供订阅者游标落后时重同步（`plan_attach → Rebuild`），不是给调用方看的屏幕内容。

**"全量输出"只有一个含义**：含滚动历史的所有可见文本。字节侧的对应物叫**字节流全量**（字节日志本身），两者不可混用——一个是解析后的可见文本，一个是未经解析的原始字节。

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
    def descendants(self) -> tuple[int, ...]: ...                # 进程树成员（轮询式，不含根）
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
    def rebuild_bytes(self) -> bytes: ...                        # Pty 专用（重同步用，非屏幕内容）
    def screen_text(self) -> str: ...                            # Pty 专用
    def full_text(self) -> str: ...                              # Pty 专用
    def screen_cells(self) -> tuple[tuple[str, ...], ...]: ...    # Pty 专用
    def render_svg(self) -> str: ...                             # Pty 专用
    def render_image(self, *, scale: float = 1.0, fmt: str = "png") -> bytes: ...  # Pty 专用
    def resize(self, cols: int, rows: int) -> None: ...           # Pty 专用
```

`TerminalSession` 与 `ProcessSession` 只实现两处差异：

| 差异 | TerminalSession | ProcessSession |
|---|---|---|
| 流数 | `(STDOUT,)` | `(STDOUT, STDERR)` |
| 摄入 | `host.ingest(data)` 喂终端模型并取回应答 | 无模型 |

### 7.1 退出与排空是两件事

- `exit_code` 一拿到就记：`refresh()` 从宿主轮询（进入退出态后仍继续补拿），`stop()` 强杀后也要等它出现（强杀后退出码不会立刻可见）。留空会让 `drained` 永远为假。
- `drained` = "不再会有输出到达"：已关闭的会话宿主已释放，恒为真；否则要求进程已退出——有外部驱动（读线程 / 泵）时还要等所有流 EOF，没有外部驱动的会话则退出即结束。
- **EOF 不等于退出**：程序可以先关掉 stdout/stderr 而继续运行（守护进程、`exec`），所以每个流单独记 EOF。
- **读空不等于 EOF**：`read_stream` 的空返回值只表示本轮无数据（超时）。宿主已释放（从未启动或已关闭）时它明确报错，不静默返回空——否则驱动方会把"没有宿主"当成"暂时没输出"而空转。

进程退出与尾部输出到达之间有竞态：把两者混为一谈会丢掉最后一段输出。

## 8. 注册表

**会话形态 = 会话类 + 宿主工厂，成对注册**：同一个模式标签的会话类与宿主来源必须
一起声明。拆成两份映射各自维护，就会出现"假宿主配真会话类"这类只在运行期才炸的组合。

```python
@dataclass(frozen=True, slots=True)
class SessionKind:
    session_class: type[Session]
    host_factory: HostFactory | None = None   # 留空 = 用注册表的默认工厂


class SessionRegistry:
    def __init__(
        self,
        host_factory: HostFactory,
        *,
        kinds: Mapping[str, SessionKind] | None = None,
        journal_budget_bytes: int = 8 << 20,
    ) -> None: ...
    # kinds：`模式标签 → 会话形态`，缺省给内置两种；接入方可自带映射

    def create(self, spec: SessionSpec) -> Session: ...   # 查映射；未知标签报错
    def get(self, uid: str) -> Session: ...               # 不存在抛 SessionNotFound
    def find(self, uid: str) -> Session | None: ...
    def list(self) -> list[Session]: ...
    def detach(self, uid: str) -> Session: ...            # 只摘除，不释放；`close` 由它拼成
    def close(self, uid: str) -> None: ...
    def close_all(self) -> None: ...
```

`detach` 是给"两阶段释放"留的（见架构设计 §11）：宿主关闭在部分平台上会长时间阻塞，
压着事件循环会冻住所有会话，所以上层要能**先把会话从列表里摘掉**（不再被轮询、不再扇出），
再把耗时的关闭交给别的线程。`close` 就是"摘除 + 关闭"的合体，两者共用同一条摘除路径。

## 9. 线程归属（不变量）

| 方法 | 唯一允许的调用者 |
|---|---|
| `read_stream` | 读线程 |
| `ingest_stream` / `refresh` / `descendants` / 屏幕视图读取 / `resize` / `mark_eof` | 所有者线程 |
| `send` | 写线程 |

两条硬不变量：

1. `ingest_stream()` 内部 `_feed_model()` 与 `journal.append()` **相邻执行**（同线程、中间无 IO 与 await）→ `fed_offset == journal.end` 恒成立；
2. 因此读取屏幕（`screen_text()` / `rebuild_bytes()` 等）期间不可能有 `ingest_stream()` 插入 → 读到的恰是"重放 `[0, end)` 之后"的状态，与对齐点同源。

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

只有字节视图（全量 / 最后 N 行 / 最后 N 字节 / 指定范围 / offset 增量 / 正则），**每流独立**。没有屏幕视图、没有 `rebuild_bytes()`、没有 `resize()`。

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

核心层只把**原料**交给上层：字节日志（`read_all` / `read_range` / `journal_for`）、offset 区间（`ingest_stream` 的返回值）、对齐决策（`attach_plan`）、屏幕视图（`screen_text` / `full_text` / `screen_cells` / `render_svg` / `render_image`）、重建字节（`rebuild_bytes`）、退出与排空（`exit_code` / `drained` / `eof_streams`）、进程树成员（`descendants`）、生命周期状态。
