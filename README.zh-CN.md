# opstools

一个由单文件运维工具组成的集合。每个工具都是一份可独立执行的脚本——
Python、shell 或其他合适的语言——彼此不共享代码：无安装器、无后台服务、
无框架。拷到机器上，读完源码即可放心使用。后续会陆续加入更多工具。

English: [README.md](README.md)

## 工具

| 工具 | 语言 | 说明 |
| --- | --- | --- |
| [keydist](#keydist) | Python 3.11+ | 把一份集中清单里的 secrets 增量分发到各消费端的 `.env` 文件，内容变化时执行 post 命令 |
| [dly](#dly) | Python 3.8+ | 打开、查看、列出每日笔记与任务笔记；简短日期表达式与可组合偏移，编辑器与查看命令可配置 |

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
```

---

## keydist

`keydist` 按下述规则定位清单，解析其中引用的 secrets，按各消费端能理解的
格式**增量**更新其 `.env` 文件；文件内容实际变化时执行可选的 `post` 命令
（例如重启服务）。

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

# TOML 内联表不可跨行；多元素回退链请用子表写法：
[secrets.tls_cert]
chain = [                        # 回退链：逐个尝试，首个成功者生效
  { env = "TLS_CERT" },
  { file = "~/.local/etc/tls/cert.pem" },
]

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
```

### secret 形式

| 形式 | 含义 |
| --- | --- |
| 裸字符串/数字/布尔 | 字面量（布尔渲染为 `true` / `false`） |
| `{ env = "NAME" }` | 进程环境变量 `NAME` |
| `{ envfile = "PATH", key = "KEY" }` | dotenv 文件中 `KEY` 的值；文件恰含一对键值时可省略 `key` |
| `{ file = "PATH" }` | 整文件内容，去末尾换行（适合私钥） |
| `{ chain = [e1, e2, …] }` | 逐元素尝试，首个成功者生效；元素可为字面量或引用（允许嵌套 `chain`） |

引用表必须恰含 `env` / `envfile` / `file` / `chain` 之一，未知键或冲突键
一律硬报错。裸字符串即字面量、由类型区分，因此不存在可拼错的前缀——
写错的引用（如 `{ en = "X" }`）会响亮失败，而不是把字面量悄悄写进消费端。

### 输出 style

每个目标按四种 style 之一渲染：可显式指定 `style = "..."`，也可由目标的
名称/路径/post 命令自动推断：

| Style | 引号处理 | 典型消费端 |
| --- | --- | --- |
| `shell`（默认） | POSIX 单引号 | 任何可 `source` 的场景；最大兼容超集 |
| `dotenv` | 双引号，`\n` 展开为换行 | node/python/Go/Ruby dotenv 解析器 |
| `systemd` | 双引号，无 `export` | systemd `EnvironmentFile=` |
| `docker` | 裸值，无任何转义 | `docker run --env-file` |

写入前执行与 style 相关的校验：`systemd` 与 `docker` 拒绝多行值；`docker`
会就 compose 与 `docker run` 的引号语义分歧给出警告。

### 行为细节

- 仅在内容变化时重写文件；未变化的文件保持 mtime 并跳过 `post`。
- `--dry-run` / `-n` 打印每个待发生变更的 unified diff 及将要执行的
  `post` 命令——不写文件、不改权限、不执行任何命令。
- 目标文件始终确保 `0600`；新建文件从一开始就是 `0600`。
- `post` 经 `shlex` 切分执行，超时 30 秒；非零退出或超时视为该目标失败。
- 自动清理历史运行遗留的重复键（附带警告）。
- 启动时检查清单权限：组/其他用户可读会告警（其中可能含 secret 引用）。
- 输出在终端上着色；管道输出或设置了 `NO_COLOR` 时自动禁用。
- 全部目标成功退出码 `0`；否则退出码 `1`，stderr 打印失败汇总。

### 用法

```bash
keydist                 # 按清单执行分发
keydist --dry-run       # 显示待发生变更的 unified diff，不做任何改动
keydist -c ~/alt.toml   # 显式指定清单
```

### 快速开始

编写 `~/.config/keydist/keydist.toml`（建议 `chmod 600`），然后运行
`keydist`（安装见[安装](#安装)）。

### 速查表

| 命令 | 作用 |
| --- | --- |
| `keydist` | 按查找顺序定位清单并执行分发 |
| `keydist -c PATH` | 使用显式清单 |
| `keydist --dry-run` | 预览 diff 与 post 命令，不做任何改动 |
| `keydist -n -c PATH` | 同上，使用显式清单 |

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
  子 shell（chezmoi cd 风格）；`dly cd -v` 仅输出路径。

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

| 类型 | 文件名 | 示例 |
| --- | --- | --- |
| 普通日期笔记 | `YYYYMMDD.md` | `20261105.md` |
| 日期任务笔记 | `YYYYMMDD.task.md` | `20261105.task.md` |
| 全局普通笔记 | `global.md` | — |
| 全局任务笔记 | `global.task.md` | — |

### 日期表达式

| 表达式 | 含义 | 示例（今天为 2026-11-05） |
| --- | --- | --- |
| （空） | 今天 | → `20261105.md` |
| `DD` / `MMDD` / `YYMMDD` | 本月某日 / 今年月日 / 20YY 年月日 | `110312` → 2011-03-12 |
| `YYYYMMDD` / `YYYY-MM-DD` / `YYYY/MM/DD` / `YYYY.MM.DD` | 完整日期，年份不限 | `19991231` |

### 日期偏移

偏移是叠加在基准日期上的辅助表达式：首个位置参数可为基准日期表达式，
其后只能是偏移参数；不写基准时以今天为基准——单个偏移因此依旧可用
（`dly +1m`）。偏移从左到右逐个在前一结果上应用，可混用、可重复：

| 偏移 | 含义 |
| --- | --- |
| `+N` / `-N[d]` | ±N 天 |
| `+Nw` / `-Nw` | ±N 周 |
| `+Nm` / `-Nm` | ±N 个日历月 |
| `+Ny` / `-Ny` | ±N 个日历年 |

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
dly task                       # 编辑今天任务笔记
dly t -v +1w                   # 查看下周任务笔记
dly 1101 +3m -2d               # 编辑 11 月 1 日 +3 个月 -2 天（从左到右）
dly +3m -2d                    # 以今天为基准组合偏移
dly global task                # 编辑 global.task.md（dly task global 亦可）
dly -l                         # 列出普通笔记文件名（字典序逆序）
dly -l -a 2026                 # 全部类型中筛选文件名含 2026 的文件
dly task -l '*01.task.md'      # 任务类型，按 glob 模式筛选
dly cd                         # 在 NOTES_DIR 中启动子 shell
dly cd t -v                    # 不进入，仅输出 TASK_DIR 路径（共用时同 NOTES_DIR）
```

- `-v` / `--view` 只读查看：默认用标准库 `pydoc.pager` 分页，配置
  `VIEWER_CMD` 后改用外部命令；文件不存在报错退出。
- `-l` / `--list` 每行输出一个文件名，按字典序逆序；pattern 不含
  `* ? [` 时按 `*pattern*` 子串匹配，否则按 glob 匹配；无匹配退出码 1；
  `+1m` 这类日期记号不在此解析，只是文件名 pattern。
- `cd [task|t]` 在目录中启动交互式子 shell（`$SHELL`，Windows 取
  `%COMSPEC%`），`exit` 退出后回到原目录，子 shell 退出码原样透传；
  加 `-v` 时仅输出目录路径、不进入。
- 编辑器与查看命令的退出码原样作为脚本退出码。

### 速查表

| 命令 | 作用 |
| --- | --- |
| `dly` | 编辑今天的普通笔记（`YYYYMMDD.md`） |
| `dly 1105` / `dly +1m` | 编辑对应日期的普通笔记 |
| `dly 1101 +3m -2d` | 在基准日期上组合偏移（从左到右） |
| `dly task` / `dly t` | 编辑今天的任务笔记 |
| `dly g t` | 编辑 `global.task.md`（`task global` 亦可） |
| `dly -v …` | 以上任意形式，改为只读查看 |
| `dly -l [-a] [pattern]` | 列出普通 / 全部类型 |
| `dly task -l [pattern]` | 列出任务类型 |
| `dly cd [task\|t]` | 在普通 / 任务目录中启动子 shell |
| `dly task cd` / `dly t cd` | 同上，task 关键字在前 |
| `dly cd t -v` | 仅输出目录路径，不启动子 shell |

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

不会——子进程无法修改父 shell 的工作目录。与 `chezmoi cd` 相同，
`dly cd` 在目标目录中启动一个交互式子 shell：在其中工作即身处该目录，
`exit` 退出后回到原目录。只需路径时用 `dly cd -v`（如 `cd $(dly cd -v)`）。

**Windows 下能用吗？**

可以——纯标准库。把 `EDITOR_CMD` 指向任意编辑器，`VIEWER_CMD` 可设为
如 `["more.com"]`；各平台示例见脚本注释。
