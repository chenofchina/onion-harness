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

### 接口（给 app 层）

```python
class MemoryStore:
    def remember(self, memory: Memory) -> str
    def recall(self, query: str, limit: int = 6) -> list[Memory]
    def forget(self, memory_id: str) -> bool
    def list_recent(self, limit: int = 50) -> list[Memory]
    def purge(self) -> None            # 用户一键清空
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
        "segments": ["core"],        # 引用哪些 prompt segment
        "overlay": "",               # 人格专属的额外指令
        "builtin": True,
    },
    {
        "id": "aggressive",
        "label": "Aggressive",
        "description": "直接动手，少问",
        "overlay": "You act decisively. Prefer doing over asking. Use tools freely.",
        "builtin": True,
    },
],
"active_persona": "default",
```

叠加顺序：`core segment` → `persona.overlay` → 模式契约（Plan/Strict）。

这样人格影响语气和激进度，但**不能**绕过 Plan 模式的只读限制或 Strict 的验证要求——安全约束永远在最外层。

### 热切换

复用现有的 `update_settings` 通道，加一个 `active_persona` 字段。切换后**下一次请求**即生效，无需重启。

同时加进元工具（AI 可提议切换人格，但同样需要你确认）：

```python
ChangeKind.SELECT_PERSONA = "select_persona"
```

### UI

侧栏模型选择器旁边加一个下拉，显示当前人格。切换即时生效。

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

`settings.json` 只存引用：

```json
{ "api_keys": { "deepseek": { "keyref": "onion:provider:deepseek" } } }
```

**迁移**：首次启动时检测到明文字符串格式的 `api_keys`，自动迁到凭据管理器并**覆写**原文件。这一步必须有测试。

### 数据

`cryptography` 的 AES-256-GCM，密钥由主密码经 PBKDF2-HMAC-SHA256（60 万轮）派生。

加密范围：记忆文本、会话事件流、任务 prompt。账本本身不加密（它只有配置快照，没有敏感数据），但会提示用户。

**这里有个必须你决定的权衡**：如果要求「没有主密码任何文件都读不了」，那应用每次启动都要你输密码。要么接受这个摩擦，要么接受磁盘上的数据可读。

---

## 六、实现顺序与工作量

| 阶段 | 内容 | 大致工作量 |
|---|---|---|
| ✅ 已完成 | UI 间距修正 | — |
| 1 | 密钥迁移到凭据管理器 + 加密存储 | 中 |
| 2 | Persona（复用 prompt segment） | 小 |
| 3 | Auto-Review（含 fail-closed 与 UI 报错） | 中 |
| 4 | RAG 存储 + 提取 + 注入 | 大 |
| 5 | RAG 向量检索（sqlite-vec） | 中 |

建议顺序：1 → 2 → 3 → 4 → 5。理由：密钥是最基础的（其他都依赖它），Persona 最小且能立刻见效，Auto-Review 独立于 RAG，RAG 最复杂放最后。

---

## 七、必须先定的四件事

这几个问题会显著改变实现，等你确认后我再动手。

**1. 主密码要不要？**
要求「没有主密码读不了任何文件」意味着每次启动输密码。接受这个摩擦，还是接受数据在磁盘上可读（靠 Windows 账户隔离保护）？

**2. RAG 提取用规则还是模型？**
规则版零成本、可测、但粗糙。模型版质量高，但每轮多一次调用。我建议先规则，用一段时间再决定。

**3. Auto-Review 用哪个审查模型？**
你指定了 `deepseek-flash`。但你机器上有 Kaspersky 拦截 HTTPS——虽然已用 `truststore` 解决，审查调用会走同一条链路。如果延迟明显，可能需要本地模型兜底。

**4. 「人格可以影响调用工具的激进程度」怎么实现？**
人格只能改提示词（软约束），模型可能不遵守。要真正的硬约束（比如激进人格自动跳过审批卡）就是在降低安全性——这个我不建议做。你希望人格只影响语气，还是确实影响行为阈值？

---

## 八、我不会做的事

写在这里，避免以后争执：

- **不会**让人格绕过 Plan/Strict 的安全约束。
- **不会**用审查替代沙箱，也不会因为「审查过了」就放宽沙箱限制。
- **不会**在密钥存储失败时静默回退到明文。
- **不会**让记忆伪装成模型的自然记忆（那会制造幻觉）。
- **不会**为了「记住一切」而无限增长上下文——检索有预算上限。