# 企业智能助手

基于 FastAPI 和 DeepSeek 的聊天服务，支持文件上传问答、SenseNova 图片理解与文生图、MySQL 会话记忆、多轮上下文、Tavily 联网搜索和 API 文档。

当前模型层使用 LangChain `ChatOpenAI`，Agent 流程使用 LangGraph 的 `model → tools → model` 状态图。前端、HTTP/SSE 接口和 MySQL 会话机制保持兼容。

迁移对照：`agent/llm.py` 和 `agent/runner.py` 文件顶部完整保留旧实现的逐行注释，新实现从“当前实现”标记开始。新增的 `agent/graph.py`、`agent/state.py` 和 `agent/tool_registry.py` 分别负责节点路由、本轮状态和工具适配，并附有中文说明。

完整的项目结构、逐文件代码职责、技术栈、数据库与接口说明见 [开发文档](DEVELOPMENT.md)。

## 登录与数据库升级

已实现网页注册、账号密码登录、退出和会话/文件/图片的用户隔离。先在 MySQL 客户端完整执行 [migrations/001_auth.sql](migrations/001_auth.sql)（默认数据库 `agent_db`，可重复执行并保留旧数据）。启动后可在登录页点击「注册账号」，注册成功自动进入工作台。已执行登录迁移的数据库无需额外 SQL。也可以通过管理命令创建账号：

```powershell
.\.venv\Scripts\python.exe -m storage.users admin
```

密码通过隐藏输入设置，长度 12–128 个字符。如需在创建账号时认领升级前的无主数据，添加 `--claim-legacy`。启动后访问首页会跳转登录页，侧栏可退出。HTTPS 部署需设置 `AUTH_COOKIE_SECURE=true`。完整部署、旧数据认领和 Swagger 的 CSRF 参数说明见 [登录功能说明](AUTHENTICATION.md)。

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

- `tool_start`：Agent 准备调用工具，`name` 区分 `web_search`、`search_documents`、`summarize_document`、`analyze_spreadsheet`、`analyze_image`、`generate_image` 和 `calculate`；`query` 展示查询范围、文件名或表达式；`reset: true` 清除中间说明，等待最终回答。
- `tool_progress`：摘要工具的章节定位、分段读取 / 归纳、合并进度，包含 `stage`、`message`，分段处理时还有 `completed`、`total`。
- `sources`：本轮网页来源 `sources`、是否已执行联网搜索 `searched`、文档引用 `document_sources` 及已完成的摘要 `document_summaries`；首次事件来源为空，工具执行后更新。
- `generated_images`：本轮生成图片的元数据列表；原件已保存，可以预览下载，最终 `done` 确认图片与问答已关联并提交数据库。
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

六张业务表与两张认证表统一由根目录 `agent_db.sql` 管理。首次部署或需要补建缺失表时，在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m storage.mysql --init
```

初始化读取 `agent_db.sql`，全部建表语句使用 `CREATE TABLE IF NOT EXISTS`，可重复执行，保留现有表结构和数据。
脚本包含 `conversations`、`messages`、`documents`、`document_chunks`、`conversation_documents` 和 `generated_images`，不包含删表语句。
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

数据库保存角色、消息原文及生成图片的元数据和消息关联；文字模型、Token 用量及搜索来源卡片的额外信息目前只在浏览器缓存。
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
- `agent/runner.py`：LangGraph 事件适配器，保持服务层原有的 `run_agent()` 调用接口。
- `agent/graph.py`：模型与工具节点、条件路由、参数及文件范围校验、工具预算、缓存、来源和用量。
- `agent/state.py`：图状态、请求上下文、最终结果及断开连接信号。
- `agent/tool_registry.py`：将原工具注册成 LangChain `StructuredTool`，注入会话文件范围和摘要进度。
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
- `agent/llm.py`：LangChain 普通及流式模型调用，保留原结果结构、默认超时、重试以及摘要的 JSON 模式；停止接收时显式关闭响应流。
- `agent/prompts.py`：现有的企业 Agent 系统提示词。
- `static/index.html`：聊天页面结构。
- `static/styles.css`：页面样式和移动端适配。
- `static/app.js`：聊天请求、会话管理、Markdown 及来源展示。
- `skills/`：待扩展的技能模块。

当前版本实现聊天、联网搜索、上传文件检索和 MySQL 会话记忆；其他企业知识库及执行工具尚未接入。
默认仅监听本机，业务 API 需要登录，并按用户校验会话、文件及图片归属。

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

## SenseNova U1.5 Lite 文生图

在聊天框输入“生成一张海豹插画，512x512，清晨海面、柔和光线”等要求，DeepSeek 会调用 `generate_image(prompt, size)`。图片保存后即展示，点击图片可以打开大图，右上角的下载按钮可保存原图。每轮最多生成一张，支持文生图。

复用已有的 `SENSENOVA_API_KEY`，默认模型为 `sensenova-u1.5-lite`。可在 `.env` 独立配置：

```dotenv
SENSENOVA_IMAGE_MODEL=sensenova-u1.5-lite
SENSENOVA_IMAGE_BASE_URL=https://token.sensenova.cn/v1
SENSENOVA_IMAGE_TIMEOUT=300
SENSENOVA_IMAGE_WATERMARK=true
SENSENOVA_IMAGE_PROMPT_EXTEND=true
# 需要独立密钥时配置 SENSENOVA_IMAGE_API_KEY，省略则复用 SENSENOVA_API_KEY
```

升级时执行 `.\.venv\Scripts\python.exe -m storage.mysql --init` 补建 `generated_images`，随后重启服务。工具会在生成前检查表是否存在，缺表时明确提示初始化；已有普通会话仍可读取。

按照 [SenseNova 官方文档](https://platform.sensenova.cn/docs#model-u1-5) 调用 `POST /v1/images/generations`，显式传入 `n=1`、`output_format=png`、`response_format=b64_json`、水印和提示词增强设置。尺寸默认 `1024x1024`，可指定 `auto`、`2K`、`4K`，或 512–4096 内、32 的倍数的宽高，长宽比最多 3:1。生成描述最多 2000 字符；图片原件最多 20 MiB，为项目资源限制。

原件和展示元数据保存在 `.data/generated/<图片UUID>.bin`、`.json`；模型上下文只保留简短元数据。`GET /api/images/{image_id}` 提供预览，加 `?download=true` 下载原图。普通聊天和流式完成响应增加 `generated_images`；会话详情的助手消息同样携带该字段，刷新页面可从 MySQL 恢复图片。生图模型用量单独记录，`ChatResponse.usage` 继续表示文字和图片理解等现有模型的累计用量。

相同参数只在本轮复用结果，HTTP 创建请求不自动重试；再次发送会生成新图片。等待时每 15 秒发送进度心跳。停止接收会关闭本地等待，不保证撤销上游已接受的任务。图片已保存但最终文字生成或数据库提交失败时，页面保留图片预览，问答保存状态以 `done` 或重新加载结果为准。删除会话会级联删除图片关联，本地原件当前保留。

核心代码：`agent/image_generation.py` 调用模型；`tools/generate_image.py` 校验并保存图片；`storage/generated_images.py` 管理原件和消息关联；`schemas/images.py` 定义响应；`router/images.py` 提供图片读取。

当前离线验证覆盖文档检索、全文摘要、工具路由与聊天保存（模型响应由测试模拟）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check static/app.js
```

## SenseNova 图片理解

主对话继续使用现有 DeepSeek 配置，图片理解通过独立的 SenseNova 模型完成。在 `.env` 中配置：

```dotenv
SENSENOVA_API_KEY=你的SenseNova密钥
SENSENOVA_MODEL=sensenova-6.8-flash-lite
SENSENOVA_BASE_URL=https://token.sensenova.cn/v1
SENSENOVA_TIMEOUT=180
SENSENOVA_MAX_TOKENS=2048
```

安装 `requirements.txt` 中的 Pillow 依赖并重启服务。前端可选择或粘贴 JPG、JPEG、PNG、WebP 静态图片，再发送“读取这张图片中的文字”等问题。上传阶段只校验和保存图片，发送问题后才由 Agent 调用 `analyze_image(file_id, query)`。接口继续使用 `POST /api/files` 上传，再将返回的 ID 放入聊天请求的 `file_ids`；成功绑定后可携带 `conversation_id` 追问。

`service/images.py` 负责实际格式校验、EXIF 方向校正、白底 JPEG 转换及缩放；`agent/vision.py` 通过 LangChain 调用 SenseNova；`tools/analyze_image.py` 读取当前会话图片并返回识别结果。工具沿用 `StructuredTool.from_function()` 注册，使用现有 LangGraph 工具节点、范围校验、缓存、取消处理和 15 秒心跳。视觉模型用量计入最终 `usage`；多模型 Token 合计不代表费用相同。

本项目限制单张图片为 20 MiB、2400 万像素，发送前最长边缩至 2048 像素；这些是项目资源限制。图片原文件与表格一起保存在 `.data/files/<文件UUID>.bin`，元数据和会话关联仍由 MySQL 管理，图片的 `char_count` 和 `chunk_count` 为 0。识别结果通过 `document_sources` 返回，位置标明“图片视觉识别（模型输出）”，不是原文文本块。图片问题使用 24000 字符上下文预算，仍共享每轮 2 轮、合计 3 次工具调用上限。

此版本处理独立静态图片，不自动识别 PDF 内嵌图片、视频或动画。小字和压缩后的细节可能无法辨认；扫描 PDF 可先转为图片上传。数据库或模型服务不可用时仍按现有异常机制返回错误，缺少视觉配置时提示检查 `SENSENOVA_*`。

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

上传后，准备阶段向 Agent 提供当前会话的文件清单，并将这条请求的 `file_ids` 对应文件写入当前问题的「本条问题附带的文件」资料。已有文件与本轮新附件可明确区分。Agent 通过直接回答或工具选择完成意图分流：通用概念和写作直接回答，文件具体事实调用 `search_documents`，全文、章节和广泛主题总结调用 `summarize_document`。不增加一次独立的分类模型请求；可以结合历史生成查询范围、指定清单中的文件 ID，并与网页或计算工具组合。
后续仅带上响应中的 `conversation_id` 和新问题，即可由 Agent 继续查询已关联文件。
`file_ids` 可以给已有会话追加文件，不会替换已有附件。模型回答及文件关联在同一数据库事务中提交，失败时保留文件解析结果供重试。
会话详情接口包含 `files` 数组，前端刷新和切换时恢复文件绑定状态，不显示已关联附件卡片。

普通及流式完成响应新增 `document_sources`，包含 `file_id`、`filename`、`chunk_index`、`location` 和 `content`。
文档问答的来源是实际送入模型上下文的片段；全文摘要的来源是完整分段处理后的引用样例，独立保留给界面，不再重复发送给最终回答模型。来源不代表模型逐项核实或引用了所有片段。
`search_documents(query, file_id?, limit=4)` 一次读取当前文件范围，在相同的 400 字原文窗口上执行标准 BM25 和本地 BGE 向量排序，各召回最多 20 项，通过 RRF 融合、去重，最终返回最多 4 项。保留真实命中内容、位置及 `char_start`，不会只截取原始块开头。向量模型不可用时自动降级为 BM25，并通过结果及进度说明实际方法；两路没有有效结果时返回空列表，不拿文件开头充当命中。
`summarize_document(file_id, query, final_answer=true)` 读取指定文件全部非空分块，每批最多约 12000 个 JSON 字符；标题只辅助定位，不过滤正文。Map 只提取紧凑要点：标题最多 80 字符，每项摘要最多 400 字符，最多 3 个代表性来源。完全重复的要点先在本地合并；所有要点能放入最终 24000 字符输入预算时直接生成摘要，超出时才执行 Reduce，不按 top-k 丢弃后续批次。检索窗口的重叠正文仍保留，避免误删真实重复条款。
单份摘要完成后图直接结束，经现有保存流程返回，主 Agent 不再重写一遍。对比、联网查证或计算等组合任务使用 `final_answer=false`；同轮多个工具、或已有工具结果时服务端也会强制采用素材模式。素材按当前问题和摘要数量分配预算，每份最多 6000 字符；主 Agent 获取全部结果后一次生成最终回答。格式、语言和篇幅要求应包含在 `query` 中，单份最终摘要同时接收用户原始问题。
工具只访问服务端校验后的当前会话文件，未调用时不读取正文分块。所有工具共享每轮最多 2 轮、合计 3 次调用的外层预算；摘要内部用量累计到本轮 `usage`。摘要最多 128 个读取批次、8 层合并；Map 与 Reduce 输出上限从 4096 降为 2048 Token，最终正文仍最多 8192 Token。输出截断时缩小当前输入重试，保留所有输入文字或要点；格式异常、不能继续拆分、最终输出截断或超出处理上限时返回错误，不显示虚假的完整覆盖。合并不能有效缩小时停止并报错。
成功完成的 Map 产物与最终摘要缓存在 `.data/summary_cache/`，按文件 UUID、正文和位置、查询范围、解析提示、模型配置及摘要版本生成指纹。最终正文还校验原始问题，格式要求变化时可复用 Map 后重新成文。缓存有效期 7 天，每个文件最多 8 项；损坏、过期或不完整的缓存被忽略。完整最终缓存命中不再调用摘要模型，`usage` 只统计本轮真实请求。缓存是可重建的派生数据，无需数据库迁移。
普通及流式完成响应的 `document_summaries` 包含文件名、查询范围、摘要、相关主题和 `coverage`（已处理 / 总片段数、批次数、起止位置、`all_extracted_text_scanned`、解析提示）。页面仅缓存展示所需元数据；数据库仍只保存最终问答。完整扫描指全部已提取文字，不含未提取的扫描页或图片，也不保证模型归纳毫无遗漏。

数据库保存提取后的文本、分块和文件信息。表格和图片原文件保存在 `.data/files/<文件UUID>.bin`，重启后仍可分析；表格正文只保存表头和前 5 行预览，图片不生成文本块。其他文档原文件不长期保留。部署或备份时，需要同时保存数据库和 `.data/files/`。
移除待发送附件只取消本轮选择；删除会话会删除关联，不删除可被其他会话使用的文档及分块。
上传后未使用的文档也会保留，当前没有自动清理任务。

文件错误沿用统一响应：415 类型不支持，413 文件或解析规模过大，400 文件损坏、无文字或会话文件数量超限，404 文件不存在。
文件正文仅作为参考数据，不作为系统指令；只在所关联会话范围内检索。
当前服务已提供用户登录与资源隔离；多人部署前按 [登录功能说明](AUTHENTICATION.md) 执行数据库迁移、创建账号并配置 HTTPS Cookie。

## 语义检索、表格分析和精确计算

文档混合检索、表格分析和计算已接入普通和流式聊天，共享现有 2 轮、合计 3 次工具调用预算。工具按服务端文件类型开放；表格不会使用文档预览进行统计。参数错误、除零和缺失列等错误回传模型，允许在预算内修正。

`search_documents` 内置 BGE 中文向量召回，Agent 无需分别调用关键词和语义工具。`tools/semantic_search.py` 保留独立 Python 检索入口，默认不再单独向 Agent 开放。模型只在实际文档问答时加载；只使用本地已有权重，不在在线请求中下载。首次准备模型可执行：

```powershell
.\.venv\Scripts\python.exe -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5').save('.data/models/bge-small-zh-v1.5')"
$env:SEMANTIC_MODEL_PATH = "$PWD\.data\models\bge-small-zh-v1.5"
```

模型准备命令需要访问模型下载站点。也可把预先下载的完整模型目录配置到 `SEMANTIC_MODEL_PATH`；`SEMANTIC_DEVICE` 默认为 `cpu`。未准备模型时混合工具自动降级为 BM25，并明确说明。语料与查询向量分开缓存在内存，改写查询不会挤掉整份文件的向量。相似度和 RRF 分数都不是答案正确率。

`analyze_spreadsheet(file_id, ...)` 支持 `sheets`、`preview`、`profile` 和 `aggregate`；工作表可按名称或从 0 开始的索引选择。`filters` 按列精确匹配，多个条件为 AND，null 表示空值。聚合支持 sum、mean、min、max、count、按列分组，以及 sort=asc/desc。count 未指定列时统计行数，指定列时统计非空值。limit 只限制展示条数，统计处理全部匹配行；结果提供 matched_rows 和截断标记。浮点统计不保证财务小数精确性。

表格限制：单文件 20 MiB、最多 20 张工作表、每张最多 100000 行和 100 列、工作簿总数据单元格最多 2000000 个、XLSX 解压后最多 100 MiB。空工作表可以列出和预览。原文件缺失时提示重新上传。

`calculate(expression, decimal_places=28)` 使用 AST 白名单和 Fraction 精确计算，支持十进制、科学计数法、括号及 `+ - * / // % **`。`exact` 始终是精确结果，`decimal` 是展示小数，`rounded` 表示是否舍入；整数幂指数限定为 -100～100，不执行函数或变量。表达式最多 1000 字符，展示小数最多 100 位。
