# opstools

一个由单文件运维工具组成的集合。每个工具都是一份可独立执行的脚本——
Python、shell 或其他合适的语言——彼此不共享代码：无安装器、无后台服务、
无框架。拷到机器上，读完源码即可放心使用。后续会陆续加入更多工具。

English: [README.md](README.md)

## 工具

| 工具                | 语言         | 说明                                                                                   |
| ------------------- | ------------ | -------------------------------------------------------------------------------------- |
| [keydist](#keydist) | Python 3.11+ | 把一份集中清单里的 secrets 增量分发到各消费端配置文件（`.env` / JSON / 整文件），内容变化时执行 post 命令  |
| [dly](#dly)         | Python 3.8+  | 打开、查看、列出每日笔记与任务笔记；简短日期表达式与可组合偏移，编辑器与查看命令可配置 |
| [fo](#fo)           | Bash 4+      | 用 fzf 选取文件并按映射表或默认程序打开；fd/fdfind/find 候选、可选预览、dry-run |

## 设计原则

- **单文件、零依赖** — 每个工具一份可执行脚本，仅依赖所用语言的标准库；
  语言按工具需求选择，见表格标注。
- **Fail closed** — 配置畸形或有歧义一律硬报错，绝不静默降级成不安全行为。
- **幂等** — 重复运行收敛到同一状态；副作用（重写文件、钩子、命令）只在
  真实变化时发生一次。
- **本地优先** — 配置放 `~/.config/`（机器级配置放 `/etc/`），可执行文件
  放 `~/.local/bin/`，不联网。

## 安装

各工具相互独立，按需从仓库直接安装到 `~/.local/bin`：

```bash
install -m755 keydist.py ~/.local/bin/keydist
install -m755 dly.py ~/.local/bin/dly
install -m755 fo.sh ~/.local/bin/fo
```

---

## keydist

`keydist` 按下述规则定位清单，解析其中引用的 secrets，按各消费端能理解的
格式**增量**更新其配置文件（`.env` / JSON / 整文件）；文件内容实际变化时
执行可选的 `post` 命令（例如重启服务）。

### 十秒看效果

仓库自带一份演示清单，一条命令即可看到完整流程——待发生变更的 diff 与
将要执行的 post 命令，不写任何文件、不执行任何命令：

```bash
keydist -c examples/keydist.example.toml --dry-run
```

```console
keydist: manifest examples/keydist.example.toml (1 target(s))
keydist: DRY-RUN — nothing will be written, no command executed
demo (shell, default)
  would update  examples/demo.env
    --- examples/demo.env (current)
    +++ examples/demo.env (proposed)
    @@ -0,0 +1,2 @@
    +API_KEY='demo-key-12345'
    +DOMAIN='example.com'
  post: would run: echo 'demo.env updated'
dry-run complete: 1 target(s): would update 1, unchanged 0
```

### 使用场景

- **API 密钥的单一来源** — 本地多个工具（nvim AI 插件、aider、litellm 等）
  各自读自己的 `.env`，却需要同一批上游密钥。每把密钥只在清单里存一次，
  keydist 推送到所有消费端，无需手动逐个编辑十几个文件。
- **同一密钥、不同格式** — 同一个值在 shell `source` 的文件里要写成
  `KEY='v'`，在 systemd `EnvironmentFile=` 里要写成 `KEY="v"`；keydist
  从一份清单按目标渲染各自的格式。
- **一次轮换，处处生效** — 修改密钥真正的来源（env 变量 / dotenv / 文件）
  后重新运行 keydist：仅重写受影响的文件，并自动执行各目标的 `post`
  （如重启服务）。
- **改动前先预览** — `--dry-run` 以 unified diff 展示全部待发生变更，
  确认无误再落地。
- **在清单里组装值** — `concat` 拼接连接串、`transform` 做哈希/编码等闭集
  变换、`pipe` 交给 `age` 等外部命令处理（如解密线下加密的密文），全部无需
  离开清单。

### 清单格式

给出 `--config PATH` / `-c PATH` 时直接使用该文件（必须存在）；否则按
以下查找顺序取第一个存在的清单：

1. `$XDG_CONFIG_HOME/keydist/keydist.toml`（默认 `~/.config/keydist/keydist.toml`）
2. `~/.local/etc/keydist.toml`（旧默认路径）
3. `/etc/keydist/keydist.toml`

```toml
# ~/.config/keydist/keydist.toml   （建议 chmod 600）

[secrets]
# 裸字符串/数字/布尔 = 字面量，原样写入（布尔渲染为 true/false）：
domain  = "example.com"
retries = 3

# 表 = 引用，运行时解析：
api_key = { env = "API_KEY" }                                 # 进程环境变量
db_pass = { envfile = "~/.config/db.env", key = "password" }  # dotenv 文件中的键
ssh_key = { file = "~/.ssh/id_ed25519" }                      # 整文件内容，去末尾换行

# TOML 内联表不可跨行；多元素 chain / concat / pipe 请用子表写法：
[secrets.tls_cert]
chain = [                        # 回退链：逐个尝试，首个成功者生效
  { env = "TLS_CERT" },
  { file = "~/.local/etc/tls/cert.pem" },
]

[secrets.db_url]
concat = ["postgresql://", { env = "DB_USER" }, ":", { env = "DB_PASS" },
          "@db.internal/prod"]   # 按序拼接；任一元素失败整体失败

[secrets.seed_b64]
env = "SEED"
transform = ["trim", "b64"]      # 闭集 op，按序应用

# 加密等超出字符串拼接的处理交给外部命令（stdin 进、stdout 出）。在 pipe 里
# 做加密是非确定性的——age 每次运行都生成新文件密钥——因此取确定性的方向：
# 线下加密一次、每次运行解密。cmd 不经 shell 执行，cmd 里的 ~ 须写绝对路径
# （"file" 引用里的 ~ 由 keydist 展开）：
[secrets.seed]
pipe = { cmd = "age -d -i /home/me/.config/age/key.txt", input = { file = "~/secrets/seed.age" } }

[targets."myapp"]
path = "~/srv/myapp/.env"                  # 必填；展开 ~，自动创建父目录
style = "dotenv"                           # 可选；shell / dotenv / systemd / docker（覆盖自动推断）
post = "systemctl --user restart myapp"    # 可选；仅在文件实际变化时执行

[targets."myapp".vars]                     # 消费端键名 = [secrets] 中的 secret 名
API_KEY     = "api_key"
DB_PASSWORD = "db_pass"
DOMAIN      = "domain"
TLS_CERT    = "tls_cert"

# 省略 style 时按目标的名称/路径/post 自动推断（此处因 systemctl 推断为
# "systemd"）；均未命中时使用默认 "shell"。
[targets."myapi"]
path = "/etc/myapi/myapi.env"
post = "systemctl restart myapi"

[targets."myapi".vars]
API_KEY = "api_key"
DOMAIN  = "domain"

# format = "json"：键值合并进顶层 JSON object。.json 后缀可自动推断（省略
# format）；现存文件必须是合法 object。幂等比较基于解析后相等——仅排版
# 不同不算变化、不触发 post。键不受环境变量命名约束。
[targets."myweb"]
path = "/etc/myweb/config.json"

[targets."myweb".vars]
"api-key" = "api_key"

# format = "raw"：单个 secret 即整个文件内容（token、license 等），必须
# 显式声明；值原样写出（UTF-8）、不追加换行，无 vars。
[targets."mytok"]
path = "/etc/mytok/token"
format = "raw"
secret = "api_key"
post = "systemctl restart mytok"
```

### secret 形式

| 形式                                | 含义                                                                 |
| ----------------------------------- | -------------------------------------------------------------------- |
| 裸字符串/数字/布尔                  | 字面量（布尔渲染为 `true` / `false`）                                |
| `{ env = "NAME" }`                  | 进程环境变量 `NAME`                                                  |
| `{ envfile = "PATH", key = "KEY" }` | dotenv 文件中 `KEY` 的值；文件恰含一对键值时可省略 `key`             |
| `{ file = "PATH" }`                 | 整文件内容，去末尾换行（适合私钥）                                   |
| `{ chain = [e1, e2, …] }`           | 逐元素尝试，首个成功者生效；元素可为字面量或引用（允许嵌套 `chain`） |
| `{ concat = [e1, e2, …] }`          | 按序连接全部元素；任一元素失败则整体失败（元素与 `chain` 相同）      |
| `{ pipe = { cmd = "…", input = … } }` | 外部命令处理：可选 `input`（字面量或引用）写入 stdin，stdout（去末尾换行）为值；非零退出即失败 |

引用表必须恰含 `env` / `envfile` / `file` / `chain` / `concat` / `pipe`
之一，未知键或冲突键一律硬报错。裸字符串即字面量、由类型区分，因此不存在
可拼错的前缀——写错的引用（如 `{ en = "X" }`）会响亮失败，而不是把字面量
悄悄写进消费端。

任意引用表可加可选的 `transform = [op, …]`：引用解析成功后按序应用一组闭
集的确定性字符串变换——`trim`、`lower`、`upper`、`b64`、`hex`、`sha256`、
`sha512`、`urlenc`；未知 op 一律硬报错。变换在目标 style 校验之前执行，
变换后的值与其他值一样接受校验。要对字面量做变换，包一层即可：
`{ chain = ["raw"], transform = ["trim"] }`。

`pipe` 把拼接之外的复杂处理——加密、压缩、接入 secret manager（省略
`input` 的 `{ pipe = { cmd = "op read op://…" } }`）——交给你本就信任的
外部工具，而不重新实现密码学。secret 经 stdin 传递、绝不进入 argv
（`/proc` 可见、引号转义易错）。pipe 命令在 `--dry-run` 下同样执行——
diff 需要真实值——因此必须确定性且无副作用；同一 secret 跨目标解析出
不同值时会告警，否则每次运行都会被当作“有变化”。注意方向：`age` 这类
工具的加密输出天生随机，pipe 应用于解密一份只加密一次的密文（或用固定
`-S` 盐的 `openssl enc`），而不是现场加密。

### 输出 format

每个目标按三种 format 之一写入：可显式指定 `format = "..."`，也可由路径
后缀自动推断（`.json` → `json`）；raw 无稳定路径特征，必须显式声明：

| Format        | 写入形态                                                                  |
| ------------- | ------------------------------------------------------------------------- |
| `env`（默认） | 键值行，按 style 渲染引号（见下表）                                       |
| `json`        | 合并进顶层 JSON object；现存文件必须是合法 object，否则该目标整体失败     |
| `raw`         | 单个 secret（`secret = "名字"`）即整个文件内容；原样写出、不追加换行      |

`json` 的幂等比较基于解析后相等：现存文件仅排版不同（缩进/紧凑）而值相同
时不重写、不触发 `post`；值恒写为 JSON 字符串。`raw` 需要尾随换行时由清单
用 `concat` 显式拼接。

### 输出 style（format = env）

format 为 `env` 的目标按四种 style 之一渲染：可显式指定 `style = "..."`，
也可由目标的名称/路径/post 命令自动推断（json / raw 目标不接受 style）：

| Style           | 引号处理                | 典型消费端                           |
| --------------- | ----------------------- | ------------------------------------ |
| `shell`（默认） | POSIX 单引号            | 任何可 `source` 的场景；最大兼容超集 |
| `dotenv`        | 双引号，`\n` 展开为换行 | node/python/Go/Ruby dotenv 解析器    |
| `systemd`       | 双引号，无 `export`     | systemd `EnvironmentFile=`           |
| `docker`        | 裸值，无任何转义        | `docker run --env-file`              |

写入前对 transform 之后的值执行与 style 相关的校验：`systemd` 与 `docker`
拒绝多行值；`docker` 会就 compose 与 `docker run` 的引号语义分歧给出警告。

### 行为细节

- 仅在内容变化时重写文件；未变化的文件保持 mtime 并跳过 `post`。
- `--dry-run` / `-n` 打印每个待发生变更的 unified diff 及将要执行的
  `post` 命令——不写文件、不改权限、不执行任何命令。
- `-v` / `--verbose` 在实际分发时同步打印每个变更目标的 unified diff
  （diff 含 secret 明文，注意终端与日志留痕）；`--dry-run` 恒打印 diff，
  不受此开关影响。
- 目标文件始终确保 `0600`；新建文件从一开始就是 `0600`。
- `post` 经 `shlex` 切分执行，超时 30 秒；非零退出或超时视为该目标失败。
- `pipe` 命令同样经 `shlex` 切分、超时 30 秒，且在 `--dry-run` 下也会执行；
  须确定性、无副作用。
- 同一 secret 跨目标解析值不一致时告警（多半是 `pipe` 引入了非确定性）；
  否则这类目标永远“有变化”、`post` 每次都会触发。
- 自动清理历史运行遗留的重复键（附带警告）。
- `json` 目标以解析后的值判断变化：仅排版不同不重写、不触发 `post`；
  `raw` 目标值原样落盘、不追加换行。
- 启动时检查清单权限：组/其他用户可读会告警（其中可能含 secret 引用）。
- 输出在终端上着色；管道输出或设置了 `NO_COLOR` 时自动禁用。
- 全部目标成功退出码 `0`；否则退出码 `1`，stderr 打印失败汇总。

### 用法

```bash
keydist                 # 按清单执行分发
keydist -v              # 同上，并同步打印各目标变更的 unified diff
keydist --dry-run       # 显示待发生变更的 unified diff，不做任何改动
keydist -c ~/alt.toml   # 显式指定清单
```

### 快速开始

编写 `~/.config/keydist/keydist.toml`（建议 `chmod 600`），然后运行
`keydist`（安装见[安装](#安装)）。

### 速查表

| 命令                 | 作用                                 |
| -------------------- | ------------------------------------ |
| `keydist`            | 按查找顺序定位清单并执行分发         |
| `keydist -c PATH`    | 使用显式清单                         |
| `keydist -v`         | 实际分发并同步打印各目标变更的 diff  |
| `keydist --dry-run`  | 预览 diff 与 post 命令，不做任何改动 |
| `keydist -n -c PATH` | 同上，使用显式清单                   |

### FAQ

**Windows 下能用吗？**

面向 Linux/macOS：post 经 `shlex` 切分后走 POSIX shell，典型场景是
`systemctl`；未在 Windows 上验证。

**某个 secret 解析失败会怎样？**

整个目标在写入前即被放弃（fail closed）；本次运行退出码 `1`，stderr
打印失败汇总。

**每次运行都会重写所有文件吗？**

不会——只有渲染结果与现有内容不同才写入；未变化的目标保持 mtime 并
跳过 `post`。

**为什么清单权限过宽会告警？**

清单里可能含 secret 引用；组/其他用户可读时告警，建议 `chmod 600`。

**为什么 keydist 不自己做加密？**

密码学交给外部：用 `age`、`openssl` 等你本就信任的工具线下加密一次，
`pipe` 经 stdin 把密文交给同一工具、每次运行解密。单文件工具内置密码学
会带来它无法回答的密钥管理问题——答错的代价是静默泄露。

---

## dly

`dly` 是每日笔记与任务笔记的命令行入口：计算日期、定位文件、调用编辑器
或分页器，本身不解析、不修改笔记内容。普通笔记与任务笔记可共用一个目录，
也可为任务笔记单独配置目录；全部配置集中在脚本最前端。

### 十秒看效果

默认 `NOTES_DIR`（`~/Documents/notes/daily`）下已有若干笔记时，列表与查看
完全不需要打开编辑器：

```console
$ dly -l -a            # 全部类型，字典序逆序
global.task.md
global.md
20261105.task.md
20261105.md
20261001.task.md
20251231.md

$ dly -l 2026          # 普通类型，文件名含 "2026"
20261105.md
```

动手试一试——查看与列表均为只读，不会改动任何笔记：

```bash
mkdir -p ~/Documents/notes/daily            # dly 不会自动创建目录
printf 'hello dly\n' > ~/Documents/notes/daily/20261105.md
dly -v 20261105                             # 用内置分页器查看
dly -l 2026                                 # 列出匹配的文件名
```

### 使用场景

- **一条命令写日记** — `dly` 直接打开今天的 `YYYYMMDD.md`；`dly 1105`、
  `dly +1m`、`dly 2026-11-05` 等简短表达式免去算日期、拼路径的步骤，
  偏移还可在基准日期上组合：`dly 1101 +3m -2d`。
- **日记与任务分离** — `dly task` 操作 `YYYYMMDD.task.md`，可为任务笔记
  配置独立目录，与普通笔记互不混杂。
- **全局笔记** — `dly global`、`dly g t` 打开不带日期的 `global.md` 与
  `global.task.md`。
- **只读回顾与盘点** — `dly -v` 只查看不编辑；`dly -l -a 2026` 按子串或
  glob 筛选文件名，快速盘点已有笔记。
- **快速进入笔记目录** — `dly cd [task|t]` 在普通/任务目录中启动交互式
  子 shell；`dly cd -p` 仅输出路径。

### 配置

配置集中在脚本最前端（不读环境变量、无配置文件），按需修改后直接使用：

```python
NOTES_DIR  = Path.home() / "Documents/notes/daily"  # 普通笔记目录（含 global.md）
TASK_DIR   = None                # None = 与 NOTES_DIR 共用；也可设为独立目录
EDITOR_CMD = ["nvim"]            # 编辑器命令，可带参数，如 ["code", "--wait"]
VIEWER_CMD = None                # None = 使用标准库 pydoc.pager；或 ["less", "-R"]
```

脚本不自动创建目录：目录不存在时硬报错退出。编辑模式下目标文件不存在时
交由编辑器决定是否创建；查看模式下文件不存在则报错退出。

### 文件命名

| 类型         | 文件名             | 示例               |
| ------------ | ------------------ | ------------------ |
| 普通日期笔记 | `YYYYMMDD.md`      | `20261105.md`      |
| 日期任务笔记 | `YYYYMMDD.task.md` | `20261105.task.md` |
| 全局普通笔记 | `global.md`        | —                  |
| 全局任务笔记 | `global.task.md`   | —                  |

### 日期表达式

| 表达式                                                  | 含义                              | 示例（今天为 2026-11-05） |
| ------------------------------------------------------- | --------------------------------- | ------------------------- |
| （空）                                                  | 今天                              | → `20261105.md`           |
| `DD` / `MMDD` / `YYMMDD`                                | 本月某日 / 今年月日 / 20YY 年月日 | `110312` → 2011-03-12     |
| `YYYYMMDD` / `YYYY-MM-DD` / `YYYY/MM/DD` / `YYYY.MM.DD` | 完整日期，年份不限                | `19991231`                |

### 日期偏移

偏移是叠加在基准日期上的辅助表达式：首个位置参数可为基准日期表达式，
其后只能是偏移参数；不写基准时以今天为基准——单个偏移因此依旧可用
（`dly +1m`）。偏移从左到右逐个在前一结果上应用，可混用、可重复：

| 偏移           | 含义        |
| -------------- | ----------- |
| `+N` / `-N[d]` | ±N 天       |
| `+Nw` / `-Nw`  | ±N 周       |
| `+Nm` / `-Nm`  | ±N 个日历月 |
| `+Ny` / `-Ny`  | ±N 个日历年 |

`dly 1101 +3m -2d` 即 11 月 1 日加 3 个月再减 2 天（2027-01-30）；
`dly +3m -2d` 则以今天为基准做同样的计算。

月、年加减按日历进行：目标月不存在该日时截断到月末（2026-01-31 执行
`+1m` 得 2026-02-28）；2 月 29 日落到非闰年时取 2 月 28 日。截断逐步
进行——每个偏移都先落在截断后的真实日期上，因此
`2026-01-31 +1m -1d` 得 2026-02-27。

日期表达式与偏移仅在打开/查看模式下解析；列表模式（`-l`）中这些记号
只是普通文件名 pattern。

### 用法

```bash
dly                            # 编辑今天普通笔记
dly -v 2026-11-05              # 查看指定日期笔记
dly -p 2026-11-05              # 输出该笔记的真实文件路径
dly task                       # 编辑今天任务笔记
dly t -v +1w                   # 查看下周任务笔记
dly 1101 +3m -2d               # 编辑 11 月 1 日 +3 个月 -2 天（从左到右）
dly +3m -2d                    # 以今天为基准组合偏移
dly global task                # 编辑 global.task.md（dly task global 亦可）
dly -l                         # 列出普通笔记文件名（字典序逆序）
dly -l -a 2026                 # 全部类型中筛选文件名含 2026 的文件
dly task -l '*01.task.md'      # 任务类型，按 glob 模式筛选
dly cd                         # 在 NOTES_DIR 中启动子 shell
dly cd t -p                    # 不进入，仅输出 TASK_DIR 路径（共用时同 NOTES_DIR）
dly -h                         # 显示用法说明后退出
```

- `-v` / `--view` 只读查看：默认用标准库 `pydoc.pager` 分页，配置
  `VIEWER_CMD` 后改用外部命令；文件不存在报错退出。
- `-p` / `--print` 输出解析后的真实文件路径而不打开：要求所在目录
  存在，文件本身可以尚不存在。
- `-l` / `--list` 每行输出一个文件名，按字典序逆序；pattern 不含
  `* ? [` 时按 `*pattern*` 子串匹配，否则按 glob 匹配；无匹配退出码 1；
  `+1m` 这类日期记号不在此解析，只是文件名 pattern。
- `cd [task|t]` 在目录中启动交互式子 shell（`$SHELL`，Windows 取
  `%COMSPEC%`），`exit` 退出后回到原目录，子 shell 退出码原样透传；
  加 `-p` 时仅输出目录路径、不进入。
- `-h` / `--help` 在参数任意位置出现即输出用法说明到 stdout 并以退出码
  0 结束，优先于其他参数解析。
- 编辑器与查看命令的退出码原样作为脚本退出码。

### 速查表

| 命令                       | 作用                                        |
| -------------------------- | ------------------------------------------- |
| `dly`                      | 编辑今天的普通笔记（`YYYYMMDD.md`）         |
| `dly 1105` / `dly +1m`     | 编辑对应日期的普通笔记                      |
| `dly 1101 +3m -2d`         | 在基准日期上组合偏移（从左到右）            |
| `dly task` / `dly t`       | 编辑今天的任务笔记                          |
| `dly g t`                  | 编辑 `global.task.md`（`task global` 亦可） |
| `dly -v …`                 | 以上任意形式，改为只读查看                  |
| `dly -p …`                 | 以上任意形式，改为输出真实文件路径          |
| `dly -l [-a] [pattern]`    | 列出普通 / 全部类型                         |
| `dly task -l [pattern]`    | 列出任务类型                                |
| `dly cd [task\|t]`         | 在普通 / 任务目录中启动子 shell             |
| `dly task cd` / `dly t cd` | 同上，task 关键字在前                       |
| `dly cd t -p`              | 仅输出目录路径，不启动子 shell              |
| `dly -h` / `dly --help`    | 显示用法说明后退出                          |

### FAQ

**为什么不自动创建目录和文件？**

刻意为之：目录缺失一律硬报错，编辑模式下文件创建交给编辑器——绝不产生
意外产物。

**`+1m` 以哪一天为基准？**

单独使用时是今天；与基准日期表达式同用时以该基准为准
（`dly 1105 +1m`）。`dly 1105` 指当前年的 11 月 5 日。

**退出码如何约定？**

成功为 `0`；用法错误、无效日期、目录/文件缺失、列表无匹配为 `1`；
编辑器与查看命令的退出码原样透传。

**`dly cd` 会切换当前目录吗？**

不会——子进程无法修改父 shell 的工作目录。
`dly cd` 在目标目录中启动一个交互式子 shell：在其中工作即身处该目录，
`exit` 退出后回到原目录。只需路径时用 `dly cd -v`（如 `cd $(dly cd -v)`）。

**Windows 下能用吗？**

可以——纯标准库。把 `EDITOR_CMD` 指向任意编辑器，`VIEWER_CMD` 可设为
如 `["more.com"]`；各平台示例见脚本注释。

---

## fo

`fo` 是一个文件选择器：把候选列表（fd / fdfind / find，按序回退）交给
fzf，再把选中的每个文件交给打开器——显式 `OPENER` 参数、扩展名映射表或
桌面默认程序（Linux 的 `xdg-open`、macOS 的 `open`）。它本身不编辑文件。
需要 bash >= 4 与 fzf。

### 十秒看效果

dry-run 照常启动 fzf 供选择，选完后只打印将执行的命令——不打开任何文件、
不写缓存：

```bash
fo --dry-run
```

```console
$ fo --dry-run
would run: fd --type f . . | fzf
would run (detach): xdg-open README.md
```

动手试一试——dry-run 不产生任何副作用：

```bash
fo --dry-run                                # 选一个文件；只打印命令
fo                                          # 再选一次；这次真正打开
```

### 使用场景

- **双手不离键盘打开文件** — 任意目录运行 `fo`，fzf 里输入模糊查询、
  回车，文件即在正确的程序中打开。
- **按扩展名映射打开器** — 在配置文件里一次性写好
  `pdf = okular --unique`、`txt = kate` 等；其余文件回落到
  `xdg-open`/`open`。
- **TUI 与 GUI 各归其位** — nvim/less 等 TUI 程序前台运行占用终端，GUI
  程序脱钩运行不占终端；映射表 `!`/`&` 前缀与 `-t`/`-x` 可覆盖自动判定。
- **深目录反复翻找** — `fo -l` 沿用上一次 `-d` 的目录，反复处理
  `~/src` 时无需重复输入路径。
- **打开前先预览** — `fo -p` 内置预览：文本用 `bat`、图片用
  `chafa`/`viu`、PDF 首页用 `pdftotext`，缺什么自动降级为 `head`。
- **打开前先核对** — `fo -n` 打印将执行的命令而不执行，也不写缓存。

### 配置

打开器映射表位于 `${XDG_CONFIG_HOME:-~/.config}/fo/opener.conf`
（`FO_OPENER_CONF` 可覆盖路径）：

```ini
# 扩展名 = 命令与可选参数（按空白拆分）
pdf = okular --unique
txt = kate
png = gwenview

# 命令前的 ! / & 前缀控制运行方式: ! = 前台, & = 脱钩
md  = !nvim
pdf = &okular
```

- 扩展名与选中文件的扩展名大小写不敏感比较。
- 值按空白拆分为命令与参数；不支持含空格的单个参数，也不支持行尾注释。
- `!` / `&` 前缀可与命令名粘连（`!nvim`）或以空白分隔（`! nvim`），两者等价。
- 每个文件的打开器查找顺序：显式 `OPENER` 参数 → 映射表 →
  `xdg-open`（Linux）/ `open`（macOS）。

### 用法

```bash
fo                        # 当前目录选文件，用默认打开器打开
fo kate                   # 选文件，固定用 kate 打开
fo -d ~/src               # 在 ~/src 下搜索
fo -l                     # 复用上一次 -d 的目录
fo -m                     # 多选，逐个打开
fo -p                     # 内置预览（bat/chafa/viu/pdftotext）
fo -H -I                  # 含隐藏文件并忽略 .gitignore（仅 fd）
fo -n                     # dry-run：打印命令，不打开
fo -t                     # 打开器全部前台运行，阻塞至退出
fo -x                     # 打开器全部脱钩运行，不占终端
fo -- --multi --height 40%   # -- 之后的参数原样传给 fzf
```

- `-d DIR` 在 DIR 下搜索（默认 `.`）；与 `-l` 同给时显式 `-d` 优先
  （并告警）。
- `-l` / `--last` 复用上一次 `-d` 记录的目录（存于缓存）；尚无记录时
  硬报错。
- `-H` / `--hidden` 包含隐藏文件——find 回退时本来就包含，此选项无效。
  `-I` / `--no-ignore` 不遵循 `.gitignore`（仅 fd/fdfind；find 回退时
  告警并忽略）。
- `-p` / `--preview` 增加 fzf 预览窗格，按已安装工具拼装：文本 `bat`、
  图片 `chafa`/`viu`、PDF `pdftotext`，否则裸 `head`。
- `-n` / `--dry-run` 打印完整 pipeline 与每个文件的打开命令（前缀
  `would run:`）；fzf 照常启动供选择，但不打开文件，last_dir 与历史
  均不写入。
- `--` 之后的参数原样传给 fzf；与之冲突的 fzf 旗标（如它自己的
  `--preview`）优先于 `-p`。
- 只接受一个位置参数 `OPENER`，且可与选项任意混排——解析与顺序无关。

### 行为细节

- 候选来源按 `fd` → `fdfind` → `find` 回退，取首个可用者。
- 打开器分前台与脱钩两类，运行方式自动判定，优先级从高到低：映射表的
  `!`（前台）/ `&`（脱钩）前缀 → `-t` / `-x` 开关 → 内置 TUI 名单
  （nvim/vim/less/htop 等）→ 无显示服务器（SSH/纯 TTY）→
  默认脱钩（GUI 程序）。
- 前台打开器阻塞至退出，退出码非零仅告警；脱钩打开器经 `setsid -f`
  隔离，缺失时后台 + `disown`，全部 fd 重定向，
  打开器不随脚本退出而被杀。
- 上次目录与历史日志（`时间 \t 打开器 \t 文件`）存于
  `${XDG_CACHE_HOME:-~/.cache}/fo`；缓存写入尽力而为、绝不致命
  （只读 FS / 无 HOME 也可用，仅告警）。
- 扩展名只在最后一个 `.` 位于文件名主体时才被识别——`.gitignore` 这类
  点文件因此永远不匹配映射表。
- 取消 fzf（Esc / Ctrl-C）或未选中任何文件退出码 `0`；fzf 真实故障
  退出码 `1` 并在 stderr 报错。
- 真实打开时历史逐条追加；dry-run 不追加任何记录。

### 速查表

| 命令              | 作用                                    |
| ----------------- | --------------------------------------- |
| `fo`              | 当前目录选文件，默认打开器打开          |
| `fo kate`         | 选文件，固定用 kate 打开                |
| `fo -d ~/src`     | 在 ~/src 下搜索                         |
| `fo -l`           | 复用上次的搜索目录                      |
| `fo -m`           | 多选并逐个打开                          |
| `fo -p`           | 内置预览窗格（bat/chafa/viu/pdftotext） |
| `fo -H` / `fo -I` | 含隐藏文件 / 忽略 .gitignore（仅 fd）   |
| `fo -n`           | 打印将执行的命令，不实际打开            |
| `fo -- --multi`   | 额外旗标原样传给 fzf                    |
| `fo -h`           | 显示用法说明                            |

### FAQ

**为什么需要 bash >= 4？**

脚本用 `${var,,}` 做扩展名小写比较；macOS 自带的 bash 3.2 无法运行，
装 Homebrew 的 bash 或在 Linux 上使用。

**映射表的值支持带空格的参数吗？**

不支持——按空白拆分是这份单文件配置格式的刻意取舍；需要这类参数的
命令包一层 wrapper 脚本，或改用显式 `OPENER`。

**`.gitignore` 为什么匹配不到映射表？**

扩展名只在最后一个 `.` 位于文件名主体时才存在，点文件因此没有扩展名
可言。

**取消 fzf 为什么退出码是 0？**

放弃选择是正常结局而非错误——与 fzf 自身的退出约定一致；只有 fzf 真实
故障（如传了非法旗标）才报错退出。
