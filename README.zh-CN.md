# opstools

一个由单文件运维工具组成的集合。每个工具都是一份可独立执行的脚本——
Python、shell 或其他合适的语言——彼此不共享代码：无安装器、无后台服务、
无框架。拷到机器上，读完源码即可放心使用。后续会陆续加入更多工具。

English: [README.md](README.md)

## 工具

| 工具 | 语言 | 说明 |
| --- | --- | --- |
| [keydist](#keydist) | Python 3.11+ | 把一份集中清单里的 secrets 增量分发到各消费端的 `.env` 文件，内容变化时执行 post 命令 |

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
install -m755 keydist ~/.local/bin/keydist
```

---

## keydist

`keydist` 按下述规则定位清单，解析其中引用的 secrets，按各消费端能理解的
格式**增量**更新其 `.env` 文件；文件内容实际变化时执行可选的 `post` 命令
（例如重启服务）。

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
