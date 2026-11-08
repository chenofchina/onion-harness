# Onion Harness V2.0 架构草案

> 本文是**设计文档**，不是已完成的功能。它给出三个模块的落点、接口和数据结构，供你先审阅方向，再分步实现。
>
> 阅读前请先看文末的「必须先定的四件事」——那里有几个决定会改变实现量级。

---

## 零、先说清楚：这份草案如何遵守既有约束

V2.0 的需求里有两处与现有架构冲突，我按现有约束做了调整，理由写在下面。

### 1. `ChangeLedger` 管不到源码

需求要求「所有代码更改必须通过 ChangeLedger 提案」。但账本的设计边界是**运行时元操作**：注册执行器、改提示词、调面板位置、挂载技能。它的校验器明确拒绝任意代码，UI 面板也只接受预定义枚举。

V2.0 的三个模块是**新功能**，属于我作为开发者的改动，直接改文件。真正的护栏是：307 个测试、架构守卫、以及你 review 每一次 commit。

账本继续负责它该管的事——用户在使用中通过 AI 调整 Onion 的**运行时配置**。两者不重叠。

### 2. 加密不能「绝对禁止明文」

「必须用 keyring，绝对禁止明文」在技术上有边界：keyring 需要一个非密钥的标识符来查找条目，所以在 `settings.json` 里存 `{"deepseek": {"keyref": "onion:deepseek"}}` 这样的**引用**是必要的，但存的不是密钥本身。

我会这样落地：

- 密钥**永不**写入 `settings.json`，只存引用名。
- 密钥本体在 Windows 凭据管理器（`win32cred`，已在你的环境验证可用）。
- 没有凭据管理器时（Linux 无 keyring 后端的容器）回退到主密码加密的文件，**不**回退到明文。

同理，SQLCipher 需要付费或自行编译，我改用 **AES-256-GCM 字段级加密**（`cryptography` 已就绪），密钥由主密码经 PBKDF2 派生。会话内容、记忆文本、向量都以密文落盘。

---

## 一、UI 间距修正（已完成）

**问题定位**：不是 margin 太大，是**空壳卡片在占位**。

`tool-result` 事件被折叠进对应的 `tool-call` 卡片后，`renderEvent` 返回 `null`，但外层 `<div className="card">` 仍然渲染。高度为 0，却照常消耗 flex 的 `gap: 14px`。两个相邻工具卡之间实际夹了 3 个空 div，视觉上就是 **28px 的断层**。

**改动**：

| 文件 | 变更 |
|---|---|
| `frontend/src/App.tsx` | 内容为空时整个 wrapper 不渲染；工具/变更类卡片加 `data-kind="tool"` 标记 |
| `frontend/src/styles.css` | 相邻工具卡间距 14px → 6px；工具块与后续文本之间略收紧 |

**实测**：连续三个工具卡之间的间距从 28px 降到 **6px**，DOM 卡片数从 9 降到 5。截图见对话记录。

---

## 二、记忆与检索（RAG）

### 落点

新建 `onion_harness/memory/` 作为**第二层**（与 `ledger` 同级），只依赖 `core`。

理由：它需要被 `harness` 使用，但不能反向依赖。放 `harness` 内部会让它无法被其他层复用；放 `api` 会越界。

需要扩架构守卫：`harness` 与 `api` 的允许集合加入 `memory`。

### 数据模型

```python
# memory/model.py —— 纯数据，无依赖
@dataclass
class Memory:
    id: str
    kind: str            # "preference" | "fact" | "event" | "code-habit"
    text: str            # 自然语言，加密存储
    weight: float        # 重要性 0..1，影响检索排序
    source_task: str
    created_at: float
    last_used_at: float  # 用于衰减
```

四类记忆对应需求里的「用户偏好、代码习惯、重要事件、核心事实」。

### 提取时机

不额外调模型——沿用现有压缩器的做法：

- 对话结束后，从事件流里提炼：用户显式说的偏好（「以后都用 PowerShell」）、反复出现的路径、报错模式。
- 走**规则 + 正则**，成本为零，可测试。
- 需要更强提炼时可以注入走模型的 summarizer，接口不变。

**为什么不调模型**：每轮对话多一次模型调用，成本翻倍，且不可预测。规则版先跑，够用再升级。

### 存储

```
~/.onionharness/memory.sqlite3
├── memories(id, kind, ciphertext, weight, source_task, created_at, last_used_at)
├── fts(memories_fts)          ← FTS5 全文索引（已验证可用）
└── vectors(memory_id, vector) ← sqlite-vec 扩展（已确认有 win_amd64 wheel）
```

**检索是混合的**：FTS5 关键词匹配 + 向量余弦相似度，加权合并。纯向量对中文短句不稳，纯关键词又抓不住同义改写。

没有 `sqlite-vec` 时（安装失败）自动降级为纯 FTS5，功能可用但召回差一些。这点会写进日志，不静默。

### 注入

沿用现有的**系统通道**——和压缩摘要同一个机制，所以不会被再次压缩丢掉：

```python
# harness/runtime.py
prompt = compose_system_prompt(state)
memories = self._memory.recall(task.prompt, limit=6)
if memories:
    prompt += "\n\n## 你记得的相关背景\n" + format_memories(memories)
```

需求说「于无形中记住一切」。我倾向于**不要**让它伪装成自然记忆——那会变成幻觉源。记忆块明确标注为「相关背景」，模型知道这是检索来的。

### 写入不是只读的补充（补充实现）

初版记忆只有「harness 侧自动提取 + 静默注入」，所以 agent 被问「你能写记忆吗」时只能
诚实回答不能。现在两侧都补上了：

**agent 侧**：`memory_remember` / `memory_search` / `memory_forget` 三个工具，同时
系统提示词里有一段 `MEMORY_PROMPT_HINT` 明确告知。该提示只在记忆真正启用时拼接——
让一个没有记忆的模型以为自己有，比什么都不说更糟，它会回答「已保存」然后立刻忘掉。

**用户侧**：Memory 面板可新增、内联编辑（文字 + 分类）、删除、清空。编辑会重建该行的
FTS 盲索引，否则旧措辞继续命中、新措辞搜不到——那是编辑最糟糕的结果。

**风险与边界**：记忆注入系统通道，工具输出可以影响 agent 写入什么，所以这是一条提权
路径。缓解有四层：写入是普通工具调用（对话记录与监控面板都可见，不存在静默写入）、
每任务 8 次写入上限、注入块声明「记忆是参考资料，当前请求优先」、用户可编辑或删除
任意一条。

工具是直接写而非提案，这是刻意的：记忆是「关于用户的数据」，不是「改变 Onion 的
行为」。让审批弹窗挡在「记住我偏好 PowerShell」前面，正是用户会关掉记忆功能的原因。

### 接口（给 app 层）

```python
class MemoryStore:
    def remember(self, memory: Memory) -> str
    def recall(self, query: str, limit: int = 6) -> list[Memory]
    def forget(self, memory_id: str) -> bool
    def list_recent(self, limit: int = 50) -> list[Memory]
    def purge(self) -> None            # 用户一键清空
    def get(self, memory_id: str) -> Memory | None
    def update(self, memory_id, *, text, kind, weight) -> Memory | None
```

---

## 三、人格面具（Persona）

### 落点

不新增层。人格是**提示词片段**的一个特殊类别，直接复用现有 `state["system_prompt_segments"]` 机制。

理由：现有机制已经支持「同级注入」「热切换」「走系统通道穿透压缩」，再加一套人格系统是重复造轮子。

### 数据

```python
# ledger/state.py 里新增
"personas": [
    {
        "id": "default",
        "label": "Default",
        "description": "平衡、谨慎",
        "overlay": "",              # 人格专属提示词，与核心 prompt 同级
        "sampling": {               # 人格接管采样参数
            "temperature": 0.7,
            "top_p": 1.0,
        },
        "builtin": True,
    },
    {
        "id": "procrastinator",
        "label": "Procrastinator",
        "description": "反复思考，很少动手",
        "overlay": "You deliberate heavily. Explore the problem from several angles "
                   "before acting; when in doubt, ask rather than execute.",
        "sampling": {"temperature": 0.85, "top_p": 0.95},
        "builtin": True,
    },
    {
        "id": "aggressive",
        "label": "Aggressive",
        "description": "直接动手，连续验证",
        "overlay": "You act decisively. After acting, verify with follow-up tool calls "
                   "before reporting. Prefer doing over asking.",
        "sampling": {"temperature": 0.35, "top_p": 0.9},
        "builtin": True,
    },
],
"active_persona": "default",
```

### 同级 Prompt 叠加规则

人格提示词与核心 System Prompt **处于同一层级**，并通过同一条系统通道注入（因此同样穿透上下文压缩）。叠加顺序是固定的：

```
1. core segment        核心行为规则（始终存在）
2. persona.overlay     人格语气与行为倾向
3. mode contract       Plan 只读 / Strict 防幻觉（始终在最后）
```

**顺序不可调换，也不允许人格覆盖前两项。** 实现上，`compose_system_prompt()` 增加一个 `persona` 参数，拼接在 core 之后、模式契约之前：

```python
def compose_system_prompt(state, persona=None):
    base = join_enabled_segments(state)          # core
    if persona and persona.overlay:
        base += "

## Persona: " + persona.label + "
" + persona.overlay
    return compose_mode_prompt(mode, base)       # mode contract 永远最后
```

`compose_mode_prompt` 已经存在且永远在最后拼接，所以 Plan 的只读限制和 Strict 的验证要求**不可能**被人格绕过——这是结构保证，不是约定。

### 采样参数热切换

人格接管 `temperature` 和 `top_p`。切换人格后，**下一次请求**即使用新参数，无需重启。

参数流向：

```
persona.sampling
    ↓  build_model_config() 覆盖
ModelConfig.temperature / .top_p
    ↓  SessionFactory.build_options()
engine options → provider.chat_completion(temperature=…, top_p=…)
    ↓  OpenAI 兼容层: payload.update(kwargs)
实际请求体
```

**需要新增的管道**（当前 `top_p` 完全没有通道）：

| 位置 | 现状 | 需要做 |
|---|---|---|
| `ModelConfig` | 有 `temperature`，无 `top_p` | 加 `top_p: Optional[float]` |
| `build_model_config` | 解析 `temperature` | 解析 `top_p`，clamp 到 0..1 |
| `SessionFactory.build_options` | 只传 `temperature` | 传 `extra_llm_kwargs={"top_p": …}` |
| `OnionOpenAIProvider` | 透传 kwargs | 无需改（已有 `payload.update(kwargs)`） |
| `AnthropicProvider` | 只处理 `temperature` | 加 `top_p` 到 payload |

**冲突规则**：人格采样参数优先级 **低于** 用户在同一会话中手动指定的值。也就是说，用户在模型选择器里显式调过温度，人格切换不覆盖它；只有用户没显式指定时才采用人格值。这避免了「换个人格就把我的设置改了」的意外。

**Anthropic 的特殊情况**：启用 extended thinking 时 API 不接受 `temperature` 和 `top_p`（会报错）。现有代码已经在 thinking 开启时跳过 temperature，`top_p` 需要同样处理。

### 行为倾向如何生效

人格通过提示词影响行为倾向——拖延症人格倾向于多思考少动手，极客人格倾向于连续调用工具验证。这是**软约束**：模型通常遵守，但不保证。

它**永远受限于** Plan 和 Standard 模式：

| 场景 | 结果 |
|---|---|
| 极客人格 + Plan 模式 | 依然是只读，写操作被拒绝 |
| 极客人格 + Standard 模式 | 依然逐条弹出审批卡 |
| 拖延症人格 + Standard | 少调用工具，但调用了照样审批 |

人格改变的是「想做什么」，模式约束决定「能做什么」。两者不在同一层，前者无法突破后者。

### 热切换

复用现有的 `update_settings` 通道，加 `active_persona` 字段。同时加进元工具（AI 可提议切换人格，同样需要你确认）：

```python
ChangeKind.SELECT_PERSONA = "select_persona"
```

### UI

侧栏模型选择器旁边加一个下拉，显示当前人格及其采样参数（如 `Aggressive · T0.35 / P0.9`）。切换即时生效。

---

## 四、自动审查（Auto-Review）

这是三个模块里**风险最高**的一个，我先说清它的两个失败模式。

### 失败模式

**漏判（假阴性）**：审查模型说 PASS，但代码实际有害。任何静态或 LLM 审查都会漏。**审查不能替代沙箱**，只能降低概率。

**误判（假阳性）**：审查模型过于保守，把正常操作也拒了，用户就会关掉这个功能——那等于零保护。

所以设计上有两条硬规则：

1. **审查永远不越过沙箱**。它是沙箱之前的一道额外闸门，不是替代品。
2. **审查模型失败时按「拒绝」处理**（fail-closed），但错误信息必须说明是审查服务不可用，而不是「代码危险」——否则用户会误以为自己的代码有问题。

### 落点

`harness/review.py`，由 `harness` 调用，位于「模型请求工具」和「沙箱执行」之间。

```
Tool call
   │
   ├─ 是否破坏性？──否──→ 直接沙箱执行
   │        │是
   │        ↓
   ├─ 审查模型判断 ──PASS──→ 沙箱执行
   │        │REJECT
   │        ↓
   └─ 中断 + UI 报错
```

### 破坏性判定

先用现有的 `looks_like_write()` 做**本地启发式预筛**——只有疑似写操作才调用审查模型。纯读取（`os.listdir`、`Get-ChildItem` 不带 `-Recurse`）直接放行，零成本。

这一步很关键：如果每条命令都过一次模型，成本和延迟都会让功能不可用。

### 审查提示词

审查模型只看**代码本身**，不看对话上下文（避免被诱导）：

```
你是代码安全审查器。判断下面的代码是否存在HIGH-RISK操作：
文件删除、磁盘格式化、注册表写入、权限修改、网络外传、
执行下载内容、修改系统目录、批量重命名。

只回一个词：PASS 或 REJECT。
不确定时回 REJECT。

代码：
<code>
```

用你指定的 `deepseek-flash`。实测它响应快、便宜，适合这种二分类。

### 配置

```python
"auto_review": {
    "enabled": True,
    "provider_id": "deepseek",
    "model": "deepseek-flash",
    "timeout_seconds": 12,
    "fail_closed": True,
}
```

### 与现有审批的关系

**Standard 模式的审批卡保留**，审查是它之前的一道自动闸门：

| 情况 | 行为 |
|---|---|
| 非破坏性 | 直接执行 |
| 破坏性 + 审查 PASS + Standard 模式 | 仍出审批卡 |
| 破坏性 + 审查 REJECT | 立即中断，不出审批卡 |
| 审查服务超时 | 按 fail_closed 拒绝，提示是审查不可用 |

两者不是替代关系——审查挡掉明显危险的，审批让你决定剩下的。

---

## 四点五、联网搜索（补充实现）

`plugins/web_search.py`，第 2 层（plugins 只依赖 core/sandbox），由 api 层注入
runtime，与 MCP 工具共用 `_extra_tools()` 这一条通道。

**为什么是本地服务**：search-neo 聚合百度与必应并在本机返回精简 JSON。云端搜索
API 的 Key 会随二进制分发而被逆向，本地服务没有这个问题，国内网络也不用翻墙。

**fail-closed 是这一模块的核心约束**。所有传输与协议失败（未启动、超时、5xx、
非 JSON、200 体内带 error）一律抛 `SearchError`，引擎会把工具结果标记为错误，
模型因此得知「搜索失败」并被要求不得凭记忆作答。返回空结果作为失败信号是最可靠
的幻觉来源——模型会拿记忆填补，并当作检索结果呈现。

**「成功但无结果」与「搜索失败」必须区分**：前者显式声明「什么都没找到，且这不
证明信息不存在」，后者是错误。两者混为一谈，用户就无法判断该重试还是该换问法。

**连接失败 ≠ 超时**：`requests` 的 `ConnectTimeout` 同时继承 `ConnectionError` 和
`Timeout`，只按名字判断会报「服务太慢」，而实际是没人在监听——用户会去修错误的
事情。代码先判断连接阶段。

**回环绕代理**：本机代理挂掉是常态，把 localhost 请求塞进死代理会让健康服务被误判
为不可用，所以回环地址显式 `proxies={"http": None, "https": None}`。

**关闭即无工具**：`enabled=False` 时 `build_web_search_tool` 返回 `None`，工具和
`SEARCH_PROMPT_HINT` 一起消失。告诉模型它有搜索能力而实际没有，比什么都不说更糟。

**不经审批与审查**：Auto-Review 的职责是在沙箱前拦代码执行；搜索是用户已开启的
只读网络调用，为每次查询弹确认框会让功能不可用。

---

## 五、密钥与数据加密

### 密钥

```python
# api/secrets.py
class SecretStore(ABC):
    def get(self, ref: str) -> str | None
    def set(self, ref: str, value: str) -> None
    def delete(self, ref: str) -> None

class WindowsCredentialStore(SecretStore):   # win32cred，已验证可用
class KeyringStore(SecretStore):             # keyring，需装包
class MasterPasswordStore(SecretStore):      # 主密码 + AES-GCM 文件，最后回退
```

**已落地**（不再只是设计）：连「引用名」都不需要单独存。`settings.json` 里只有一个
`key_providers` 名单，列出哪些厂商配置过 key：

```json
{ "key_providers": ["deepseek"] }
```

读 key 时才去凭据库取（`CRED_PREFIX + provider_id`），所以凭据管理器里删掉条目后，
界面立刻显示未配置，不存在「文件说配了、其实没有」的漂移。`AppStore.save_settings`
会主动剥掉任何 `api_keys` 字段，即便调用方误塞进来也写不到磁盘。

**迁移已实现并有测试**：`AppStore.migrate_plaintext_keys` 在服务构造时运行，把旧版
明文 `api_keys` 逐条写入凭据库，**先写成功再改写文件**，中途崩溃只会重复而不会丢
key。已有测试覆盖：明文不出现在文件里、迁移幂等、`public_settings` 只报告真的能取到
的 key。

**没有凭据管理器时**：不回退到明文。`_open_memory` 会直接停用长期记忆并在界面说明
原因；API key 退化为仅本次会话内存有效，重启即失效。宁可少一个功能，也不落明文。

### 数据

`cryptography` 的 AES-256-GCM，密钥由凭据库里的 32 字节随机主密钥经
PBKDF2-HMAC-SHA256（20 万轮）派生。每条记录用独立随机 nonce，nonce 前置在密文里，
不单独建列。

加密范围（已实现）：**记忆文本**。

**盲索引**：FTS5 要能比较词元，通常就得把词明文写进索引表——那样 `偏好` 会直接出现在
`.sqlite3` 文件里。改成用独立的 HMAC 子密钥对每个词元取摘要后入索引，查询时同样取摘要。
命中语义不变（词元精确匹配，bm25 排序照常），文件里则只剩十六进制摘要。测试
`test_no_plaintext_anywhere_in_the_file` 逐词扫描原始字节验证这一点。

**已加密（补充实现）**：会话事件流 `tasks/*.jsonl` 逐行 sealed（每行一个
`onion:v1:` + base64 AES-GCM），任务索引 `tasks.json` 整文件 sealed。两者都能兼容
读取旧版明文并在启动时自动迁移（`migrate_plaintext_events` 已对本机 41 个真实转录
与 41 条索引执行过，迁移后仍可正常读回）。

`settings.json` 保持明文 JSON：其中只有 provider id、窗口大小和开关，没有任何对话
内容。这样出问题时仍然可以直接打开检查，不需要先解密。

账本不加密（只有配置快照）。

**这里有个必须你决定的权衡**：如果要求「没有主密码任何文件都读不了」，那应用每次启动都要你输密码。要么接受这个摩擦，要么接受磁盘上的数据可读。

---

## 六、实现顺序与工作量

| 阶段 | 内容 | 状态 |
|---|---|---|
| ✅ | UI 间距修正 | 已完成 |
| ✅ | 密钥迁移到凭据管理器 + AES-256-GCM 加密 | 已完成 |
| ✅ | RAG 存储 + 规则提取 + 静默注入 | 已完成 |
| ✅ | Persona（prompt 叠加 + Temperature/Top_P 热切换） | 已完成 |
| ✅ | Auto-Review（启发式预筛 + fail-closed + UI 报错） | 已完成 |
| ✅ | 会话事件流与任务索引加密（复用 DataCipher） | 已完成 |
| 1 | RAG 向量检索（sqlite-vec） | 待做 |

剩下的唯一一项是向量检索。当前是纯 FTS5（盲索引），中文召回已经可用；加向量是为了
抓住同义改写，属于质量提升而非缺失功能。

---

## 七、已确认的决定

**1. 主密码：不需要。**
使用 Windows 凭据管理器（`win32cred`）。`settings.json` 只存引用名，密钥本体在系统凭据库。靠 Windows 账户隔离防止其他用户读取。无凭据管理器时的回退仍需设计（见「未决事项」）。

**2. RAG 提取：先规则，后模型。**
规则版（正则 + 启发式）先落地，零成本、可测。接口预留 `summarizer` 注入点，需要时换走模型的版本。

**3. Auto-Review：`deepseek-flash` + 本地启发式预筛。**
只有疑似写操作才调用审查模型，纯读取直接放行。审查失败按 fail-closed 处理，错误信息明确区分「审查不可用」与「代码危险」。

**4. 人格：同级 Prompt + 接管采样参数 + 软行为倾向。**
不越过任何安全约束。详见第三节。

---

## 八、未决事项

**无凭据管理器时的密钥回退。**
Linux 容器里可能没有 keyring 后端。届时要么要求安装 `keyring` 后端，要么回退到主密码加密文件。这个分支还没定，等真正跨平台时再决定。当前 Windows 目标下不阻塞。

**`sqlite-vec` 安装失败时的召回质量。**
降级到纯 FTS5 是可用但明显变差。需要在实现时实测降级前后差异，再决定是否值得把向量检索设为必需依赖。

---

## 九、我不会做的事

写在这里，避免以后争执：

- **不会**让人格绕过 Plan/Strict 的安全约束。
- **不会**用审查替代沙箱，也不会因为「审查过了」就放宽沙箱限制。
- **不会**在密钥存储失败时静默回退到明文。
- **不会**让记忆伪装成模型的自然记忆（那会制造幻觉）。
- **不会**为了「记住一切」而无限增长上下文——检索有预算上限。