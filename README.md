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
`web_search` 默认为 `false`；开启后先搜索当前问题，再让模型参考结果回答。
历史消息只允许 `user`、`assistant` 角色，系统提示词由服务端设置。
每条输入最多 8000 字符，历史最多 40 条，总输入最多 60000 字符。
服务端将对话原文保存在 MySQL，默认读取最近 20 条用户及助手消息作为记忆。
API 校验限制总历史及当前问题最多 60000 字符；实际模型输入由 `service/context.py` 统一裁剪，当前字符预算为 6000，包含系统规则、资料、当前问题和历史，不影响数据库中的完整原文。
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

- `sources`：本次检索的网页来源 `sources` 和实际加入上下文的文件片段 `document_sources`。
- `delta`：正文片段，数据格式为 `{"text": "回答片段"}`。
- `done`：模型正常结束且数据库保存成功后，返回完整的 `ChatResponse`。
- `error`：流开始后的错误，数据包含统一管理的 `status` 和 `detail`。

输出开始前的校验、会话及搜索错误仍通过 HTTP 状态码返回；
开始输出后 HTTP 状态已确定，错误通过 `error` 事件返回，前端会显示提示。
服务端仅在模型正常结束后保存完整问答；没有正常结束标记的半段回答不写入记忆。
停止接收或连接中断时，页面保留已显示的文字，输入框保持清空；部分回答不会写入浏览器会话历史。
若中断发生在数据库提交附近，保存状态需重新加载会话确认。

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
  "history": [],
  "web_search": true
}
```

服务使用当前问题的前 400 字符作为搜索词，获取最多 5 条结果后交给模型生成回答。
响应中的 `sources` 返回本次检索的参考来源；它们不代表模型逐一引用或独立核实了全部网页。
多轮对话中的追问应带上明确主题，搜索词不会自动结合历史改写。
未开启联网搜索时 `sources` 为空列表，也不要求配置 Tavily 密钥。

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
- `service/chat.py`：组装系统提示词和对话历史，按需搜索并附加参考资料。
- `service/context.py`：统一控制模型输入的字符预算及参考资料。
- `service/files.py`：提取 TXT、MD、PDF、DOCX 文字并分块。
- `storage/documents.py`：文件元数据、分块和会话文件查询。
- `agent/rag.py`：基于关键词和中文双字匹配的文件片段检索。
- `service/memory.py`：异步读取记忆、裁剪上下文、提交本轮问答。
- `storage/mysql.py`：MySQL 连接、事务、历史查询及并发冲突检测。
- `agent_db.sql`：统一管理会话和文档表的初始化脚本，保留已有数据。
- `tools/web_search.py`：调用 Tavily，处理网络异常并提取网页标题、链接及摘要。
- `agent/llm.py`：普通及流式异步模型调用，60 秒请求超时、最多一次重试。
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
- 点击输入框内部左下角的 `+` 选择 TXT、MD、PDF、DOCX。识别完成后，输入文件相关问题并发送；附件在本轮回答成功后关联当前会话。
- 附件卡片仅显示待发送文件，发送成功后隐藏；文件仍绑定会话并用于后续问答。回答下方的「文件参考片段」可展开查看来源内容。
- 点击「联网搜索」后，发送的请求会带上 `web_search: true`，回答下方会展示返回的参考来源。
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
联网搜索还会使用 Tavily 账户额度；可先用 `/api/search` 验证搜索，再开启聊天的 `web_search`。
`GET /health` 可用于检查服务是否启动，不调用模型。

## 文件上传与问答

先安装 `requirements.txt`，再执行 `.\.venv\Scripts\python.exe -m storage.mysql --init` 补齐文档表。
上传依赖为 `python-multipart`、`pypdf` 和 `python-docx>=1.1.0`，无需额外的文件识别 API 密钥。

支持 TXT、Markdown、带文字层的 PDF、DOCX 段落及表格。PDF 保留页码，DOCX 的位置标为「正文」，不生成不可靠的页码。
扫描 PDF 或图片没有可提取文字时会提示需要 OCR；当前不支持图片识别、OCR、旧版 DOC、Excel 和密码保护文件。
混合型 PDF 的无文字页面会出现在上传结果的 `warnings` 中，可用页面仍继续解析。

限制：单文件最多 10 MiB、PDF 最多 200 页、提取正文最多 200000 字符、最多 400 个片段；
每段约 1000 字符，相邻段重叠 150 字符，每个会话最多关联 5 个文件。
DOCX 解压后大小不超过 50 MiB、压缩条目不超过 2000 个。

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

后续仅带上响应中的 `conversation_id` 和新问题，即可继续检索已关联文件。
`file_ids` 可以给已有会话追加文件，不会替换已有附件。模型回答及文件关联在同一数据库事务中提交，失败时保留文件解析结果供重试。
会话详情接口包含 `files` 数组，前端刷新和切换时恢复文件绑定状态，不显示已关联附件卡片。

普通及流式完成响应新增 `document_sources`，包含 `file_id`、`filename`、`chunk_index`、`location` 和 `content`。
它们是本次实际送入模型上下文的参考片段，不代表模型核实或引用了每一个片段。
当前按关键词和中文相邻双字匹配检索，最多选 4 个片段，每段最多 700 字符，再受整体上下文预算限制。
概览或总结类问题会按文件轮流选择开头片段，因此属于片段概览，不能保证覆盖全文；需要全文总结时应后续增加分段汇总流程。

数据库保存提取后的文本、分块和文件信息，上传原始文件不长期保留。
移除待发送附件只取消本轮选择；删除会话会删除关联，不删除可被其他会话使用的文档及分块。
上传后未使用的文档也会保留，当前没有自动清理任务。

文件错误沿用统一响应：415 类型不支持，413 文件或解析规模过大，400 文件损坏、无文字或会话文件数量超限，404 文件不存在。
文件正文仅作为参考数据，不作为系统指令；只在所关联会话范围内检索。
当前服务没有用户认证或多用户权限隔离，适用于现有的本机使用方式。
