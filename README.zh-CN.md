# ops-toolkit

一个由小型、自包含运维工具组成的工具集。每个工具都是仅依赖 Python 3.11+
标准库的单文件可执行脚本——无需安装依赖、没有后台服务。拷到机器上，
读完源码即可放心使用。

| 工具 | 说明 |
| --- | --- |
| [keydist](#keydist) | 把一份集中清单里的 secrets 增量分发到各消费端的 `.env` 文件，内容变化时执行 post 命令 |

English: [README.md](README.md)

## 设计原则

- **零依赖** — 仅用标准库（`tomllib` 要求 Python 3.11+）。
- **Fail closed** — 配置畸形或有歧义一律硬报错，绝不静默降级成不安全行为。
- **幂等** — 仅在文件内容实际变化时重写，下游钩子每次变化恰好触发一次。
- **本地优先** — 清单放在 `~/.local/etc/`，可执行文件放在 `~/.local/bin/`，不联网。

---

## keydist

`keydist` 按下述规则定位清单，解析其中引用的 secrets，按各消费端能理解的
格式**增量**更新其 `.env` 文件；文件内容实际变化时执行可选的 `post` 命令
（例如重启服务）。

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
post = "systemctl --user restart myapp"    # 可选；仅在文件实际变化时执行

[targets."myapp".vars]                     # 消费端键名 = [secrets] 中的 secret 名
API_KEY     = "api_key"
DB_PASSWORD = "db_pass"
DOMAIN      = "domain"
TLS_CERT    = "tls_cert"
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
- 全部目标成功退出码 `0`；否则退出码 `1`，stderr 打印失败汇总。

### 用法

```bash
keydist                 # 按清单执行分发
keydist --dry-run       # 显示待发生变更的 unified diff，不做任何改动
keydist -c ~/alt.toml   # 显式指定清单
```

### 安装

```bash
install -Dm755 keydist ~/.local/bin/keydist
```

随后编写 `~/.config/keydist/keydist.toml`（建议 `chmod 600`）并运行 `keydist`。
