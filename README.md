# 企业智能助手

基于 FastAPI 和 DeepSeek 的聊天服务，支持文件上传问答、MySQL 会话记忆、多轮上下文、Tavily 联网搜索和 API 文档。

完整的项目结构、逐文件代码职责、技术栈、数据库与接口说明见 [开发文档](DEVELOPMENT.md)。

## 启动

在项目根目录执行（Windows PowerShell）：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```dotenv
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
TAVILY_API_KEY=你的Tavily密钥
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=agent_user
MYSQL_PASSWORD=你的数据库密码
MYSQL_DATABASE=agent_db
```

启动服务：

```powershell
.\.venv\Scripts\python.exe main.py
```

- 聊天页面：http://127.0.0.1:8000
- 接口文档：http://127.0.0.1:8000/docs
- 存活检查：`GET /health`（不会调用模型）
- 开发时自动重载：`.\.venv\Scripts\python.exe -m uvicorn main:app --reload`

## 聊天接口

网页使用 `POST /api/chat/stream` 逐段接收模型生成的回答，完整结束后显示 Markdown 和来源卡片。
`POST /api/chat` 继续提供完整 JSON 响应，便于 Swagger 手动测试。

`POST /api/chat`，请求示例：

```json
{
  "message": "请把上面的回答总结为三点",
  "history": [
    {"role": "user", "content": "介绍一下知识库的作用"},
    {"role": "assistant", "content": "知识库用于集中管理和检索资料。"}
  ]
}
```

首次提问可省略 `history` 和 `conversation_id`。响应包含 `conversation_id`、`reply`、`model`、可选的 `usage` 和搜索来源 `sources`。
后续携带返回的 `conversation_id`，由服务端自动读取数据库历史，此时请省略 `history`。
首次请求的 `history` 可用于导入已有对话，随本轮问答一起保存。
`web_search` 省略或为 `null` 时，Agent 根据问题与历史自主决定是否调用搜索工具，并生成搜索词。兼容 `true`（要求先搜索）与 `false`（禁止搜索）；网页不传此字段，也没有搜索按钮。
历史消息只允许 `user`、`assistant` 角色，系统提示词由服务端设置。
每条输入最多 8000 字符，历史最多 40 条，总输入最多 60000 字符。
服务端将对话原文保存在 MySQL，默认读取最近 20 条用户及助手消息作为记忆。
API 校验限制总历史及当前问题最多 60000 字符；实际模型输入由 `service/context.py` 统一裁剪，普通问答、表格统计与精确计算使用 16000 字符预算，包含系统规则、资料、当前问题和历史。联网搜索、网页抓取（包含失败后继续处理的情况）及文件摘要综合回答使用 24000 字符预算，不影响数据库中的完整原文。请求准备和工具调用后的预算由同一函数管理。
长网页会在工具内部进行「完整保留正文 → 分段归纳 → 逐级合并」，再向聊天模型返回不超过 5000 个 JSON 字符的阅读结果，避免仅保留开头。每个网页最多处理 240000 字符正文、64 个批次，每批不超过 8000 个 JSON 字符；超过正文范围时明确标记部分读取。`summarized` 标明摘要，`coverage` 记录提取字符数、处理字符数及批次数。完整来源 URL 与工具调用配对始终保留，`truncated` 标明展示裁剪，`links_truncated` 标明链接减少。分段及合并需要额外模型请求，其用量计入聊天响应；每 15 秒报告处理进度，停止接收会取消当前摘要请求。
前端展示最近 20 段会话，并为当前会话加载最近 80 条消息。

错误状态码：422 参数错误，429 模型限流或额度限制，502 上游调用失败，
503 配置缺失或数据库不可用，504 模型响应超时，500 未预期的服务端错误，
404 会话不存在，409 同一会话被并发更新。
业务和模型错误统一返回 `{"detail": "错误提示"}`，不会返回密钥或上游原始报文。
参数校验错误仍使用 FastAPI 默认的 422 响应，`detail` 为字段错误列表。
Swagger 中已列出聊天接口的错误状态码及响应结构。

### 流式聊天

`POST /api/chat/stream` 使用与 `/api/chat` 相同的请求体，返回 `text/event-stream`。
前端使用 `fetch` 读取 SSE，支持中文跨数据块解码和逐段追加显示。

- `tool_start`：Agent 准备调用工具，`name` 区分 `web_search`、`search_documents`、`summarize_document`、`semantic_search`、`analyze_spreadsheet` 和 `calculate`；`query` 展示查询范围、文件名或表达式；`reset: true` 清除中间说明，等待最终回答。
- `tool_progress`：摘要工具的章节定位、分段读取 / 归纳、合并进度，包含 `stage`、`message`，分段处理时还有 `completed`、`total`。
- `sources`：本轮网页来源 `sources`、是否已执行联网搜索 `searched`、文档引用 `document_sources` 及已完成的摘要 `document_summaries`；首次事件来源为空，工具执行后更新。
- `delta`：正文片段，数据格式为 `{"text": "回答片段"}`。
- `done`：模型正常结束且数据库保存成功后，返回完整的 `ChatResponse`。
- `error`：流开始后的错误，数据包含统一管理的 `status` 和 `detail`。

输出开始前的校验、会话及文件范围检查错误仍通过 HTTP 状态码返回；
开始输出后 HTTP 状态已确定，错误通过 `error` 事件返回，前端会显示提示。
服务端仅在模型正常结束后保存完整问答；没有正常结束标记的半段回答不写入记忆。
停止接收或连接中断时，页面保留已显示的文字，输入框保持清空；部分回答不会写入浏览器会话历史。
若中断发生在数据库提交附近，保存状态需重新加载会话确认。
浏览器在连续 180 秒没有收到事件时中断请求；每个进度或正文事件都会重置计时。摘要内部模型请求最多等待 180 秒，每 15 秒报告仍在处理的进度，长文档可持续处理多个批次。

## MySQL 会话记忆

服务读取现有 `MYSQL_*` 配置，也兼容 `DB_HOST`、`DB_PORT`、`DB_USER`、`DB_PASSWORD`、`DB_NAME`；
同时存在时优先使用 `MYSQL_*`。数据库需已创建，账号需有会话表的读写权限。

会话和文档的五张表统一由根目录 `agent_db.sql` 管理。首次部署或需要补建缺失表时，在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m storage.mysql --init
```

初始化读取 `agent_db.sql`，全部建表语句使用 `CREATE TABLE IF NOT EXISTS`，可重复执行，保留现有表结构和数据。
脚本包含 `conversations`、`messages`、`documents`、`document_chunks` 和 `conversation_documents`，不包含删表语句。
数据库本身需提前创建；已有表的结构变更需要单独迁移。

在 Swagger 中先发送：

```json
{
  "message": "我叫小林，后续请用简洁的中文回答。"
}
```

复制响应中的 `conversation_id`，下一次发送：

```json
{
  "conversation_id": "替换为上一步返回的会话UUID",
  "message": "我叫什么名字？"
}
```

服务会读取该会话的历史、调用模型，再将本轮用户问题和助手回答在一个事务中保存。
创建新会话也包含在此事务中，模型或搜索失败时不写入半轮消息或空会话。
如果数据库提交失败，则返回错误；同一会话在生成期间被其他请求修改时返回 409，重新加载后再提问。
不同会话按 ID 隔离；重启服务后，数据库中的历史仍可继续使用。
这是一种基于最近历史的会话记忆，较早内容不会自动摘要或检索。

会话管理接口：

- `POST /api/conversations`：传入 `{"title": "新对话"}` 创建空会话，返回 `conversation_id`。
- `GET /api/conversations?limit=20`：查询最近会话，最多 100 段。
- `GET /api/conversations/{conversation_id}?limit=80`：按正序读取最近消息，最多 100 条。
- `DELETE /api/conversations/{conversation_id}`：在同一事务中删除该会话及其消息。

数据库保存角色和消息原文；模型、Token 用量及搜索来源卡片的额外信息目前只在浏览器缓存。
MySQL 同步操作在线程中执行，聊天生成期间不占用数据库连接。

## 联网搜索

在 `.env` 中添加 `TAVILY_API_KEY` 后重启服务。通过 Tavily 的官方 REST 接口调用搜索，
密钥仅在服务端使用，无需额外安装搜索 SDK。

先在 Swagger 中调用 `POST /api/search` 单独验证搜索（不调用 DeepSeek）：

```json
{
  "query": "Python 官方最新稳定版本",
  "max_results": 5
}
```

响应中的 `results` 包含每条网页的 `title`、`url` 和 `content` 摘要。
搜索词最多 400 字符，`max_results` 范围为 1–10，每条摘要最多保留 3000 字符。
没有匹配结果时返回空列表。

需要模型结合搜索结果回答时，调用 `POST /api/chat`：

```json
{
  "message": "Python 官方最新稳定版本是什么？请附上来源链接。",
  "history": []
}
```

服务将 `web_search` 的工具定义传给 DeepSeek，使用 `tool_choice: "auto"`。模型可直接回答，也可返回 `tool_calls`；后端校验参数、执行 Tavily，再以 `role: "tool"` 回传结果，继续生成答案。每轮问答最多 2 轮工具调用、合计 3 次调用，每次最多 5 条结果；相同查询会复用本轮结果。
响应中的 `sources` 返回本次检索的参考来源；它们不代表模型逐一引用或独立核实了全部网页。
模型可以结合保留的会话历史生成追问的搜索词。最新动态、实时数据、当前版本和用户明确要求搜索的问题应调用工具；概念解释、写作或已有文件资料足够时可以直接回答。
`searched` 表示本轮是否实际成功执行过搜索，空结果也为 `true`。未调用工具时 `sources` 为空，不要求 Tavily 密钥；自主搜索仍需服务端配置 `TAVILY_API_KEY`。

搜索错误同样统一管理：503 密钥缺失，502 认证、连接或结果异常，
429 限流或额度限制，504 超时。搜索失败时直接返回错误，不伪装成已搜索成功的回答。

## 项目结构

- `main.py`：应用入口、页面和存活检查。
- `schemas/chat.py`：聊天请求、响应模型和输入校验。
- `router/chat.py`：聊天路由。
- `router/conversations.py`：会话创建、列表、历史及删除接口。
- `router/search.py`：独立搜索路由。
- `router/files.py`：上传文件及查询解析结果。
- `schemas/files.py`：文件信息和引用片段结构。
- `schemas/search.py`：搜索请求和结果结构。
- `schemas/conversation.py`：会话及已保存消息结构。
- `core/exceptions.py`：业务异常定义。
- `core/exception_handlers.py`：异常到状态码、提示文案的映射及全局处理器注册。
- `schemas/error.py`：业务和模型错误响应结构。
- `service/chat.py`：组装上下文，协调 Agent 执行和最终问答保存。
- `agent/runner.py`：模型自主调用网页和文档工具，校验参数、限制次数并收集来源。
- `service/context.py`：统一控制模型输入的字符预算及参考资料。
- `service/files.py`：提取 TXT、MD、PDF、DOCX 文字并分块。
- `storage/documents.py`：文件元数据、分块和会话文件查询。
- `tools/documents.py`：文档工具定义、可用文件清单和关键词 / 中文双字匹配的片段检索。
- `tools/document_summary.py`：完整扫描已提取文字，定位相关章节、分段归纳，再逐级合并摘要和覆盖统计。
- `service/memory.py`：异步读取记忆、裁剪上下文、提交本轮问答。
- `storage/mysql.py`：MySQL 连接、事务、历史查询及并发冲突检测。
- `agent_db.sql`：统一管理会话和文档表的初始化脚本，保留已有数据。
- `tools/web_search.py`：调用 Tavily，处理网络异常并提取网页标题、链接及摘要。
- `tools/scrape_webpage.py`：校验 URL、读取 HTML、提取正文并报告网页阅读进度。
- `tools/webpage_summary.py`：长网页分段归纳、逐级合并、有界结果与覆盖范围。
- `agent/llm.py`：普通及流式异步模型调用，默认 60 秒超时、最多一次重试；支持摘要的 JSON 模式及独立输出、超时配置。
- `agent/prompts.py`：现有的企业 Agent 系统提示词。
- `static/index.html`：聊天页面结构。
- `static/styles.css`：页面样式和移动端适配。
- `static/app.js`：聊天请求、会话管理、Markdown 及来源展示。
- `skills/`：待扩展的技能模块。

当前版本实现聊天、联网搜索、上传文件检索和 MySQL 会话记忆；其他企业知识库及执行工具尚未接入。
默认仅监听本机，尚未提供用户认证。

## 前端页面

启动 FastAPI 后访问 http://127.0.0.1:8000 即可使用，无需安装 Node.js 或运行前端构建命令。

- 输入消息后点击发送或按 Enter，输入框在提交时立即清空；Shift + Enter 换行。
- 点击输入框的 `+` 选择文件，或复制 TXT、MD、PDF、DOCX、XLSX、XLS、CSV 文件后在输入框按 Ctrl + V 粘贴。文件会自动上传，普通文字仍可正常粘贴；每个文件最多 20 MB。
- 首次询问文件时，上传完成后输入问题并发送；本条请求携带文件 ID，问题气泡显示附件卡片，Agent 的当前问题上下文也明确标记该附件。上传未完成或有失败附件时不能发送，需等待或移除失败附件。
- 发送成功后，输入区的待发送卡片清空，文件卡片保留在它随附的那条问题中；文件绑定会话供后续追问。回答下方的「文件参考片段」可展开查看来源内容。
- 直接发送问题，Agent 自主判断是否搜索；搜索时显示查询状态，完成后展示参考来源。
- 提问「总结全文」或「总结员工部分」时，Agent 自主调用摘要工具，页面显示分段与合并进度；完成后显示扫描片段数、位置范围和解析提示。「摘要引用片段」是证据样例，数量不代表实际扫描数量。
- 回答生成时逐段展示正文，结束后统一展示 Markdown 和来源；点击停止按钮可中断接收并保留已显示内容。
- 服务端在发现流连接断开时关闭模型流；已开始的搜索或数据库提交可能仍会完成。
- 左侧可新建、切换和删除会话；页面加载最近 20 段数据库会话，每段展示最近 80 条消息，完整原文仍保留在数据库。
- 首次提问后自动保存服务端会话 ID，后续只传 ID 和问题，不重复传入历史。旧版仅保存在浏览器的对话可在下一次提问时一并导入。
- 删除会话会同时删除数据库中的该会话和消息，操作前页面会要求确认。
- 支持标题、列表、表格、代码块、链接等基础 Markdown 展示，以及复制回答和代码。
- 网络或模型错误会显示在输入框上方，输入框保持清空；点击「重试」可重新提交最近一次问题。
- 手机端通过左上角按钮打开会话导航；Ctrl / Cmd + K 可开启新对话。

前端访问同源的 `/api/files`、`/api/chat/stream`、`/api/conversations` 和 `/health`，API 密钥及数据库凭据仍仅由服务端读取。
右上角状态仅表示应用服务是否可连接，不代表外部模型或搜索服务已验证可用。

## 使用 Swagger 测试

启动服务后打开 http://127.0.0.1:8000/docs。

展开 `POST /api/chat`，点击 `Try it out`，输入以下请求体后点击 `Execute`：

```json
{
  "message": "你好，请介绍一下你自己",
  "history": []
}
```

查看响应中的 `conversation_id`、`reply`、`model` 和 `usage`。测试多轮记忆时，将返回的 `conversation_id` 带到下一次请求，无需传历史。
聊天接口会调用真实模型，需要有效的 DeepSeek 配置和网络连接，并可能产生模型费用。
联网搜索还会使用 Tavily 账户额度；可先用 `/api/search` 验证搜索，再直接向 Agent 提问最新信息。
`GET /health` 可用于检查服务是否启动，不调用模型。

## 文件上传与问答

先安装 `requirements.txt`，再执行 `.\.venv\Scripts\python.exe -m storage.mysql --init` 补齐文档表。
上传依赖为 `python-multipart`、`pypdf` 和 `python-docx>=1.1.0`，无需额外的文件识别 API 密钥。

支持 TXT、Markdown、带文字层的 PDF、DOCX 段落及表格，以及 XLSX、XLS、CSV。PDF 保留页码，DOCX 的位置标为「正文」，不生成不可靠的页码。Excel/CSV 使用 pandas、openpyxl 和 xlrd 解析。
扫描 PDF 或图片没有可提取文字时会提示需要 OCR；当前不支持图片识别、OCR、旧版 DOC 和密码保护文件。
混合型 PDF 的无文字页面会出现在上传结果的 `warnings` 中，可用页面仍继续解析。

限制：单文件最多 20 MiB、PDF 最多 500 页、提取正文最多 500000 字符、最多 800 个片段；
每段约 1000 字符，相邻段重叠 150 字符，每个会话最多关联 5 个文件。
DOCX 解压后大小不超过 100 MiB、压缩条目不超过 2000 个。

在 Swagger 中展开 `POST /api/files` → `Try it out`，选择文件后执行。
请求为 `multipart/form-data`，字段名为 `file`；返回 201，响应包含：

```json
{
  "file_id": "上传后返回的UUID",
  "filename": "员工手册.pdf",
  "file_type": "pdf",
  "char_count": 5000,
  "chunk_count": 6,
  "preview": "首段文字预览……",
  "warnings": [],
  "created_at": "2026-10-25T12:00:00"
}
```

`GET /api/files/{file_id}` 可再次读取解析信息。上传接口本身不调用模型。
复制 `file_id`，在 `POST /api/chat` 或 `/api/chat/stream` 中使用：

```json
{
  "message": "文件中规定的年假如何申请？",
  "file_ids": ["替换为上传返回的文件UUID"],
  "web_search": false
}
```

上传后，准备阶段向 Agent 提供当前会话的文件清单，并将这条请求的 `file_ids` 对应文件写入当前问题的「本条问题附带的文件」资料。已有文件与本轮新附件可明确区分。Agent 自主选择 `search_documents` 查询具体条款，或 `summarize_document` 总结全文、章节和广泛主题；可以生成查询范围并指定清单中的文件 ID，也可以与 `web_search` 组合使用。
后续仅带上响应中的 `conversation_id` 和新问题，即可由 Agent 继续查询已关联文件。
`file_ids` 可以给已有会话追加文件，不会替换已有附件。模型回答及文件关联在同一数据库事务中提交，失败时保留文件解析结果供重试。
会话详情接口包含 `files` 数组，前端刷新和切换时恢复文件绑定状态，不显示已关联附件卡片。

普通及流式完成响应新增 `document_sources`，包含 `file_id`、`filename`、`chunk_index`、`location` 和 `content`。
它们是本次实际送入模型上下文的参考片段，不代表模型核实或引用了每一个片段。
`search_documents` 按关键词和中文相邻双字匹配，最多选 4 个片段，每段最多 700 字符，再受整体上下文预算限制，适合具体事实查询。
`summarize_document(file_id, query)` 执行「定位相关章节 → 分段读取 → 分段总结 → 合并汇总」。它读取指定文件全部非空分块，每批最多约 12000 个 JSON 字符，保留每块完整正文；标题只辅助定位，不用于过滤正文。每批归纳相关内容，全部分段结果进入逐级合并，不按 top-k 丢弃后续批次。
工具只访问服务端校验后的当前会话文件，未调用时不读取正文分块。所有工具共享每轮最多 2 轮、合计 3 次调用的外层预算；摘要内部的模型调用在一次工具执行中完成，用量累计到本轮 `usage`。摘要最多 128 个读取批次、8 层合并；分段归纳与中间合并最多 4096 Token，最终摘要及综合回答最多 8192 Token。分段或中间合并输出截断时自动缩小当前输入后重试，完整保留文字和摘要要点；超过处理上限、不能继续拆分、最终输出截断或结果格式异常时返回错误，不把未完成处理显示为完整摘要。
普通及流式完成响应的 `document_summaries` 包含文件名、查询范围、摘要、相关主题和 `coverage`（已处理 / 总片段数、批次数、起止位置、`all_extracted_text_scanned`、解析提示）。页面仅缓存展示所需元数据；数据库仍只保存最终问答。完整扫描指全部已提取文字，不含未提取的扫描页或图片，也不保证模型归纳毫无遗漏。

数据库保存提取后的文本、分块和文件信息。表格原文件保存在 `.data/files/<文件UUID>.bin`，重启后仍可分析；表格正文只保存表头和前 5 行预览。其他文档原文件不长期保留。部署或备份时，需要同时保存数据库和 `.data/files/`。
移除待发送附件只取消本轮选择；删除会话会删除关联，不删除可被其他会话使用的文档及分块。
上传后未使用的文档也会保留，当前没有自动清理任务。

文件错误沿用统一响应：415 类型不支持，413 文件或解析规模过大，400 文件损坏、无文字或会话文件数量超限，404 文件不存在。
文件正文仅作为参考数据，不作为系统指令；只在所关联会话范围内检索。
当前服务没有用户认证或多用户权限隔离，适用于现有的本机使用方式。

## 语义检索、表格分析和精确计算

三个工具已接入普通和流式聊天，共享现有 2 轮、合计 3 次工具调用预算。工具按服务端文件类型开放；表格不会使用文档预览进行统计。参数错误、除零和缺失列等错误回传模型，允许在预算内修正。

`semantic_search(query, file_id?, limit=4)` 使用 BGE 中文向量模型检索当前会话文档。模型只在工具实际调用时加载；只使用本地已有权重，不在在线请求中下载。首次准备模型可执行：

```powershell
.\.venv\Scripts\python.exe -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5').save('.data/models/bge-small-zh-v1.5')"
$env:SEMANTIC_MODEL_PATH = "$PWD\.data\models\bge-small-zh-v1.5"
```

模型准备命令需要访问模型下载站点。也可把预先下载的完整模型目录配置到 `SEMANTIC_MODEL_PATH`；`SEMANTIC_DEVICE` 默认为 `cpu`。未准备模型时工具返回明确错误，Agent 可改用关键词检索。相似度不是答案正确率；结果保留实际命中的短片段和原始位置。

`analyze_spreadsheet(file_id, ...)` 支持 `sheets`、`preview`、`profile` 和 `aggregate`；工作表可按名称或从 0 开始的索引选择。`filters` 按列精确匹配，多个条件为 AND，null 表示空值。聚合支持 sum、mean、min、max、count、按列分组，以及 sort=asc/desc。count 未指定列时统计行数，指定列时统计非空值。limit 只限制展示条数，统计处理全部匹配行；结果提供 matched_rows 和截断标记。浮点统计不保证财务小数精确性。

表格限制：单文件 20 MiB、最多 20 张工作表、每张最多 100000 行和 100 列、工作簿总数据单元格最多 2000000 个、XLSX 解压后最多 100 MiB。空工作表可以列出和预览。原文件缺失时提示重新上传。

`calculate(expression, decimal_places=28)` 使用 AST 白名单和 Fraction 精确计算，支持十进制、科学计数法、括号及 `+ - * / // % **`。`exact` 始终是精确结果，`decimal` 是展示小数，`rounded` 表示是否舍入；整数幂指数限定为 -100～100，不执行函数或变量。表达式最多 1000 字符，展示小数最多 100 位。

验证命令（不调用外部模型或真实数据库）：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```
