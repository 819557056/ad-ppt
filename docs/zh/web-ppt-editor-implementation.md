# Web PPT 对象编辑器运行与验证

设计基线见 [`web-ppt-editor-design.md`](./web-ppt-editor-design.md)。新流程是 `scene_v1`，与旧整页图片项目及旧导出隔离。

**当前状态（2026-10-03）**：后端 Scene/任务、前端编辑器、导出/运维三个并行模块已整合。最新 `source-v13` 已用原始三个 Dockerfile 重建，在新空卷验证完整 Linux Compose、33 页模板、浏览器编辑/冲突、PowerPoint 原生对象往返以及另一个空集群备份恢复。LibreOffice 实际文字/行距/越框/裁剪的**不可豁免检查**已进入真实 worker→renderer→报告→审核链路，旧未审核 PPTX 必须重新导出。source-v13 部署回归为 Windows 后端 **366 passed / 17 skipped**、Linux **116 passed / 7 skipped**、正式 PostgreSQL **19 passed**、隔离 renderer **3 passed**、前端 **76 passed**。新增真实契约入口后的最新 Windows 联合回归为 **406 passed / 18 skipped**；Linux 契约专项 **44 passed / 1 skipped**，其中真实计费测试均按默认策略跳过。全量 TypeScript 仍有 118 行既有诊断；真实模型计费/出网、WPS、视觉阈值标定仍未验收，不能声称首版全部验收完成。已新增默认跳过的真实网关契约测试入口（见下节），但尚无真实付费测试配置或授权。逐轮记录保留历史事实，最新完整部署证据见 source-v13 小节。

## 当前验收边界（2026-10-03 用户更新）

当前先完成功能代码、单元/组件测试、本地构建和离线回归。以下六类环境验收均延期，待用户提供环境再执行；不再作为当前代码交付的阻塞条件：

1. Linux Docker 部署、renderer 隔离与安全验证。
2. PostgreSQL 实际并发事务验证。
3. 干净 Linux 环境的数据库＋资产一致性备份恢复演练。
4. PowerPoint/WPS 实机打开、可编辑性、字体换行及 PDF/浏览器视觉一致性。
5. 真实 Sub2API 文本/图片调用、扣费及断连/崩溃后结果未知场景。
6. 指定 33 页 PPTX 的 Linux LibreOffice 转换及第 6、22、30 页复核。

对应实现、质量门与显式测试入口全部保留，不因延期放宽发布检查。上方统计和下方逐轮部署/Office/模型记录属于历史证据，不意味着本轮重新执行；历史镜像与 hash 也不代表后续功能修改后的源码。当前不索要模型 Key、不启动计费调用、不自动运行上述验收。延期不等于测试通过，代码完成与生产环境验收分别报告。

## 启动

正式部署使用 `docker-compose.scene.yml`。复制 `.env.example` 到部署环境并设置 `SCENE_POSTGRES_PASSWORD`、`SCENE_DATABASE_URL`、`SECRET_KEY`、`ACCESS_CODE`、`MODEL_GATEWAY_ALLOWLIST`、`CREDENTIAL_ENCRYPTION_KEY` 和 `SCENE_ORIGIN`。`SCENE_DATABASE_URL` 应指向同一 compose 中的 `postgres:5432/banana_scene`，密码需 URL 编码。可用 `python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` 生成凭据加密主密钥；该密钥应作为部署 secret 备份，不得随意轮换，否则已存用户 Key 无法解密。

```sh
docker compose -f docker-compose.scene.yml up --build -d
docker compose -f docker-compose.scene.yml logs -f backend worker renderer
```

仅前端 `127.0.0.1:${FRONTEND_PORT:-3011}` 对宿主机发布端口，正式访问应经 TLS 与受限网络/反代。`backend` 和 `worker` 必须共享同一个 `scene-assets` volume 与 `ASSET_STORE_ROOT`，否则任务虽可能完成，下载会报告 `ASSET_UNAVAILABLE`。`worker` 与无网络权限的 `renderer` 共享 `scene-jobs`；官方 Compose 的 worker 为每份渲染作业从持久追加日志分配不重用的 UID/GID，并把 0700 私有目录移交给该身份；renderer 协调进程仅为降权启动和回收子进程保留必要能力，Chromium/LibreOffice 子进程以各自 UID 运行，无法读取后来作业的目录。清理失败时 renderer 停止接收新作业。普通本机开发可不启用逐作业 UID；这种非隔离模式不得用于不可信 PPTX 的正式部署。不要把 SQLite 用于多进程正式队列。

### 维护窗口备份与恢复

`scripts/scene-backup.sh` 仅用于 Linux Docker Compose 部署。先阻断反代写流量、确认没有外部数据库写入者，并在当前部署目录设置与运行时相同的 `.env`。脚本会停止原本运行的前端/API/worker/renderer，检查 PostgreSQL 无其他客户端，然后导出数据库、`scene-assets` 与 `legacy-uploads` 卷、字体及镜像清单；对所有 ready 资产字节、生成计划/Scene/快照哈希及已存模型 Key 的可解密性做只读审计。最后写 `SHA256SUMS` 和 `COMPLETE`；失败时没有 `COMPLETE`，脚本尝试恢复原先运行的服务。**加密主密钥不在备份中**，须另行安全保管。

```sh
SCENE_MAINTENANCE_CONFIRMED=yes bash scripts/scene-backup.sh /secure/scene-backups/2026-10-02
```

恢复仅针对**全新、空的** PostgreSQL 数据库和两个空数据卷；不能在已有实例上覆盖。先部署相同代码镜像、字体 manifest、环境变量和原 `CREDENTIAL_ENCRYPTION_KEY`，只启动 `postgres`。脚本会验证备份 SHA-256/字体清单、无外部数据库连接、目标数据库无表及两个卷为空；恢复 `pg_dump` custom format 和两个卷后重新审计，并把原本 `running` 的任务租约置为过期，以便 worker 恢复时按派发状态处理付费调用（可能计费的结果未知不自动重试）。恢复脚本**不会自动启动**其余服务；任何失败都应保留现场并重新准备全新空目标，不能在半恢复状态上再次运行。

```sh
docker compose -f docker-compose.scene.yml up -d postgres
SCENE_RESTORE_CONFIRM=restore-into-empty-stack bash scripts/scene-restore.sh /secure/scene-backups/2026-10-02
docker compose -f docker-compose.scene.yml up -d backend renderer worker frontend
```

启动后再检查 `GET /api/v2/readiness`、历史项目/素材和待处理任务。新挂载的 `legacy-uploads` 只备份该卷中的旧文件；如果升级前旧文件仅存在于旧容器可写层，必须在淘汰旧容器前单独迁入该卷。当前脚本已在 Linux Docker / PostgreSQL 16.6 上完成真实备份与全新空集群恢复；恢复后再次检查 readiness、历史 Scene、导出原文件及 33 张参考图 hash。此样本没有真实生成计划或付费 attempt，不把该演练扩展为真实模型故障恢复验收。

Windows 本地开发可运行前后端并用临时 SQLite 做无模型回归；PPTX 参考模板上传与静态化必须配置通过逐作业 UID 烟测的 Linux 隔离 renderer；否则接口返回 `TEMPLATE_RENDERER_UNAVAILABLE`。PPTX/PDF 导出均需完成 Chromium TextLayout 测量；本地测试可设置 `SCENE_CHROMIUM_PATH` 指向已安装的 Chrome/Chromium。正式验收仍以 Linux 容器和固定字体为准。

`/health` 只表示 Web 进程存活；受访问码保护的 `GET /api/v2/readiness` 才检查数据库、新 worker 心跳/有效任务租约、资产卷可写空间、固定字体 SHA-256，以及隔离 renderer 的近期 Chromium PDF 与 LibreOffice PPTX 小样本烟测。正式部署需待该接口返回 200 再接收 Scene 创作流量；renderer 的自检每五分钟重跑，状态心跳独立于长时间转换作业。当前 readiness 已在源栈和全新恢复栈的 Linux Compose 上实跑，全部检查通过。

## 使用路径

开启 `SCENE_EDITOR_ENABLED=true` 后，从首页的“对象编辑”入口创建项目；在编辑器中确认大纲、设置自己的模型网关 Key 和文本/图片模型，选择参考模板，再按当前页、指定多页或全部页生成候选。凭据默认检查只查询网关模型列表，不执行生成，也不证明具体文本/图片能力。用户可在填写模型 ID 后明确确认可能计费，分别进行最小文本/图片能力试调用；每次试调用作为持久任务记录 dispatch/usage，失败或结果未知时不得静默重复扣费。文字模型先保存不可变 DraftScene，图片请求各自成为持久 `generate_asset` 子任务；只有全部素材 ready 后才在本地 fenced finalize 生成候选。局部 AI 修改如果请求替换选中图片，也走独立素材子任务、范围校验和候选审核。失败素材可单独重试，成功素材不重画，可能再次计费的步骤需明确确认。AI 不直接修改 head，需预览并接受候选。手工对象操作生成新的不可变 revision。导出先冻结 snapshot，再由独立 worker 生成 PPTX/PDF；导出结果提供文件和 JSON 质量报告。`/api/v2/projects/{id}/tasks` 可按 cursor 找回项目任务；项目、版本、候选、参考页和模型凭据列表也按 owner/cursor 分页；`/api/v2/projects/{id}/exports?limit=20&cursor=...` 和网页“历史导出”可找回待审核/已完成产物。

模板面板在参考页完成风格分析后提供“自动匹配建议”：依据已分析的 cover/agenda/content/section/closing 角色与大纲标题/页位分配，不使用未分析页，不覆盖人工绑定的页面；先展示逐页建议，再经用户确认应用。该首版匹配是确定性启发式，不是模型对内容的智能匹配；任何页版本冲突会停止后续应用、刷新项目并明确报告已完成页数，用户可逐页调整。人工绑定未分析页时会提示视觉模型需求和可能费用；此路径把冻结的私有预览压缩为有界 JPEG 并作为 `image_url` 输入文本模型，不会只传一个模型无法看到的素材 ID。已分析页使用冻结的结构化风格摘要；若模型不支持视觉输入，未分析页任务应明确失败而不是假装参考生效。风格分析任务自身也冻结预览资产 ID/hash，派发付费调用前复核实际字节。离线测试验证未分析图片输入、分析后结构化摘要及素材损坏不派发模型调用；真实视觉模型兼容性仍待验收。

含生成图片的候选标记 `IMAGE_TEXT_UNVERIFIED`，网页要求先预览再接受；目前没有可靠 OCR 自动判定图片内正文，因此不能以提示词“无文字”代替人工检查重影/烘焙文字。

两种导出报告均记录逐页 revision_id/scene_hash、text/image 数量、role_counts，以及每个 element_id/kind/role/z_index；图片记录 asset_id，背景独立列出而不计为可移动图片。报告不复制业务正文；完整 TextLayout、素材 hash 与引擎信息仍关联原快照。PPTX 的 `PPTX_FONT_NOT_EMBEDDED` 是接收端字体安装要求，与实际观察到的 renderer 字体告警分开记录。

导出结构与文字检查通过后状态为 `needs_review`，不是自动发布。正式部署的隔离 renderer 会生成浏览器 Scene 对照 PDF 栅格图或 LibreOffice PPTX 栅格图，并为每页存储并排/差异图与描述性指标；阈值尚未实样标定，不能自动判为视觉通过。用户查看报告、待审核文件和逐页对照后，调用 `POST /api/v2/projects/{id}/exports/{export_id}/review`，提交报告 SHA-256 与 `acknowledged_warning_codes=["VISUAL_DIFF_UNCALIBRATED"]`；服务端核对逐页证据完整性后才发布。没有 renderer 时报告 `visual_comparison=not_run`，不能在 Web UI 中确认通过。

受访问码保护的 `GET /api/v2/openapi.json` 提供 OpenAPI 3.1 合同。它显式列出 Scene 路由、请求字段、成功状态、通用错误、签名资产及 `Idempotency-Key`；集成测试逐条对照 Flask 实际路由，并检查缺少访问码的结构化 401。生成计划确认已加入幂等记录，HTTP 响应丢失后同键重试返回原 `plan_id`，不同输入复用键返回 409。浏览器对“确认计划→创建生成任务”和“冻结快照→创建导出任务”保留两步请求键直至任务到达已知终态；模板文件上传及付费风格分析也保留请求键直至任务结束；中途断连/刷新后可复用相同计划或快照，避免前一步重建导致后一步生成重复资源。六天后未完成请求不再自动重放，需先查询历史任务/导出；会话存储损坏或不可写也阻断新提交。计划请求另附逐页基版本，防止项目规划版本未变但 Scene head 已变化时错误复用。PostgreSQL 对相同 owner/操作/键的并发提交使用事务 advisory lock 串行化；此并发路径已在独立 PostgreSQL 16.2 和正式固定 PostgreSQL 16.6 容器上实测。Scene 的读取、保存、恢复和候选接受现统一返回 `asset_urls`。接受候选会更新对应页缩略图，并将当前页旧 head 加入会话撤销栈；离线测试验证接受→撤销→重做恢复同一 Scene/资产。

## 显式开启真实模型契约测试

设计 §13 要求真实模型契约与 fake Provider 回归分开。新增 `backend/tests/integration/test_scene_live_contract.py`，标记为 `scene_live` / `requires_service`；实现位于 `backend/tests/live_scene_contract.py`。**普通 CI 默认跳过，不读取 `.env`、不查找模型 Key、不回退平台凭据。** 只有显式确认和上游支出上限确认都存在、全部专用配置有效时才访问网络。

准备一个健康的 Scene 测试部署，在应用凭据面板录入**专用测试 Key**，并在网关给该 Key 设置实际硬支出上限。测试只接收已有 `credential_id`，不接收模型 Key；面板访问码通过进程环境传入且不写入证据。下面的确认只是操作者声明，程序无法独立验证上游金额上限，更不把请求数当作金额预算。

```sh
# 仅在获得真实调用/计费授权并设置上游硬支出上限后运行。
# 不把这些开启标志放入应用 .env 或默认 CI 环境。
export PYTHON_DOTENV_DISABLED=1
export SCENE_LIVE_ORIGIN=https://your-scene-test.example
export SCENE_LIVE_GATEWAY=https://your-test-gateway.example/v1
export SCENE_LIVE_CREDENTIAL_ID=已在面板创建的凭据UUID
export SCENE_LIVE_TEXT_MODEL=your-text-model
export SCENE_LIVE_IMAGE_MODEL=your-image-model
export SCENE_LIVE_RUN_DIR=/secure/scene-contracts/run-001
read -r -s -p 'Scene access code: ' SCENE_LIVE_ACCESS_CODE; printf '\n'
export SCENE_LIVE_ACCESS_CODE
export SCENE_LIVE_CONFIRM=run-paid-scene-contracts
export SCENE_LIVE_BUDGET_ACK=dedicated-key-upstream-spending-cap-configured
python -m pytest backend/tests/integration/test_scene_live_contract.py -m scene_live -q
unset SCENE_LIVE_ACCESS_CODE SCENE_LIVE_CONFIRM SCENE_LIVE_BUDGET_ACK
```

`SCENE_LIVE_ORIGIN` 是独立 Scene API 的根 origin，HTTPS 优先，仅 loopback 开发地址允许 HTTP；网关必须 HTTPS 且以 `/v1` 结尾。拒绝 URL 内用户名/密码、query、fragment；不开 TLS 绕过，不跟随重定向，不继承代理环境。`SCENE_LIVE_TIMEOUT_SECONDS` 可设置为 30～1800 秒，默认每个任务等待 300 秒。

测试创建自己的单页 QA 项目，按顺序走**真实 API→持久任务→worker**：模型列表、最小 JSON 文字试调用、`b64_json` 图片试调用、合成无字参考 PNG 导入及单页视觉风格分析。只有前三项完成才继续下一项，合计提交 **3 个可能计费任务**（文字、图片、参考图分析）。测试不提交 `/retry`；服务端仍可能对明确拒绝的 429 按既有 `max_attempts` 重试，因此不是“最多 3 次 HTTP”或固定金额保证。真实服务器保留 attempt、provider request ID 和 usage；测试记录 task/request ID、状态和 attempt count，**不伪造或独立认定账单金额**。

`contract.json` 位于显式绝对路径的证据目录，保存配置/输入 hash、预先持久化的幂等键、资源 ID 和有界状态；不保存访问码、Key、模型原始回复或签名素材 URL。独占锁阻止两个测试同时运行。HTTP 响应丢失、写入失败或任务超时后，保留项目/任务供核查；程序不会自动取消、删除凭据、接受候选、审核导出或提交下一付费步骤。确认已有状态后，**用同一目录显式重跑**，已知任务仅查询，响应未知的提交使用原幂等键恢复；成功重跑不刷新原完成时间，也不是新一轮模型调用证明。

超过六天、配置/测试源码发生变化、记录损坏或遗留 `.running` 锁时拒绝自动恢复。先核对存活进程、已创建任务及账单；不能通过删除记录或换目录来“自动修复”可能已经扣费的请求。QA 项目和资产故意保留，不做隐式清理。

**证明边界**：成功仅证明这次所选模型的最小 JSON 输出、可解码图片，以及实际参考图输入/StyleProfile 合同；不是原生 JSON Schema、图片理解准确性、透明背景、异步 Provider、完整单/多页生成质量、故障恢复计费或网关账单验收。模板分析模型及结果 hash 会再次核对；项目模型被改动或分析结果被手工改写时不冒充原验证结果。实际运行仍须另行核查上游请求/usage/账单。

## 回归

以下逐轮计数保留为历史记录；当前汇总见文末，不叠加不同轮次的重复测试。

前端规范化 Scene 类型由 `backend/schemas/slide_scene_v1.schema.json` 生成至 `frontend/src/features/editor/slideScene.generated.ts`；修改服务端 schema 后运行 `python scripts/generate_scene_types.py`，CI/本地用 `--check` 防止漂移。默认字段在服务端 `model_dump()` 后均已填充，因此生成的是响应类型而不是宽松的输入类型。

```sh
python -m pytest backend/tests/integration/test_scene_v1.py -q
python scripts/generate_scene_types.py --check
cd frontend
npm run test:run -- sceneHash.test.ts
npm run build
```

当前离线 Scene 集成回归为 54 个通过，另有 1 个 schema→TypeScript 生成物漂移检查通过，覆盖 Scene 哈希/Schema、版本冲突、锁、owner 隔离与项目/版本/候选/任务/凭据 cursor 分页、renderer 用户配置与就绪检查与作业串行化、待审候选导出选择、候选 stale、模板图片导入、worker 租约 fencing、素材 DAG 的失败重试/unknown/取消、报告哈希绑定审核、原生 PPTX 结构、图片裁剪/透明度/旋转导出、卷归档路径拒绝与备份资产审计、非隔离 PPTX 上传拒绝和渲染输出文件边界；新增回归覆盖逐页计划版本校验、快照/导出跨步骤响应丢失重放、付费任务重试同键重放、新文字对象不得遮挡锁定标题、付费模板分析与模板上传的同键重放、加页/恢复版本重放及过期签名资产 URL 更新。前端 Scene 哈希、对象几何、预览单位、模板自动匹配、幂等键、跨步骤响应丢失及候选接受后撤销共 21 项测试已通过，`npm run build` 与相关文件 ESLint 通过。2026-10-02 Windows 本地以临时 SQLite、Flask、独立 Scene worker、Vite 和 Chrome 154 实跑：网页口令登录→创建项目→添加并修改“中文 English 123”→保存刷新核对→PDF 导出进入 `needs_review`→刷新后从历史导出找回；无浏览器运行错误。同一快照的 PPTX 导出也进入 `needs_review`，报告与待审核文件均可获取。追加实跑了图片上传、裁剪、透明度、旋转、文字行距/内边距、背景图片上传、刷新与 PPTX/PDF 导出；PPTX 原生文字和 PDFium 可搜索文字均含正确的“中文 English 123”，浏览器预览曾有图片 pt/px 混用，已修复并对照 PDF 栅格复核。另用两个标签页复现 409 冲突、比较后重放草稿；通过阻断 PATCH 验证离线草稿刷新后可恢复并成功保存；通过中文 IME 合成事件验证拼音中间态超过 800 ms 仍不提交，`compositionend` 后提交。未配置隔离 renderer 时 `visual_comparison=not_run`，Web 不展示确认按钮，直接调用审核 API 返回 422。另以同机 renderer daemon 实跑 PDF 浏览器预览对照：生成一页逐页差异图，人工检查文字/位置一致，提交绑定报告 hash 的审核后状态变为 `succeeded` 并提供下载链接。另在同机验证了串行化作业传输：通过共享目录锁提交最小 PDF 作业，Chrome 154 返回 9447 字节 PDF，完成后只留锁/状态文件；renderer 本机自检为 Chromium=true、LibreOffice=false。这证明本地基础流和视觉复核状态机，不代表 Linux 容器、LibreOffice 或正式客户端的视觉验收。Windows Chrome 154 的 Type3 中文 PDF 在 PyMuPDF 中会误报 U+FFFD，PDFium 能按浏览器复制语义提取正确文字；因此 PDF 质检由 PyMuPDF 检查页/尺寸/栅格，PDFium 检查 Unicode 与逐框位置，并保存了小型回归 fixture。全量 `tsc --noEmit` 仍有原项目既存错误，新 Scene 文件经单独筛查和 ESLint 无错误。

后端全套在 2026-10-02 Windows 环境重跑 930 项，结果为 901 通过、22 跳过、7 失败。失败中 2 项需预先运行旧 API 服务，2 项调用真实模型且当前 Key 返回 401，另 3 项受旧 Provider 环境值影响；本轮 Scene 集成回归中 PDF 分支因没有设置 `SCENE_CHROMIUM_PATH` 被跳过。带本机 Chrome 路径运行的 Scene/相关单测则为 109/109 通过。**不能宣称全套或正式环境验收通过**。

**最新本地回归（替代上段旧计数）**：Scene 集成测试 73 项、类型漂移测试 1 项、任务 watchdog 与 app factory 单测 43 项，合计 117 项通过；前端定向测试 32 项、Vite 构建及相关 ESLint 通过。新增验证了候选接受、图片上传、删页、项目归档、候选拒绝、任务取消、导出审核和模型 Key 撤销在响应丢失后同键重放；候选接受的历史响应会重新签发素材 URL。全部页任务取消并结束后，任务组现返回 `cancelled`，不再误报 `failed`；父任务取消后的孤儿素材子任务不会被 worker 领取或消耗 attempt。worker 领取队列改为按 `(created_at, id)` 分批扫描，忙碌 owner 的前 50 个付费任务不再饿死后续 owner 的任务；51 个排队任务的回归已通过。页面 API 现支持按位置插入及复制当前页：复制时核对源页版本/修订、为所有对象重分配 ID、复用同项目素材与参考模板绑定，旧页后续修改不影响副本；网页提供复制和插入按钮。导出前质量门新增单字横向裁切和无可用内框检查；创建快照时校验固定字体清单、所引用素材的实际字节以及持久 Scene hash，快照之后素材损坏也会以明确 `ASSET_UNAVAILABLE` 任务错误阻断导出。图片上传超限现按 API 契约返回结构化 HTTP 413。排队导出后继续改字并重排，PPTX 与使用本机 Chrome 的 PDF 均保持原快照页序及文字；回归修正了 renderer 配置测试对共享 Flask app 的污染。未分析的参考页现在真的以压缩后的图片作为模型视觉输入，而非只发送资产 ID；确认计划及调用前验证预览素材实际字节与冻结 hash，损坏时不派发付费请求；网页对视觉模型需求及可能费用做显式确认。大纲、生成和 AI 修改提交前也提示页数、模型及图片请求上界。浏览器在保存响应丢失后若服务器 Scene 已与草稿一致，会自动清除已应用的本地草稿，避免重复重放。候选接受及新页面写入在数据库支持时使用行锁，但 PostgreSQL 并发仍待正式实测。全量 `tsc --noEmit` 仍有 118 行旧代码类型错误，本次 Scene 文件未出现在错误列表中。上述结果不是 A01–A20 正式验收。

## 当前剩余代码与验收边界

- **TextLayout（§6.4）**：已补齐逐行 Unicode 范围、换行类型、字号、行框、基线、实际字体解析与有界私有缓存，见文末专项。最新 worker/PDF 校验及依赖变更尚需重新构建完整 Compose；派生 QA renderer 镜像不能替代三个正式 Dockerfile 的重新验收。
- **真实模型**：用户自有 Sub2API Key、文本/视觉/生图能力、真实费用及付费请求后的断连核查尚未执行；普通回归使用假 Provider。本轮真实模型调用为 0，不读取或复用用户现有凭据。
- **模型出网**：专用 QA Docker 未启用宿主 NAT/转发；内部 DNS、API/worker/PostgreSQL 和回环前端已通，但不能据此宣称模型网关网络可达。
- **视觉一致性**：逐页对照和人工报告确认已实跑；LibreOffice PPTX 有文字基线/行距差异，自动阈值未标定。33 页参考已静态转换并复核第 6/22/30 页，没有与原模板在 PowerPoint 中逐页对照，不承诺无损复制。
- **客户端与平台**：PowerPoint 16.0.20430.20092 的两页原生导出样本通过；WPS、arm64 尚未实测。
- **隔离强度**：只读、断网、五项 capability、逐作业 UID 和限额已按真实 renderer 容器验证；这是同一挂载命名空间下的 DAC 隔离，不是任意恶意 PPTX 的安全证明。孤儿作业/清理失败仍须按文档停机处理。
- **后续身份集成**：Sub2API iframe/SSO 是设计后续阶段，不在首版单所有者实现中冒充完成。

## 历史实施记录

以下各节的“尚未”仅描述该轮结束时的状态；后续实现与验证会关闭其中一部分缺口。

### 最近的模板入口加固（2026-10-02）

PPTX 上传前的静态包检查现拒绝重复/歧义路径、路径穿越、加密或符号链接条目、伪装成 `.pptx` 的宏及 ActiveX；限制关系文件和演示文稿 XML 大小、总解压量及压缩比。外部关系用 `defusedxml` 解析，避免单引号/空格绕过及实体展开；嵌入对象以 `EMBEDDED_OBJECT_STATIC_ONLY` 告警。页数直接从有界 `presentation.xml` 的 `sldIdLst` 获取，不再由 API 进程完整加载不可信 PPTX。指定样本的 SHA-256 仍为 `bb5e048aa4cf73898f8a603088d2e304beca5f7008b127a5026ebd6f378d8135`，静态检查得到 33 页和外链/嵌入对象两项告警。图片模板上传现按实际字节识别 PNG/JPEG/WebP，拒绝动态/超大图及伪装 GIF，并以实际 MIME 和后缀保存源文件。以上仍不是 LibreOffice 隔离转换验收。

正式 renderer 路径的 PPTX 导出现在先用同一 Scene HTML/固定字体在 Chromium 中测量文字自动折行，按 Unicode code point 记录软换行位置，再写为 PPTX 可编辑的 `<a:br>`；原始文本、显式段落换行及哈希均不改写，结构校验会核对软换行位置。无 renderer 且未指定本机 `SCENE_CHROMIUM_PATH` 的轻量离线开发仍只能使用旧的近似预检，不得据此宣称视觉一致；renderer 就绪烟测已把文字排版测量一并纳入 Chromium 检查。该链路在本机 Chrome 154 验证过中文/英文混排软换行、文字原样回读及 renderer 作业目录传输，但仍需固定容器和 PowerPoint/WPS 对照。

本轮以本机 Chrome 指定 `SCENE_CHROMIUM_PATH` 运行 Scene 集成及相关单测 131 项全部通过；前端 Scene 定向 32 项、Vite 构建、Scene 类型漂移检查与 `git diff --check` 通过。全量后端、全量 TypeScript 及 Linux/PostgreSQL/LibreOffice 的限制仍同上。

模型网关响应现用流式读取在解码后的字节达到上限时立即停止，不再先把任意体积的响应读入内存才检查长度；认证/余额/限流等可由响应头判定的拒绝不读取错误正文。付费请求在响应流断开或解码失败时归为结果未知，不自动重试。新增 MockTransport 回归覆盖超限早停、断连、畸形压缩、拒绝响应零正文读取及正常 usage/request ID。带本机 Chrome 的相关后端回归现为 135 项通过；真实 Sub2API 计费与断连行为仍须单独验收。

维护窗口资产审计现还核对每份 Scene 不可变版本的资源引用索引与资产 owner/project，并检查导出所引用的快照、文件、报告及已发布导出的报告哈希和逐页视觉证据。离线篡改回归能抓到多余的 revision 资源引用、错误审核哈希及被替换的视觉证据 ID；审计仍只读，不自动删除历史或孤儿文件。Linux PostgreSQL 备份恢复演练依旧待完成。

生成任务和 AI 修改任务提交现按项目→目标页的稳定顺序锁定数据库行，冻结计划/Scene 基版本后才入队；并拒绝非对象的生成 targets、不可哈希的对象 ID/参考页索引，而不是在请求处理中报 500。十页项目的离线回归确认仅为选中的第四页建立生成任务，手工编辑该页后以旧基版本再次提交返回 409，未新增付费任务。相关后端回归为 138 项通过；SQLite 控制路径不能替代正式 PostgreSQL 并发交错实测。

手工 Scene 保存/恢复及批量候选接受现也在项目归档锁之后、页面锁之前重新检查项目状态，避免在初次授权读取与提交之间被归档的项目继续发布版本。离线回归覆盖“已取得旧项目对象后项目被归档”时候选接受拒绝且候选保持 pending。当前相关后端回归为 139 项通过；真实 PostgreSQL 并发事务仍待验证。

固定字体质量门现用 `fontTools` 检查 Scene 原生文字每个 Unicode code point 是否存在于已打包字体的 cmap；生成候选预检和导出前会阻断缺字，创建快照时也立即返回 `FONT_GLYPH_UNAVAILABLE` 与缺失码点，不再让浏览器悄悄使用系统回退字体。回归证实中文、英文、数字、★、™ 可用，未覆盖的 😀 在快照前被拒绝。Scene 输入进一步拒绝制表符及不成对代理码点，避免导出排版歧义或编码 500。相关后端回归现为 142 项通过；这仍不替代目标 PowerPoint/WPS 字体安装环境检查。

导出面板现通过 `GET /api/v2/font-manifest` 显示固定字体内部族名 **Noto Sans CJK SC**、样式、SHA-256 和 OpenType 文件下载入口；PPTX 的原生文字对象改用该内部族名，而非与打包字体不匹配的 `Noto Sans SC`，并同时写入 DrawingML 的 `a:latin` 与 `a:ea`，避免中文被客户端主题字体接管。接收文件的 PowerPoint/WPS 客户端需先安装该字体并按质量报告中的 `font_sha256` 核对版本；PPTX 本身不嵌入字体。报告同时记录字体 manifest ID 与族名。后端相关回归 143 项、前端 Scene 定向 34 项、Vite 构建、类型漂移与 `git diff --check` 通过；尚未在实际 PowerPoint/WPS 客户端验证换行及安装效果。

导出面板现在可明确勾选单页或有效页子集，按项目页序冻结；所选页列表保存在会话存储，刷新后同一选择仍可重放快照/导出的幂等请求。未选页不会被悄悄混入，也不会因该页缺字阻断显式子集；所选其他页若存在未确认本地草稿则阻断，提示先切换该页完成保存。提交导出、生成或 AI 修改前会等待当前页文本与命令队列保存完成；保存失败、版本冲突、中文 IME 组合输入或未结束的拖动不会继续创建快照或付费任务。离线回归覆盖等待保存响应后才冻结新 revision、失败保存阻断、AI 修改使用新 revision、选择恢复与子集请求键，以及两页项目只导出第二页为单页 PPTX/PDF（第一页面含不支持的字形）。本轮相关后端 145 项、前端 Scene 定向 40 项通过；Vite 构建、相关 ESLint、类型生成漂移和 `git diff --check` 通过。全量 TypeScript 仍有旧代码错误，Scene 文件未出现在错误列表中；正式容器与客户端验收限制不变。

PPTX 质量门不再只检查 ZIP CRC、页数和文字内容：现逐框核对原生文本的字体族与 `a:ea`、字号/字重/颜色、段落对齐/行距、文本框坐标/边距、自动缩字关闭；逐张核对图片/背景的素材派生字节、位置、旋转及裁剪。另检查 OPC 关系来源与目标存在、ID 唯一，拒绝输出包中的外部关系。篡改 East Asian 字体、字号、自动缩字、行距、文字位置、图片裁剪/旋转/像素或关系目标的回归均会被阻断。带本机 Chrome 的相关后端回归 **147 项通过**；这些结构性检查仍不能代替 LibreOffice/PowerPoint/WPS 的人工视觉验收。

导出确认面板新增只读 `GET /api/v2/projects/{id}/snapshots/preflight?page_ids=...`：按所选页检查当前服务器 head 的文字溢出、固定字体字形、素材可用性与候选状态，并逐页列出阻断项；未选页不参与，预检不创建快照。选择或页版本变化时丢弃旧响应，避免上一组页面的阻断结果误用于当前选择。预检只是提示：本地未保存草稿、并发更改或检查后素材损坏仍可能使正式提交失败；`POST /snapshots` 会重新按客户端版本和实际素材字节进行权威校验。页切换、复制/插入、撤销、候选接受和版本恢复也会避开未结束的拖动与中文 IME 组合输入，防止把编辑中的本地状态丢在旧页。带本机 Chrome 的相关后端 **148 项通过**；前端 Scene 定向测试 **43 项通过**、Vite 构建、相关 ESLint、类型生成漂移检查及 `git diff --check` 通过。全量 TypeScript 仍有旧代码错误；Linux/PostgreSQL/LibreOffice、真实模型计费与 PowerPoint/WPS 验收仍未完成。

### 三路并行实现后的集成复核

后端 Scene 流程阻断了局部 AI 修改对未选对象的层序越界重排，并将畸形 add/reorder 命令稳定返回 422；模板分析避免同一文档重复派发，人工风格修订不会被在途分析覆盖，失败分析后也可通过人工修订恢复文档状态。不同参考页的分析完成时按模板行、文档行顺序加锁并汇总状态，避免最后完成的并发任务仍留下 `analyzing`。前端模板面板展示导入状态、页数及已记录的转换告警，支持刷新和参考页大图；逐页预览后可原子批量接受候选，未保存草稿会阻断。导出质量门增加 PPTX 画幅、背景、文本旋转和对象层序校验；卷归档先验证整棵树，失败时删除不完整归档。

主线程在合并工作区状态后重跑带本机 Chrome 的相关后端 **153 通过、1 跳过**（Windows 无符号链接创建权限），前端 8 个定向测试文件 **45 项通过**；Vite 构建、相关 ESLint、Scene 类型生成检查与 Git diff 格式检查通过。全量 TypeScript 仍有约 118 行旧代码诊断，本轮 Scene/模板文件未出现在诊断中。上述只是本地回归：模板字体替换告警尚无可靠的 renderer→导入记录端到端来源；Linux Docker/PostgreSQL/LibreOffice、指定 33 页模板、真实模型计费及 PowerPoint/WPS 客户端验收仍未完成。

后续补齐了保守的模板字体**可用性风险**告警链路：隔离 renderer 只扫描 PPTX 幻灯片 XML 中显式写出的 `a:latin`/`a:ea`/`a:cs` 字体族，与该容器的 `fc-list` 字体库比较；缺失时报 `FONT_FAMILY_UNAVAILABLE`，检查不可用时报 `FONT_AVAILABILITY_UNVERIFIED`。告警经 renderer 结果、模板导入记录和网页面板传递，并限制为已知代码。它**不判断**主题继承字体，也不声称已证明 LibreOffice 实际替换、换行差异或 PowerPoint/WPS 显示效果；这些仍须在固定容器/客户端人工复核。指定 33 页样本的静态扫描发现 15 个显式字体族，但当前 Windows 主机无 LibreOffice/Docker，未执行该样本的真实隔离转换。候选预览/历史读取失败现在会在网页显示错误而不是留下未处理 Promise。带本机 Chrome 的相关后端回归更新为 **156 通过、1 跳过**，前端 8 个定向文件 **46 项通过**，Vite 构建及相关 ESLint 通过。


### 锁定操作的撤销闭环与剩余代码审计

设计 §8.3 要求锁定动作的撤销使用显式 `set_locked`，而不能绕过历史恢复的锁保护。编辑器现在为纯锁定动作保存反向命令；锁定/解锁均能撤销、重做，并继续生成不可变版本。其他手工编辑与 AI 接受仍走受锁校验的 revision 恢复。锁定前先等待当前文字草稿保存，避免正文尚未保存就被锁住；历史请求在途时串行化撤销/重做，并阻止新编辑、切页或创建导出/生成任务覆盖该请求结果。前端回归覆盖锁定与解锁两个方向、连续点击去重、在途编辑阻断、文字保存完成后再锁；后端回归确认普通 restore 仍拒绝隐式解锁，显式 undo/redo 命令保留原文字和历史版本。相关后端 **157 通过、1 跳过**，前端 9 个定向文件 **51 项通过**；Vite 构建、相关 ESLint 与 Scene 类型筛查通过，全量 TypeScript 的旧诊断仍为 118 行。

继续按设计核对发现仍有**代码缺口**，不能将剩余工作仅表述为外部环境验收：

- §7.5 的参考模板软删除：已在下节补齐 API、网页入口、绑定版本校验和在途分析保护；保留历史素材，不作为释放存储空间的手段。
- §15.2 的容量策略：已有页数/上传/解压限额、模型并发、磁盘就绪阈值，队列容量与所有者存储配额已在下节补齐；仍需可执行的保留/清理策略。后续实现不得用按时间直接删文件替代完整引用检查。

尚余的容量/清理代码缺口与已记录的 Linux/PostgreSQL/LibreOffice、指定模板、真实计费和客户端视觉验收共同构成未完成项。

### 参考页软删除（2026-10-02）

新增幂等 `DELETE /api/v2/projects/{project_id}/template-assets/{template_asset_id}`。请求携带 `base_project_version`、`base_analysis_revision` 和所有当前绑定页的 `expected_pages`（page_id/page_version/revision_id）；任一绑定页在确认后发生变化即返回 409，不会部分解除绑定。成功时仅设置参考页 `deleted_at`、解除活跃页面绑定并递增页面/项目版本，保留页面 Scene head、用户补充说明、来源文档、预览/缩略图、历史计划及快照。列表隐藏该参考页，新绑定、分析、人工修订及分析任务重试均拒绝；已冻结计划内的素材不会物理删除。同键重放可取回原结果。

同一来源文档仍有 queued/running 分析任务时返回 `ANALYSIS_IN_PROGRESS`，要求先完成或取消；不偷偷取消可能已经计费的请求。分析提交改为项目→稳定顺序的参考页→文档加锁，和分析发布/人工修订的参考页→文档顺序一致，避免先锁文档再更新参考页的反序。删除与新分析/重试通过项目锁串行化；worker 在模型派发前及发布前都拒绝已删除参考，迟到失败也不再改写已删除记录或覆盖人工风格修订。SQLite 回归只验证控制路径，PostgreSQL 真实锁交错仍待正式验证。

网页“移除参考页”先显示将解除的绑定页数，并说明历史保留及不会释放磁盘。未保存正文、中文 IME、拖动、历史操作及未保存风格说明会阻断移除；请求期间防重复提交。移除后刷新若遇到新草稿或页切换，不覆盖当前编辑。新增测试覆盖跨 owner、版本冲突、逐页并发编辑、同键重放、多页原子解绑、历史引用/签名素材可读、排队与在途分析拒绝、删除后重试拒绝、派发前/调用后删除防御以及前端确认/失败/在途 IME 草稿保护。

本轮带本机 Chrome 的相关后端回归 **164 通过、1 跳过**（Windows 符号链接权限），前端 9 个定向文件 **57 项通过**；Vite 构建、相关 ESLint、类型生成漂移检查及 staged/unstaged diff 格式检查通过。全量 TypeScript 仍有 **118 行旧诊断**，Scene/模板文件未出现在诊断中。队列容量、owner 存储配额、安全保留/清理策略仍待实现；正式容器、真实模型计费和客户端视觉验收限制不变。


### 队列容量与派生任务预留（2026-10-02）

新增 `SCENE_QUEUE_CAPACITY_UNITS=1024` 和 `OWNER_QUEUE_CAPACITY_UNITS=256`，均必须为正整数，零不是关闭限制。Compose 与示例环境变量已接入，readiness 新增 `queue_config` 检查；队列满只阻止新工作，不把正在正常处理的服务判为不可用。当前排队/运行/waiting_assets 任务计入占用，历史终态不计入。大纲、导出、模板导入/分析、模型能力试调用和单独图片重试各占 1 单位；尚无持久 DraftScene 的生成或 AI 修改每页预留 7 单位（主任务 + 最多 6 个后续图片任务），与模型输出验证使用同一图片数量上界。草稿和子任务一次提交后，父任务改占 1 单位、实际图片任务各占 1 单位，剩余预留自动释放。因此后台 DAG 展开不会增大总占用，不会因其他请求占满队列而丢弃已经付费的文字结果。

所有 API 入队及失败重试先校验幂等记录，再以项目锁/所需业务锁→全局准入锁的顺序，在与任务写入相同事务内核对 owner/global 总额。项目锁使用 PostgreSQL `FOR NO KEY UPDATE`，仍阻止项目规划/归档交错，但允许 worker 对同项目插入外键引用；重试只锁仍为 failed/outcome_unknown 的任务，防止两个不同请求键把已开始执行的任务重新排队。PostgreSQL 准入使用独立的双整数事务 advisory lock；SQLite 开发使用不改变记录的空 UPDATE 获取数据库写保留锁，再读取合计。批量不足时整体返回 HTTP 429 `QUEUE_CAPACITY_EXCEEDED`，没有部分任务、导出记录或模板源文件；已完成的同键请求可在满额时重放原响应。取消 running 工作仅设置取消请求，在真正结束前仍计入容量。worker 不持有准入锁，也不在模型网络调用期间锁住全局队列。

受访问码保护的 `GET /api/v2/queue-capacity` 与编辑器“队列容量”折叠面板展示账户预留、实际任务数和系统剩余单位，支持手工刷新；只是查询时的提示，不预先承诺下一次提交成功，也不会自动重试付费任务。回归覆盖全部七类入队、批量原子拒绝、同键重放、重试/取消、跨 owner 全局总额、SQLite 两连接并发、无图/六图预留转换、拒绝第七个图片请求、在途子任务取消仍占容量及非法配置 fail-closed。**PostgreSQL 的真实并发锁交错仍未实测**，不以 SQLite 结果替代。

剩余代码项仍包括 owner 存储配额、任务/候选/文件的可执行安全保留与清理策略；本次容量控制不限制已经完成的历史数据，也不删除资产。正式容器、模型计费与客户端视觉验收要求不变。

本轮带本机 Chrome 的相关后端回归 **183 通过、1 跳过**，前端 10 个定向文件 **60 项通过**；Vite 构建、相关 ESLint、类型生成漂移检查及 staged/unstaged diff 格式检查通过。全量 TypeScript 仍有 **118 行旧诊断**，新增队列面板与 Scene/模板代码没有诊断。


### 私有素材卷存储配额（2026-10-02）

新增 `SCENE_STORAGE_QUOTA_BYTES=21474836480`（全卷 20 GiB）、`OWNER_STORAGE_QUOTA_BYTES=5368709120`（owner 5 GiB）、`SCENE_STORAGE_LOCK_SECONDS=10`、`SCENE_STORAGE_MAX_ENTRIES=200000`，Compose 与环境变量示例已接入；这些参数必须为正整数，`SCENE_MIN_FREE_BYTES` 须为非负整数。readiness 新增 `storage_config` 校验，并拒绝链接/重解析点形式的素材根目录。超配额只阻止新增素材，不阻止读取既有 Scene 和签名素材。

所有 `put_bytes` / `put_image`（上传、参考源/预览、生成图片/草稿、导出文件/报告/视觉证据）统一经私有卷跨进程文件锁执行“扫描实际文件→检查 owner/global 配额及磁盘安全余量→临时文件写入/fsync→原子发布”。按实际内容字节计数，不只统计数据库 ready 行，因此历史版本/软删除项目、尚未提交的写入、staging 与崩溃/回滚孤儿都会继续占用。锁和 readiness 临时探针不算素材字节；文件及目录总数有扫描上限。扫描不跟随符号链接、Windows 重解析点或硬链接；不支持的卷布局返回结构化错误而非跳过后放行。文件锁内部不访问数据库，不跨模型网络请求持锁；正式服务需共享同一支持 OS 文件锁的本地素材卷，不能把它当作跨主机对象存储的硬配额实现。

请求超过配额返回 HTTP 507 `STORAGE_QUOTA_EXCEEDED`，磁盘安全余量不足返回 `STORAGE_DISK_LOW`，不写入新的素材记录或不完整正文文件。磁盘写入失败只清理本次尚未发布的 staging 文件；如果文件已发布但后续数据库回滚，它仍计入占用，留给安全清理流程，不猜测其是否可删除。模型生成/修改/配图/大纲在派发前检查当前是否已满，已满时不会派发上游；此检查不是空间预留，模型返回后仍可能因并发写入而超限。迟到的配额错误保留 acknowledged/可能已计费状态，任务不会静默重复调用。

受访问码保护的 `GET /api/v2/storage-capacity` 与编辑器“素材存储”面板展示实际字节、配额和文件数，明确历史/失败遗留占用及软删除不释放空间。回归覆盖回滚遗留、staging、跨 owner 全局合计、满额上传不产生部分记录、扩容后同键重试、归档后仍占用、磁盘安全余量、rename 失败清理、文件数上限、非法路径/硬链接/重解析点、配置 fail-closed、派发前零调用和调用后可能计费。另启动两个真实 Windows Python 进程，通过同步起跑和有意延长锁内扫描验证同卷并发只有一份 80 字节写入能通过 100 字节限额。Linux 容器文件锁及崩溃/备份恢复演练仍待正式验收。

这是私有素材文件的应用层内容字节配额，不包括 PostgreSQL 数据、日志、renderer job/tmp 或文件系统元数据，也不能约束绕过应用的外部写入者；部署仍需为这些卷设置硬容量和监控。**当时安全保留/清理代码尚未实现，现已在下节补齐离线工具**；仍不得直接按时间删除文件或只看当前 head，正式使用前须完成容器维护演练。

本轮带本机 Chrome 的相关后端回归 **201 通过、1 跳过**；readiness 根目录检查调整后，相关 6 项再次通过。前端 11 个定向文件 **63 项通过**；Vite 构建、相关 ESLint、类型生成漂移检查与 staged/unstaged diff 格式检查通过。全量 TypeScript 仍为 **118 行旧诊断**，新存储面板与 Scene/模板文件没有诊断。以上不代表 Linux/PostgreSQL/LibreOffice 或真实计费/客户端验收完成。


### 安全保留与可恢复清理（2026-10-02）

新增迁移 `02f8479a3c61`、`services.scene.retention` 引用规划器、`ops.scene_retention` 离线 CLI 与 `scripts/scene-retention.sh`。**不是在线定时 GC，不会因为软删除自动回收项目历史**。默认保留期：`SCENE_RETENTION_CANDIDATE_DAYS=30`、`SCENE_RETENTION_TASK_DAYS=90`、`SCENE_RETENTION_ATTEMPT_DAYS=180`、`SCENE_RETENTION_ASSET_DAYS=30`、`SCENE_RETENTION_ORPHAN_HOURS=24`，只接受 1～36500 的整数；Config、Compose、环境变量示例及 readiness 已接入。

清理规划从真实数据库、完整素材文件清单和 JSON 草稿/导出报告构建引用图，而不是只查询当前 head：

- 当前/软删除页面、所有手工及已接受的历史版本、历史 GenerationPlan、DeckSnapshot、参考模板及来源文档、导出记录都是保护根。快照关联表、不可变 manifest、签名 URL、报告内逐页视觉证据和实际外键都会保护被引用资产。
- 只有超期 rejected/stale 或基版本已实际漂移的 pending 候选可能回收；其 AI proposed revision 也必须没有其他根引用。未过期幂等响应及其任务组会保护对应候选、任务与素材。
- 任务和 attempt 使用独立保留期。任何 queued/running/waiting_assets 任务存在时整个清理被拒绝；`outcome_unknown`、`may_have_been_sent` 或 attempt 留有未知结果的任务及同组成员不回收，不靠清理掩盖可能计费状态。
- 派生资产必须超期且不可达；未引用的用户 upload 也须等到项目归档超过宽限期。规范路径下无人引用的 orphan/staging 文件按文件 mtime 宽限；年轻文件会反过来保护旧 Asset 行。未知布局文件一律保留。
- 每份 ready Asset 的实际大小/hash、Scene hash 和 revision 资源引用索引必须正确；不跟随符号链接、Windows 重解析点或硬链接。自定义 GC 表触发器、未经支持的 PostgreSQL 多应用 schema 均拒绝执行，需先审计适配，不猜测级联副作用。

操作顺序（Linux Docker 主机）：

1. 阻断入口和外部写入者，让任务结束或显式取消；对结果未知的付费任务先查账，不自动重试。先用上文备份工具生成经验证的完整备份，密钥另存。
2. 准备素材卷**以外**的私有维护目录，运行 plan。包装脚本停止 frontend/backend/worker/renderer，保留 postgres，并确认没有外部数据库客户端。成功或失败均**不自动重启**服务。
3. 审阅 JSON 中的 records、文件列表、保留策略和 byte_size；用 plan 中的完整 SHA-256 明确确认 apply。服务必须在两步之间保持停止；数据库或素材有漂移就重新生成和审阅计划，不能手改 hash 强行执行。

```sh
mkdir -m 700 /secure/scene-maintenance
SCENE_MAINTENANCE_CONFIRMED=yes bash scripts/scene-retention.sh plan /secure/scene-maintenance/plan.json
# 审阅 plan.json；将下面占位符替换为其 plan_sha256。
SCENE_MAINTENANCE_CONFIRMED=yes bash scripts/scene-retention.sh apply /secure/scene-maintenance/plan.json REVIEWED_SHA256 /secure/scene-maintenance/receipt.json
```

apply 绑定数据库身份（SQLite 实际文件 inode；PostgreSQL 集群 system identifier、数据库 OID 与地址）、Alembic 版本和素材根目录身份，使用原 as_of/策略重新计算整份计划。PostgreSQL 使用有界 lock_timeout 和应用表独占锁；维护账号需查询 `pg_control_system()` 的权限（默认 Compose 管理账号具备）。SQLite 仅供明确离线的本机维护，启用外键并用 `BEGIN EXCLUSIVE`；这**不声称检测到了所有 SQLite 读连接**。两个阶段都遵循先数据库锁、后同一素材卷文件锁的顺序，不能与运行中的服务混用。

先按依赖顺序删除过期幂等记录、attempt、候选、任务、revision 引用/版本和 Asset，并将完整计划与 `db_pruned` journal **在同一 DB 事务中提交**。提交前没有文件删除。随后重新加锁，核对剩余数据库和全部保留文件的指纹，对待删文件逐一复核规范路径、大小、mtime、SHA-256 后 unlink；只尝试非递归移除已空的单个 asset 目录，不删除 owner/project 目录，不触碰 legacy uploads 或 renderer job/tmp。完成后将 journal 标为 `completed` 并输出私有 receipt。

若数据库提交后进程中断，保持停机，查询未完成 journal（不会输出 Key），然后用记录中的 run ID 恢复；resume **只相信数据库已提交的 plan_json**，不接受外部替换计划。已删除的目标文件可跳过，保留文件、剩余目标或数据库有变化则拒绝继续。新计划遇到未完成 journal 会拒绝，readiness 的 `maintenance` 检查也返回 false。不要手工删除 journal 或改状态绕过；迁移降级在存在任何 journal 时也拒绝销毁恢复证据。

```sh
docker compose -f docker-compose.scene.yml exec -T postgres psql -U banana_scene -d banana_scene -c "SELECT id, plan_sha256, status, created_at FROM scene_maintenance_runs WHERE status <> 'completed';"
SCENE_MAINTENANCE_CONFIRMED=yes bash scripts/scene-retention.sh resume RUN_ID /secure/scene-maintenance/resumed-receipt.json
# 仅确认 completed、审计通过后手动启动服务，并等待 readiness 返回 200。
docker compose -f docker-compose.scene.yml start backend renderer worker frontend
```

本机离线维护可在 `backend` 目录使用 `python -m ops.scene_retention --offline-confirmed plan --output ...`，通过环境变量提供 DATABASE_URL / ASSET_STORE_ROOT；apply/resume 参数与包装脚本一致（CLI 使用 `--plan`、`--confirm-sha256`、`--run-id`、`--receipt`）。CLI 默认拒绝未确认维护；计划及 receipt 必须在素材根目录外，输出不覆盖已有文件。此工具的 hash 是防误操作/漂移校验，不是抵抗有权限修改数据库与计划的恶意管理员的数字签名。数据库或素材根被恢复/搬迁后，旧计划身份校验可能不通过；不要强改计划，应使用清理前备份恢复一致状态再制定新的维护方案。

Windows 真实临时 SQLite 回归已覆盖候选回收、current head/快照/历史计划/模板/视觉证据保护、任务组与付费未知结果、独立 attempt 保留、外键发现、硬链接与损坏拒绝、数据库/文件/身份/Schema 漂移、FK 保持开启的父子删除、事务回滚零删文件、提交后中断与 resume、CLI 三步闭环、未确认/已有输出拒绝和 readiness。PostgreSQL 的真实独占锁、Linux 文件持久化及 Docker 停机/恢复流程仍待验收；本机结果不能代替这些验证。


本轮整合回归（2026-10-03）：指定本机 Chrome 的相关后端 **237 通过、1 跳过**（Windows 符号链接权限）；其中新增 retention 的 **36 项**涵盖真实 Alembic 迁移表结构、禁止有 journal 时降级和不可变 manifest 校验；另外 Alembic CLI 原有 4 项测试通过。前端 11 个定向文件 **63 项通过**，Vite 构建、相关 ESLint、类型生成漂移检查、Shell 语法检查及 staged/unstaged diff 检查通过。全量 TypeScript 仍为 **118 行旧诊断**，Scene/模板及容量面板无诊断。脚本命令显式使用 `bash`，不依赖 Windows Git 工作区提供 Unix 可执行位。尚未证明 Linux/PostgreSQL/LibreOffice 的正式运行、指定模板实转换、真实模型费用及 PowerPoint/WPS 客户端验收；总目标仍未标记完成。


### 真实 PostgreSQL 迁移与并发回归（2026-10-03）

当前主机虽无 Docker，但 WSL Ubuntu 24.04 可用。已在专用、全新的 WSL 临时目录运行 Linux **PostgreSQL 16.2**（`pgserver==0.1.4` 提供二进制），Windows Python 3.12 通过回环端口连接进行真实数据库测试；未安装系统数据库服务、未连接现有业务库、未调用任何模型。此环境不是正式 Compose 的 PostgreSQL 16.6 镜像，也没有完成 Docker/LibreOffice 或 Linux 文件卷验收。

实测发现并修复了新部署的阻断问题：历史迁移 `019_per_page_template` 将 `user_edited_analysis BOOLEAN` 的默认值及已有模板回填值写成整数 `0`，PostgreSQL 会报 `DatatypeMismatch`，因此先前仅通过 SQLite 的迁移测试不能证明正式数据库能初始化。现在默认值使用 `sa.false()`，回填使用绑定的 Python `False`；SQLite 仍得到相同的 0/false 语义。修改既有迁移是为了让新 PostgreSQL 数据库能够走完迁移链，不增加新的数据改写迁移，也不重新执行已升级实例的回填。新增测试同时验证 PostgreSQL 和 SQLite 的已有模板、绑定说明、legacy 模式及原文件字节保持不变。

新增 `backend/tests/integration/test_scene_postgres.py`，必须显式提供独立测试服务器的 `SCENE_TEST_POSTGRES_URL`；普通 CI 未设置时跳过，不把 SQLite 当成替代。测试账号需要 CREATEDB，以及清理身份校验所需的 `pg_control_system()` 查询权限；每项测试生成随机 `scene_test_<uuid>` 数据库，从空库执行真实 Alembic 迁移，并仅在结束时删除自己创建的数据库。**不要指向生产服务器**；测试不会清空 URL 指定的维护数据库，但会在其服务器上创建临时库。

```sh
SCENE_TEST_POSTGRES_URL='postgresql+psycopg://TEST_USER:TEST_PASSWORD@127.0.0.1:5432/postgres' \
  python -m pytest backend/tests/integration/test_scene_postgres.py -q
```

该组测试不只是在 PostgreSQL 上重跑顺序操作：并发保存、相同幂等键和快照冻结场景先让第一个请求持有业务锁，再通过 `pg_stat_activity` 观察第二个真实 backend 已进入 Lock 等待，释放后核对 head/版本、409 和不可变 manifest。其他覆盖包括：

- 相同幂等键并发返回同一 task；不同输入复用键在 advisory lock 等待后返回 409，不插入第二份任务。
- 两个不同项目同时到达队列准入，owner 仅剩 1 单位时恰好一项成功、一项 429；JSONB DraftScene 发布及六个图片子任务展开仍保持 7 个预留单位。
- API 的项目 `FOR NO KEY UPDATE` 锁尚未释放时，worker 对同项目插入外键引用可以完成提交，不因锁模式过强形成阻塞。
- 一个连接锁住前 50 个排队任务，另一个 worker 的 `SKIP LOCKED` 实际领取第 51 个；两个 worker 同时竞争相同 owner 的 Principal 行锁时，只有一个可分配受限的付费执行槽。
- 已 acknowledged 的付费调用租约过期转为 `outcome_unknown`，不新增 attempt；旧 fence 无法续租或发布。
- 冻结快照期间并发编辑等待；完成后的新 head 不改写快照中的原始两页版本。
- 清理命令确实拒绝仍有其他 PostgreSQL 客户端的维护；无其他连接时，计划/记录删除/journal 提交、模拟删文件前中断、resume 均在真实 PostgreSQL 上执行。

这关闭了“PostgreSQL 代码路径完全未实测”的缺口，但不是 A20 的完整 Linux Compose 备份恢复验收。该轮按设计核对还发现了部署依赖锁和 §15.3 观测代码缺口；它们已在后续两节实现并补充测试。不能把剩余工作统称为外部环境问题，或把已有测试全部通过当成 M0～M3 已全部验收。


本轮结果：真实 PostgreSQL 专项 **13 项通过**；其中 7 项关键并发用例又连续重跑两轮，均通过。与原 Scene/容量/清理回归联合执行为 **250 通过、1 跳过**（Windows 符号链接权限）；额外迁移回归 **7 项通过**。移除 PostgreSQL 显式环境变量后，该专项 **13 项全部按预期跳过**。类型生成与 staged/unstaged diff 检查通过；本轮未修改前端，前端验证沿用上一轮结果。所有随机测试数据库均已删除，独立 PostgreSQL 进程已正常停止；WSL 的依赖与空测试集群留在专用临时目录，不成为生产依赖，不修改项目锁文件。完整目标保持进行中。

### Scene 运行时锁、构建输入与引擎恢复保护（2026-10-03）

设计 §15.1 的部署依赖代码缺口现已补齐：Scene Compose 改用独立的后端/前端 Dockerfile，并固定 PostgreSQL 与五个构建/渲染基础镜像的 digest。`deployment/scene/runtime.lock.json` 记录 amd64/arm64 manifest、APT 时间快照和 InRelease hash、uv/npm/wheel 锁、浏览器/字体/小样本校验值；离线验证器检查漂移但不自动改锁。后端只安装已锁定 wheel，不构建项目或浮动 build-system；前端与 renderer 使用 `npm ci --ignore-scripts`；renderer 改用预装固定 Chromium 的官方镜像，并增加独立的最小 npm/wheel 锁。构建期实际启动浏览器核对版本、运行原有独立 UID 的中文与 LibreOffice 小样本自检；这些命令已经接入，但**当前尚未实际执行完整 Docker build**。

补齐三个 Dockerfile-specific 构建白名单，既修复根 `.dockerignore` 的 `*.pptx` 导致 renderer 缺少 `smoke.pptx`，也排除 `.env`、`backend/instance/`、上传/私有素材、数据库、日志、依赖缓存和 QA 临时目录。使用 Docker/Moby 自身固定版本的 patternmatcher 验证排除规则与必需输入，不拿 gitignore 的不同语义替代。Linux 入口由 `.gitattributes` 固定 LF。worker 不监听 HTTP，Compose 关闭继承的 API HTTP healthcheck，继续由 API readiness 验证其数据库心跳。

构建期新增不含业务数据/环境变量的实测引擎清单（包含自研编译/转换源码 hash，防止仅比较第三方包而漏过算法变更）：Python、dpkg、Node/npm、nginx、LibreOffice、Playwright/Chromium 等。备份纳入这些清单、完整运行时锁和实际 PostgreSQL image ID/版本，并比较已有容器与维护临时容器，防止重新打 tag 后把不同引擎混在同一次备份。恢复在任何数据库写入前验证清单；不同引擎、架构、checkout/镜像锁或缺失清单一律拒绝。**旧备份没有运行时清单时不自动兼容**，必须保留旧环境，另做受控迁移与验收；不提供忽略引擎差异的开关。操作与升级步骤见 `deployment/scene/README.md`。

独立 WSL Ubuntu / x86_64 / Python 3.12.3 临时环境已实际执行后端 `uv sync --locked --no-dev --no-install-project --no-build`，安装全部 **139 个生产依赖**，核心 Flask/psycopg/PPTX/PyMuPDF/加密模块导入通过；renderer 的两个 hash 锁 wheel 也完成真实安装。另以经过官方 SHA256 校验的 Node **22.23.3** / npm **10.9.9**，在独立临时目录执行前端和 renderer 的全新 `npm ci --ignore-scripts`，Vite 网页构建通过。没有替换工作区 Windows 虚拟环境或 node_modules，没有改宿主服务/业务库，没有调用付费模型。这个 Python 3.12.3 测试不是目标 Python 3.12.15 镜像的替代证明；arm64 尚未实机验证。

部署专项测试在 Windows 可执行部分通过，并在独立 Linux 环境 **46 项全部通过**，包括真实 Bash/sha256sum、缺失/篡改/多余 APT index、输入锁漂移、浏览器版本拒绝、维护容器差异、旧备份拒绝、正确恢复分支和写库前阻断。维护测试使用记录调用顺序的假 Docker CLI，仅证明脚本合同，不声称已完成真实卷恢复。Moby 构建上下文 **3 组测试（9 个组件子用例）通过**。

该轮结束时 §15.3 的关联日志和分阶段指标尚未完成，现已在下一节补齐。正式 Compose 构建、受限 renderer 实运行、指定 33 页模板转换及第 6/22/30 页复核、真实模型计费、PowerPoint/WPS 客户端视觉验收仍未完成；上述安装与单测不等于 M0～M3 全部验收。

本轮最终联合后端回归 **276 通过、30 跳过**：13 项 PostgreSQL opt-in 测试因本轮没有运行中的专用测试库而跳过（上轮实测记录仍保留），16 项 Linux 专用测试随后已在 WSL 的 **46 项部署专项测试**中全部通过，另 1 项为 Windows 符号链接权限限制。独立 Linux 前端 11 个测试文件 **63 项通过**、Vite 网页构建通过；类型生成漂移检查、输入锁验证、Moby 上下文测试与 staged/unstaged diff 格式检查通过。本轮没有修改前端业务代码，没有将 Vite 构建等同于全量 TypeScript 类型检查。


### 结构化关联日志、指标与真实代理验证（2026-10-03）

已实现设计 §15.3 的代码合同，管理员命令、字段解释、隐私边界和告警要求详见 `deployment/scene/README.md` 的“关联日志与运维指标”。新增 migration `13e79b401ca8`（上游 `02f8479a3c61`），增加 task/attempt 请求关联、当前阶段入队时间/queue_wait 和有限标签指标聚合表。SQLite 与真实 PostgreSQL 均验证了旧任务回填：以 task UUID 合成关联，不谎称恢复历史 HTTP 请求，旧等待时长保持空值。

- API envelope 与 `X-Request-ID` 使用同一服务端 UUID，不接受客户端伪造关联；签名 JSON 文件下载不会被 after_request 当作 API body 解析。最初入队 ID 保持不变，手动重试记录新 ID；真实生成 DAG、图片子任务、候选 finalize 和导出 snapshot 贯通到 worker/renderer 日志。
- API/worker/renderer 只输出固定字段和规范 UUID；不记录 prompt、模型 Key、认证头、query、完整 URL、原始异常/traceback、provider request ID 或外部路径。Scene HTTP 未处理异常转换为安全 JSON；旧启动配置日志也不再输出模型地址和值。
- 阶段包括模型文本/生图、素材写入、候选校验/发布、模板导入/分析、快照验证、文字测量、PPTX/PDF 编译/质检、渲染排队及视觉对照。任务成功/unknown、人工/自动/租约重试、真正的版本冲突、渲染失败和不同失败来源分别统计。大纲和模板导入的成功 attempt 补齐 `finished_at`。
- ORM 事务 hook 只在业务提交后发布状态，rollback 丢弃；hook/日志/指标异常隔离于业务。指标延迟到业务 session 释放连接/锁后，以独立短事务 atomic upsert。真实 PostgreSQL 上验证了两进程合计 40 次更新、被锁指标行的有界放弃，以及后续业务提交仍成功；这是尽力而为的运维统计，不能作为精确计费账本。排队等待不包含 backoff，本地 DAG finalize 不误记为自动重试。
- `python -m ops.scene_metrics` 只读导出 Prometheus 文本，不启动应用、不自动迁移、不新增跨 owner 指标 API；包含实际磁盘/素材占用和当前任务 gauge。`--output` 原子替换；非法连接 URL、DB/写入/替换失败都返回固定错误，保留上份文件。因此运维必须检查退出状态和文件 mtime，不能把旧 `scrape_success 1` 当作当前健康。
- Scene 镜像独立 Nginx 配置不记录原始 query/header/referrer，并有意关闭会嵌入原始签名请求的内建 error log。该诊断取舍及外层代理责任已明确记录；输入锁包含新配置，renderer engine manifest 包含 `observability.py`。

验证结果：

1. 指定本机 Chrome，联合 Scene/任务/导出/安全/配额/清理/迁移/部署/观测回归 **321 通过、17 跳过**。其中 **17 项真实 PostgreSQL** 全部执行并通过，覆盖最新迁移与旧 trace 回填、并发锁、HTTP→worker 关联、多进程指标和锁超时。跳过为 16 项 Linux 专用测试及 1 项 Windows 符号链接权限；不是跳过 PostgreSQL 冒充实测。最后补充 asset_write 阶段、commit hook 失败隔离测试后，定向回归另为 **89 通过、17 跳过**（16 项 Linux 专用及 1 项 opt-in Nginx）。随后补充日志输出故障不得打印 fallback traceback 的测试，最终观测专项 **29 项通过**。
2. 独立 WSL 中 **46 项部署专项全部通过**，Moby 构建上下文仍 **3 组/9 子用例通过**；验证器及类型生成无漂移，staged/unstaged diff 格式检查通过。
3. 从锁定 nginx amd64 manifest `sha256:3b03dfd827515a07ccf4946000f0bbdd2a4fb6aca7499249fcb6b1f9726823d6` 获取并逐层校验 SHA-256，只提取 nginx 二进制在独立 WSL 临时目录运行。**nginx 1.28.0** 的真实 `nginx -t` 和回环 HTTP 测试通过：200/500 保持上游 ID，编码 URI 正常解析，上传 413 与断开的上游 502 也不泄漏 query/Key/Cookie/referrer/响应正文；测试进程正常停止。配置仅替换测试端口、文件目录和 stub 上游，不改日志格式/路由。使用 WSL 系统共享库，未据此宣称正式 nginx 镜像通过。
4. 独立 Linux Node 22.23.3/npm 10.9.9 环境再次执行前端定向 **63 项通过**，Vite 网页构建通过。前端业务代码本轮未改；Vite 不等于全量 TypeScript，旧诊断记录仍适用。

独立 PostgreSQL 16.2 的随机测试数据库已全部清理，服务已正常停止；没有连接现有业务库、修改系统服务、改用平台模型 Key 或执行付费调用。完整 Compose build/只读断网 renderer/一致性备份恢复、指定 33 页 PPTX 静态转换及第 6/22/30 页复核、真实模型收费合同、PowerPoint/WPS 实客户端仍未验收；目标继续保持进行中。


### 真实容器全栈与备份恢复验收（2026-10-03）

**范围与环境**：三个 subagent 分别负责后端 Scene/API/任务、前端画布/模板、导出/运维；主线程整合并执行跨模块验证。本轮在 WSL Ubuntu 24.04 amd64 的专用 Docker **28.5.2**、Compose **2.40.3**、Buildx **0.29.1** 上实际构建全部镜像，PostgreSQL 使用锁定 **16.6**。未安装系统服务，未改宿主防火墙/转发；构建临时使用受限 CONNECT relay，TLS/GPG/hash 校验保持开启。该 QA 网络不是生产网关可达性的证明。

#### 实测暴露并修复的问题

- renderer 的临时目录原先先 `chown` 后 `chmod`，在没有 FOWNER 的正式能力集合中失败；改为先设权限再移交 UID，不增加 capability。
- Docker 后端禁用 Flask debugger/reloader，启动日志不再输出数据库连接串或上传目录。
- 独立 renderer 模式不再错误要求 backend 镜像存在本地文字测量 JS；本地 fallback 仍检查脚本。
- PostgreSQL 模板 preview/thumb Asset 必须先 flush，再插入引用它们的模板页，避免真实外键失败。
- Werkzeug 彩色 access 行先剥离 ANSI escape 再脱敏，防止 404 行中的签名 query 绕过匹配。
- 字体单测使用 monkeypatch 恢复共享 app 配置，避免后续 Windows 测试误走 Linux UID 模式。
- 新文字对象的首次保存尚未返回时，快速输入/blur/debounce 使用排队 `add_element` 的文字为基线，后续文字不再丢失。
- 相同属性值的 blur 不生成无效保存请求，防止点击 Undo 被正在保存状态吞掉。新增测试另修正 Testing Library 的 `ByRoleOptions` 类型用法。

#### 已执行的端到端路径

1. **Compose/API**：原服务定义和隔离限制启动；readiness 全 true，匿名 API 401。创建两页、上传图片、手工版本保存、旧基版本 409。快照冻结后继续修改，PPTX/PDF 仍保留原始快照内容。
2. **导出**：真实 worker→renderer 产出两页原生文字/图片 PPTX 和有文字层 PDF；PDFium 中文提取与 960×540 pt 画幅检查通过。逐页对照先为 `needs_review`，错误报告 hash 返回 409；查看四张对照图并明确确认 `VISUAL_DIFF_UNCALIBRATED` 后才为 `succeeded`。PDF 内容布局一致；LibreOffice PPTX 存在文字基线/行距差异，未见缺字、裁切或对象丢失，不宣称像素一致。
3. **参考模板**：原始指定 PPTX 经 API→PostgreSQL→worker→断网 LibreOffice→私有资产库产生 **33/33 张 1080×608** 预览；原文件 SHA-256 保持 `bb5e048aa4cf73898f8a603088d2e304beca5f7008b127a5026ebd6f378d8135`。实际查看第 6/22/30 页；字体缺失、外链忽略、嵌入对象静态化告警可见。模板没有执行模型风格分析。
4. **浏览器**：全新 Chromium **143.0.7499.4**，没有 mock 业务接口；登录→建项目→新增文字立即输入→保存刷新→再次编辑→撤销刷新→UI 上传模板→33 张缩略图全部加载及字体告警可见。无 page error。
5. **文件与日志**：有效短时单资源签名是下载授权，不要求文件请求重复提交访问码；无签名或篡改签名返回 404。检查 API/worker/renderer/frontend 日志，QA secrets 和签名 query 未泄漏。
6. **PowerPoint**：客户端 **16.0.20430.20092** 打开本工具导出的两页静态样本，原生文字修改、图片移动 12 pt、另存并重开断言通过；启用正常警告，无需修复或手工确认对话框。实际检查原始两页及修改后截图；中文/英文/数字可读，无明显裁切。固定 Noto Sans CJK SC 字体仅临时会话注册并在 finally 移除。此样本不是 33 页原模板的原生对照，WPS 未测。
7. **备份恢复**：原仓库 `scene-backup.sh` / `scene-restore.sh` 在真实 Compose 执行，生成数据库 dump、卷归档、运行时清单、校验和及 COMPLETE；新 Compose project、新空卷与**不同 PostgreSQL system identifier** 的空集群恢复成功。审计覆盖 **143 个资产、19 个 Scene revision、1 个 snapshot、2 个已审核 export、1 条可解密的假模型凭据**；generation plan 为 0。恢复后 readiness 全 true，当前/历史 Scene、两份导出原文件及 33 张参考图 hash 相同。无真实模型凭据或付费任务。

#### 当前回归结果

| 检查 | 结果与边界 |
| --- | --- |
| Windows Scene/相关后端联合 | **284 passed / 39 skipped**；不是旧项目全部测试通过 |
| PostgreSQL 16.6 实容器 | **19 passed**，随机测试库已清理；包含模板资产 FK 专项 |
| Linux 部署 / Moby ignore | **46 passed**；**3 组 / 9 子用例通过** |
| 正式限制 renderer 容器合同 | **2 passed**，包含指定模板；不是恶意样本安全认证 |
| 前端 11 个定向文件 | **66 passed**；Vite build 与定向 ESLint 通过 |
| 全量 TypeScript | 仍失败，**118 行原有诊断**；Scene/模板及本轮新增测试无诊断 |
| 运行时锁 / 类型生成 / diff 格式 | 验证通过；未自动修改依赖锁 |

Windows 跳过项为 19 个 PostgreSQL opt-in、16 个 Linux 专用、2 个 Docker renderer opt-in、1 个 Nginx opt-in 和 1 个符号链接权限用例；前三组已分别真实执行，Nginx 专项在前轮实跑，Windows 符号链接限制仍在。不要把它们当作未执行却通过，也不要将不同环境计数简单相加。

可复跑入口：`backend/tests/integration/test_scene_postgres.py`、`backend/tests/integration/test_scene_container_runtime.py`、`backend/tests/integration/test_scene_proxy_logging.py`；均需显式独立环境。容器合同测试需要 Linux root、`SCENE_TEST_RENDERER_IMAGE`、`SCENE_TEST_DOCKER_DAEMON_ID` 和 `DOCKER_HOST`，仅允许预建镜像，不拉取、不使用默认 daemon、不复用已有卷。

**证据与保留**：本机 `tmp/scene-docker-qa/verification.json` 汇总实际 image ID、源/恢复集群身份、归档审计、hash、测试日志及关闭状态；`compose-artifacts/`、`browser-artifacts/`、`powerpoint-qa-v2/` 保存产物和截图。这些位于 Git 忽略的 QA 目录，不随代码分发，也不含可复用真实模型 Key。专用 WSL 备份位于 `/var/tmp/banana-scene-docker-23k50xtn/verified-backup-v1`，其 `SHA256SUMS` 本身的 SHA-256 为 `d36a261e2629be8171b134eff2bb202ea3cedf96f3ffe998bfafb467544a4adc`。备份未包含凭据加密主密钥，QA 私有环境另存；不能将该假数据部署直接作为生产配置。

本轮关闭临时源栈、恢复栈、专用 daemon 和构建 relay，保留镜像、数据卷、备份和证据，不 prune、不删除用户文件。完整目标仍进行中：真实模型能力/收费及其出网、WPS、视觉阈值标定尚未验收。


### 大纲候选、原规划版本与事实来源合同（2026-10-03）

继续按 §4.3 / §10.1 核对发现，早期大纲虽然没有由 worker 直接改写 pages，但前端会在 AI 返回时直接替换编辑表单；接收端没有返回原规划版本，且把 facts_needed 拼进 points，20 条正文加待补事实会突破普通要点上限。该问题是代码缺口，不是待授权模型调用能自动解决的问题。

本轮补齐：

- 新增 `backend/services/scene/outline.py` 的严格 OutlinePage/OutlineDraft 校验；title、role、points、facts_needed、sources 有独立类型/数量/长度边界，拒绝未知字段、控制字符与 surrogate。三组列表分别最多 20 条，每条最多 1000 字；无 sources 的早期模型草稿兼容为空，旧手工 title/points 形状不被无声扩充。
- 模型草稿资产 provenance 永久保存 `base_project_version`；获取草稿回传原版本。早期资产只从原 task 恢复该值；任务已被清理或来源未知则返回 null，不拿当前项目版本冒充原基线。worker 仍不更新 head/pages。手工确认保留角色/待补事实/来源，生成计划完整冻结这些字段；模板自动匹配优先采用用户已确认角色。
- 提取独立 `OutlineEditor`，AI 返回只出现比较面板，不能覆盖正在输入的表单。载入时重新读项目；过期或未知基线要求比较服务器版本并明确确认。保存始终使用打开/确认时的基线，409 保留草稿并展示最新对照；二次确认前又发生变化则重新要求比较，不悄悄推进版本。增删/排序页面先留在本地，最后一次版本校验保存，不再在表单中立即删除服务器页面。
- 刷新后可从成功的大纲任务打开候选，不重新发起模型调用；窗口在模型配置响应前关闭会阻止后续派发，已提交的任务则继续保留在任务列表。关闭窗口不暗中取消可能计费的请求。
- 来源只是未核验的纯文本归属信息，不自动访问其中地址，不等于事实校验；待补信息不被偷偷填成模型编造的数字。界面将三类信息分开编辑，旧模板文案仍不得成为业务来源。

验证包括假 Provider 下真实 API/数据库/worker 的 **1 页和 10 页**大纲、原版本冻结、过期 409、确认后 plan 元数据、历史资产回退、无效模型结果失败且不自动重试；新增大纲专项 **20 项**。前端包含在途输入、旧/未知基线、比较后服务器再次变化、独立元数据、本地增删、关闭时不派发、刷新找回候选等测试。

真实 Chrome 的桌面 1280×900 与窄屏 390×844 验证了鼠标比较、草稿保留、显式按新版本继续编辑及保存；界面沿用现有黄色操作色和 Noto 字体，修正窄屏正文与固定操作区的挤压。**这轮浏览器用隔离 Axios 夹具，不是新源码的 Compose 或真实模型验收**；产物在 `tmp/scene-outline-qa/`，与上一轮真实全栈证据分开保存。

联合回归还暴露观测测试的不正确假设：两个 SQLite 进程在忙碌主机上的 250 ms 有界锁等待会按既有 best-effort 合同丢弃一批指标，旧测试却要求所有 40 次写入必达。生产代码未放宽时限或自动重试；测试改为核对实际已报告丢弃与成功写入总和，成功部分仍必须原子累加，并新增真实写锁持有时丢弃一批、恢复 busy_timeout、不写入指标的专项。不能把运维指标当成 exactly-once 计费账本。

本轮业务代码尚未重新构建 Docker 镜像；上一节 image ID/备份证明的是上一轮快照，不能扩展为新大纲代码的容器验证。TextLayout 派生合同与真实模型等项仍待完成，目标保持进行中。

本轮最终验证：带本机 Chrome 的相关后端 **208 项全部通过**（含大纲专项 20 项），前端 **12 文件 / 75 项通过**，Vite build、Scene ESLint、运行时输入锁、Schema 类型漂移与 staged/unstaged diff 检查通过；全量 TypeScript 仍有 **118 行旧诊断**，本轮 Scene 文件无诊断。初次并发指标测试失败及后续修正按上段解释，不以简单重跑掩盖。证据与源码 hash 在 `tmp/scene-outline-qa/verification.json`。本轮临时 Vite 与浏览器均已关闭，无付费调用。


### 完整 TextLayout、缓存与原生导出合同（2026-10-03）

本轮补齐 §6.4，不再仅返回软换行数组：

- `layout_contract.py` 定义严格 TextLayout v1：逐行源文字 code point `[start,end)`、`hard/soft/end`、字号、页面坐标系 pt 行框与基线；hard 行后的原始 LF 由映射消耗，末尾/连续 LF 和空文字也有真实空段落行框。没有复制一份可编辑文本，没有改写 Scene/hash。
- Chromium 以 `Intl.Segmenter` 遍历完整字素簇；DOM Range 的 UTF-16 索引显式转 code point。独立同样式探针测 baseline，不在原文插标记，也不把 glyph rect 冒充 CSS line box。CDP 查询实际 shaping 字体，记录 family/PostScript/custom/glyph count；服务端拒绝非固定字体回退。
- React 与导出 HTML 都显式保留硬段落和空行，避免 flex 匿名空白文本被丢弃。空页也显式加载字体。PPTX 保留原生文字，以已测软换行、固定 pt 行距和边距导出；行距按 OOXML 1/100 pt 精度量化，关闭自动缩字及额外自动折行，空段落也设置字体。
- 新依赖 **`regex==2025.11.3`** 独立验证 UAX #29 字素边界；Unicode 数据与 Chromium 不一致时失败关闭，不拆开组合字符。只更新该依赖及其 wheel/hash 锁，未升级其他包。引擎清单新增 `layout_contract.py`，相应审查更新 `pyproject.toml`、`uv.lock` 与 verifier 三项输入摘要；新的运行时锁指纹为 `fa46ccde2013164dd3a15b0ec2242b1a8b5e23778bce21820ab4ecee789b0f24`。部署清单维持 LF；维护脚本的精确字节比较未放宽。
- worker 内存 LRU 最多 **64 项/32 MiB，300 秒过期**；按 app/owner/project 隔离，键包含有序 Scene hash、完整字体 manifest hash，以及 Chromium/Node/Node ICU/Playwright/脚本/HTML 编译器/合同/regex 的共同指纹。返回反序列化副本，没有未纳入 quota/retention 的磁盘缓存。查缓存前必须确认当前引擎身份与字体；隔离状态必须新鲜、健康且满足 UID 模式，旧 renderer 协议不再通过 readiness。测量结果身份与查找身份不一致时拒绝缓存。
- PPTX **和 PDF** worker 均先测量，失败时不再走“未测但可人工放行”的出口。完整布局及输入指纹写入受 owner 授权保护的导出质量报告；PDF 同源 HTML 渲染时复核引擎身份。结构/文字失败仍不可被视觉确认豁免。

真实测试发现，PDFium 对部分 Chromium PDF 会丢失空格/拆碎提取顺序，而 MuPDF 对另一些合法 CJK Type3 PDF 会返回 U+FFFD。现在要求 **PDFium 或 MuPDF 中的同一个提取器**完整通过全页字符计数和每对象区域内的源文本检查，报告记录实际使用者；不拼接两者的部分成功，不删除空格、不做 Unicode 归一化、不用 Scene 文案补造提取结果。补充漏/多空格、组合字符归一化、错位与既有 Type3 CJK 回归。

#### 验证与证据边界

- 本机 Chrome **154.0.8037.59**：相关后端 **245 passed / 16 skipped**，包含新布局专项 **44 项**、真实中英/数字/astral/组合字符/粗体/空文字/空白/连续末尾 LF/三种垂直对齐、PPTX 原生结构及 PDF 文字验证。16 项 Linux 维护测试随后在 WSL 实际执行。
- 独立 Linux：部署/权限/布局测试 **85 passed / 7 skipped**；7 项本机 Chrome opt-in 已在 Windows 执行。真实断网、只读根、独立 UID renderer 合同 **2 passed / 1 skipped**，新增一页六文本框测试证明测量→服务端严格校验→第二次缓存命中→PPTX/PDF；日志只有一次 `text_layout` 作业和一次 `pdf` 作业。跳过的是本轮未重跑的 33 页参考模板，历史证据不冒充本轮实测。
- Linux renderer 使用 Chromium **143.0.7499.4**、Playwright **1.57.0**、Node **24.11.1**。测试镜像 `sha256:dbd2783fb630e133202ffa9b1e2f76db5fd8df49d1415562e1de35282ddf8451` 是在此前已验证依赖镜像之上、断网覆盖当前 renderer 代码并重新自检的 **QA 派生镜像**；不是重新执行三个原始 Dockerfile，也不是最新完整 Compose/备份验收。首次自检暴露 smoke 容器自动高度过小而误判字体 ink overflow，已给烟测明确框高；生产溢出检查未放宽。
- PowerPoint **16.0.20430.20092**：打开同一六文本框 PPTX，原始文字（包括空白/硬换行/astral/组合字符）一致、自动折行关闭、原生文字编辑→另存→重开通过，原文件未改变。实际查看 PPTX 与 PDF 图片：断行/对象位置一致，无明显裁切或缺字；Office 原生粗体与 Chromium 合成粗体有可见差异，不宣称像素一致，视觉结果仍需审核。临时字体在 finally 移除，未修改用户已有文档。
- 前端 **12 文件/76 项通过**，Vite build 与 Scene ESLint 通过。全量 TypeScript 仍有 **118 行既有诊断**，Scene 文件无新诊断；类型生成、运行时锁和 staged/unstaged diff 格式检查通过。

本机证据在 `tmp/scene-layout-qa/verification.json`、`container-artifacts/`、`powerpoint-artifacts/` 和各测试日志；与旧 Compose 证据分开。临时 renderer 容器、专用 Docker daemon、PowerPoint 均已关闭，原镜像/卷/备份保留；无真实模型调用。目标继续进行：最新全栈重新构建/验收、真实模型授权与计费/出网、WPS 和视觉阈值标定尚未完成。


### 当前源码全栈复验与导出角色报告（2026-10-03）

本轮补齐 §12.4 的逐对象角色明细：新增 `services/exports/report.py`，PPTX/PDF 统一使用同一 page inventory；无效 role 或与快照不符的 Scene hash 拒绝生成报告。字体告警区分接收端要求与检测引擎实测，不把“未嵌入字体”冒充已发生字体替换。原有图片背景测试错误复用了修改前的快照 hash，已改成独立内存快照，没有放宽生产完整性校验。

#### 当前源码与真实路径

- 冻结 Moby 白名单构建输入为 `source-v12`，使用仓库原始三个 Dockerfile 和原 Compose 定义；没有以 QA 派生 renderer 代替当前镜像。backend / renderer / frontend image ID 分别为 `e603408b8363…` / `62e25a5fd576…` / `2812fb991d42…`，完整值、源码/产物摘要及构建日志保存在下述证据目录。运行时锁指纹仍为 `fa46ccde2013164dd3a15b0ec2242b1a8b5e23778bce21820ab4ecee789b0f24`。
- 新空卷源栈中真实运行授权 API→PostgreSQL 16.6→worker→独立 UID/断网/只读根 renderer；保存、旧版本 409、冻结快照后继续编辑、PPTX/PDF 生成及逐页视觉证据均通过。报告包含完整 TextLayout 和角色明细；无签名/篡改签名下载拒绝，日志没有 QA secrets 或签名 query。
- 查看两页 PPTX/PDF 的四张对照图后，错误报告 hash 被拒绝，精确 hash 加明确视觉告警确认才发布。PDF 内容布局一致；LibreOffice PPTX 仍有轻微字形/基线差异，不宣称像素一致，也不把这个正常样本作为严重截断检测已完备的证明。
- 原指定模板 hash 不变，当前镜像实际转换 **33/33 页**；重新查看第 **6/22/30 页**，内容可读，字体替代/外链忽略/静态化告警可见。未调用模型做风格分析。
- 浏览器 Chromium **143.0.7499.4** 使用真实 Compose、无 mock API：登录、创建、即时输入、保存刷新、修改与撤销刷新；两标签页大纲 409 保留草稿并显式按新版本继续；role/facts_needed/sources 持久化；本地从 1 增到 10 页只在确认保存时写库，未保存的删除不写库；33 张模板缩略图全部加载。无 page error。
- PowerPoint **16.0.20430.20092** 再次打开本轮 Compose 导出的 PPTX；原生文字修改、图片移动 12 pt、另存/重开通过，正常警告开启且无需处理修复对话框。实际查看原始两页和修改后页面，无明显裁切或对象丢失；临时字体会话注册已移除，原文件未修改。
- 原备份/恢复脚本生成新 `verified-backup-final-v1`，恢复到不同 Compose project、空数据卷和不同 PostgreSQL system identifier。审计 **143 个资产、19 个 revision、1 个 snapshot、2 个 export、1 个假模型凭据**，generation plan 为 0。恢复后 readiness 全 true；当前/历史 Scene、导出文件、完整报告（含 TextLayout/roles）和 33 张参考图的字节/hash 均一致。凭据可解密性由维护审计检查；没有真实付费 attempt。

#### 回归与失败记录

| 检查 | 本轮结果 |
| --- | --- |
| Windows + 本机 Chrome，Scene 相关后端联合 | **330 passed / 17 skipped**；16 项 Linux 专用 + 1 项 Windows 符号链接权限 |
| 正式 backend 镜像 Python 3.12.15 / PostgreSQL 16.6 | **19 passed**，随机测试库已清理 |
| Linux 部署/权限/布局/报告/归档 | **90 passed / 7 skipped**；7 项本机 Chrome opt-in 在 Windows 已执行 |
| 正式 renderer 镜像合同 | **3 passed**，包含 33 页模板、逐 UID 读隔离、真实 TextLayout 缓存与 PPTX/PDF |
| 前端定向 | **12 文件 / 76 passed**；Vite build、ESLint 通过 |
| 全量 TypeScript | **118 行既有诊断**；本轮 Scene/模板文件无诊断，不声称全仓库通过 |
| Moby 构建上下文、输入锁、类型生成及 diff | **3 组 / 9 子用例通过**；其他检查通过 |

旧 WSL Python **3.12.3** 辅助环境的 PostgreSQL 测试在第八项 GC 期间发生段错误，失败日志保留；随后在正式 backend 的 **3.12.15** 镜像中只挂入测试及纯 Python pytest 依赖，19 项全部通过。未禁用 GC、未降级驱动，也未据此宣称已查明旧辅助环境的崩溃根因；崩溃遗留的随机测试库记录身份后清理。首次联合测试因 QA 环境变量名称错误跳过本机 Chrome，修正为 `SCENE_CHROMIUM_PATH` 后完整执行，最终计数以表格为准。

恢复辅助流程第一次漏启动 PostgreSQL，被维护脚本在写入前拒绝；按原文档启动 PostgreSQL 后恢复成功。Compose `up --wait` 会因非 HTTP worker 无 healthcheck 返回失败，但服务实际已启动；因此检查真实容器状态及受认证 readiness，而不是给 worker 增加虚假的 HTTP 检查或宣称失败命令成功。

本机证据目录：`tmp/scene-final-qa/verification.json` 与 `compose-artifacts/`、`browser-artifacts/`、`container-artifacts/`、`powerpoint-qa-v2/`。新备份为 `/var/tmp/banana-scene-docker-23k50xtn/verified-backup-final-v1`，其 `SHA256SUMS` 文件 SHA-256 为 `f94946e1ed67cae3f1e83b7f65129c0c842135820f105d6d4b316acf0e2991fa`；旧备份不覆盖。QA 资产位于 Git 忽略目录，不随代码分发；生产密钥须单独配置与备份。

本轮无真实模型调用。完整目标继续进行：真实模型能力/计费与网关出网需要测试配置及授权，WPS 未测；视觉指标仍仅作描述。后续需针对目标引擎严重重排/截断建立不可豁免的确定性检查/回归，不能仅依靠 `VISUAL_DIFF_UNCALIBRATED` 的普通人工确认补足 §13。

本轮临时源栈/恢复栈已关闭；确认专用 daemon 无活跃容器后，daemon 与 CONNECT relay 均正常 exit 0。保留全部镜像、数据卷、两轮独立备份和证据；不 prune、不提交或重置用户改动。


### 目标 PPTX 实际渲染硬检查（2026-10-03）

新增 `services/exports/rendered_text.py`。PPTX 的目标对照先由隔离 LibreOffice 生成一次 PDF，对这份实际输出硬检查，再从**同一份 PDF**生成视觉对照；用户下载的 PDF 仍为 Chromium 的同 Scene 原生文字层出口。

- MuPDF drawing trace 按绘制顺序与冻结 Scene 逐 code point 对照，只消耗源 LF，保留真实空格，不做 Unicode 归一化或 OCR 补文。PDFium 独立映射可见字形的紧致边界，处理自动生成的位置空格及 UTF-16 surrogate pair；仅这个第二层 ink 映射排除空白，主检查从不删除真实空格。
- 每个冻结 TextLayout 行必须保持同一 baseline；相邻可见行的间距计入中间空段落，字号/字体不能静默替换，字形不得越过原文字框。实际 PDF clip path 裁掉文字也阻断，不以 trace 中仍有字符当作完整显示。
- 当前固定 LibreOffice 输出使用 page-level text 和矩形 axis-aligned clip；意外嵌套 Form 文字、非矩形裁剪、无法解析或两提取器映射不一致一律 `PPTX_RENDER_UNVERIFIED`，不是静默跳过。0.25 pt 只是序列化坐标 epsilon，不是 SSIM 或视觉自动通过阈值。普通字形/抗锯齿差异仍需人工看逐页对照；本检查不声称检测所有视觉遮挡。
- `PPTX_RENDER_TEXT_MISMATCH / PAGE_MISMATCH / CLIPPED / REFLOW / FONT_MISMATCH / UNVERIFIED` 均不可被 worker 降级为普通视觉告警。审核接口要求 v1 passed 的目标证明、合法 PDF SHA-256 和准确页映射；旧未审核报告缺少证明返回 422 `PPTX_RENDER_UNVERIFIED`，须重新导出，不能补写证明绕过。已审核历史文件不追溯撤销。
- 报告 `visual.rendered_text_layout` 记录目标 PDF hash、提取器、逐对象字符数/源行号/实际 baseline，不复制业务正文。readiness 新增 `pptx_pdf_ok`，旧 renderer 不满足就绪要求。

完整文字烟测暴露旧 `renderer/smoke.pptx` 主题默认字号导致实际裁切（源 `Scene readiness 中文 123` 只留下 `ne readiness 中文 1`）。修复样本为显式 Noto Sans CJK SC / 12 pt / 固定行距、边距与禁用 autofit/自动折行；没有放宽生产检查。运行时锁仅更新烟测摘要，依赖版本未升级；新指纹为 `3d1adc7eea51a7a1bdf5fbe4f75b10c7106a3dbc377a4c05d2a0bbc6d802fe83`。

可分发的真实 LibreOffice 六文本框样本为 `docs/fixtures/scene_pptx_libreoffice_text.pdf` 及同名 JSON，包含输入/输出 hash 与 Scene/TextLayout；PDF 已加精确 Git 忽略例外。新增 26 个专项用例，覆盖文字/空格/Unicode、换行/空段落、四边越框、实际 clip、字体/缩字、不可见文字、异常 PDF/页数/画幅、独立映射以及不支持结构的 fail-closed。

本轮专项结果：Windows 后端 **256 passed / 16 Linux-only skipped**；审核错误提示跟进 **6 passed**；Linux **116 passed / 7 本机 Chrome opt-in skipped**；隔离 renderer **3 passed**（含 33 页模板和新目标 PDF 检查）。前端本轮未改且未重跑。renderer `5ef444642e9e…` 为 QA 派生镜像，不是原始 Dockerfile 重建；初始构建标签错误、自检发现烟测裁切与审核缺少 `re` import 的失败均保留日志，修复后再验证。证据在 `tmp/scene-target-layout-qa/verification.json`。后续最新原始三个镜像、Compose 与新锁备份复验单独记录；旧备份不能忽略引擎差异强行恢复。真实模型、WPS、视觉阈值的限制不变。


### 当前硬检查版本的完整部署复验（source-v13，2026-10-03）

这轮使用 422 个 Moby 白名单输入、仓库原始 backend/frontend/renderer Dockerfile 和完整 Compose，**不是派生镜像**。实测全部构建输入与当前源码字节一致：

- backend：`sha256:d88491289e0daad7002007347e6a2d06c1db9fe0679aad7a168bab04463dd51b`
- renderer：`sha256:866051e1d22a1b0629c147430dd3ab2f52600a1f14516b603e43e753021931ab`
- frontend：`sha256:32b4241d69f925753037adf3a2b087f565c29a169b57d0d6b4ec84925adc1951`
- runtime lock：`3d1adc7eea51a7a1bdf5fbe4f75b10c7106a3dbc377a4c05d2a0bbc6d802fe83`

新 Compose 源栈与恢复栈的 readiness 全通过，包含 `pptx_pdf_ok`。实际 worker 导出的 PPTX 报告有 `visual.rendered_text_layout.status=passed`，逐页/对象映射和目标 PDF hash 完整；冻结后继续改字没有污染在途 PPTX/PDF。人工查看四张导出对照图，未见裁切/丢字；PPTX 字宽、字重与少量基线差异仍可见，不宣称像素一致。错误报告 hash 返回 409，绑定准确 hash 的明确确认才发布。

33/33 参考页重新转换，第 6/22/30 页实际查看，源 hash 未变。浏览器无 mock API 完成即时编辑/刷新/撤销、大纲两标签页 409 保留草稿与显式 rebase、role/facts_needed/sources 持久化、1→10 页原子保存、未保存删除不写库及全部缩略图加载。PowerPoint **16.0.20430.20092** 打开本轮文件，原生文字编辑、图片移动 12 pt、另存/重开通过；原始两页与修改页实际查看，无明显裁切/丢失，临时字体已移除，原始导出不变。

新备份 `/var/tmp/banana-scene-docker-23k50xtn/verified-backup-render-v1` 的 `SHA256SUMS` 文件 hash 为 `3dab762743641efb98e8a8fb27508425449a93b9f806c1e2dfd60044bea4a4d9`。恢复到不同 PostgreSQL system identifier 的全新空集群后，143 个资产、19 个 revision、1 个 snapshot、2 个 export、1 个假凭据，以及完整新报告/目标证明/33 张参考图保持原字节。generation plan 仍为 0，不代替真实生成任务恢复。旧 `verified-backup-final-v1` 在新运行时被明确拒绝，拒绝后 public 表数量仍为 0；没有忽略引擎差异或覆盖旧备份。

本轮独立计数（不与旧轮相加）：

| 检查 | 结果 |
| --- | --- |
| Windows Scene 相关后端联合（含 Chrome） | **366 passed / 17 skipped**：16 Linux 专用与 1 Windows 符号链接权限 |
| Linux 部署/权限/布局/目标文字/报告/归档 | **116 passed / 7 skipped**：7 本机 Chrome opt-in 已在 Windows 执行 |
| 原始正式 backend Python 3.12.15 + PostgreSQL 16.6 | **19 passed**，随机测试库已清理 |
| 原始正式 renderer 隔离合同 | **3 passed**，含真实 `pptx_pdf` 与 33 页参考模板 |
| 前端 | **12 文件 / 76 passed**；Vite build、Scene ESLint 通过 |
| 全量 TypeScript | **118 行既有诊断**；Scene/模板无新诊断，不宣称仓库类型检查通过 |
| 三组件输入锁、类型生成、staged/unstaged diff | 通过 |

证据：`tmp/scene-render-fullstack-qa/verification.json`、`source-v13.files.json` 和同目录构建/回归日志；`compose-artifacts/`、`browser-artifacts/`、`container-artifacts/`、`powerpoint-qa-v2/` 保存实际产物。QA 辅助 PDF 文案抽取曾输出未显式关闭辅助句柄的 finalizer 提示，生产 verifier 已使用上下文/finally 关闭；没有忽略失败断言。独立 WSL Docker 桥接未启用 IP forwarding，内部 API/数据库/renderer 已实测，但不以此证明模型出网可用。

#### 设计验收的证据等级

- **真实运行覆盖**：A01、A04、A08、A10、A11、A17、A20 的核心链路；A19 仅 PowerPoint，**WPS 未验证**。
- **有代码与离线/fake Provider 回归，尚非真实付费验收**：A02 的风格分析/模板文字隔离、A03 的选定页生成、A06/A07/A09 的 AI 锁/并发/候选撤销，以及 A13/A15/A16 的上游错误/计费未知/失败与取消。浏览器与 PostgreSQL 已验证其中非模型部分，不能据此宣称真实模型支持。
- **确定性回归**：A05 图片变换与历史、A12 字体/资产/布局阻断、A14 响应丢失幂等、A18 第二 owner 隔离；目标 PPTX 严重重排/实际裁剪已新增非豁免检查和实际 LibreOffice 样本，不把普通视觉确认作为替代。
- **仍需外部配置或人工标定**：专用真实网关/模型及授权费用上限、WPS、代表性样本视觉阈值。当前所有视觉对照仍人工审核，阈值未标定不作自动通过依据。完整目标保持进行中，以上测试不等于 M0～M3 全部验收。

本轮源栈、恢复栈、测试容器与 PowerPoint 均已关闭；检查专用 daemon 无活跃容器后，daemon 与 CONNECT relay 正常 exit 0。保留镜像、卷、三个独立备份及全部证据，不 prune，不提交或重置用户改动。


### 真实网关契约入口与恢复保护（2026-10-03）

按设计 §13 审计发现，此前有 fake Provider 状态机测试和 UI 能力检查，但缺少可单独显式开启的真实网关 pytest 入口。本轮新增 `scene_live` marker、入口、客户端执行器与 **40 个离线保护用例**，具体配置、调用边界和恢复流程见前文“显式开启真实模型契约测试”。

保护回归既有纯内存 HTTP，也有真实 Flask 路由/SQLite/worker 配合 fake Provider：证明三个模型步骤分别产生持久 attempt/usage，默认零网络；无预算确认/非法 endpoint/错误凭据网关不提交；重定向与敏感响应不外泄；幂等键先落盘再发请求；响应丢失和响应落盘失败后恢复同一任务；任务 unknown/timeout 不自动重试或继续付费；过期/损坏/并发记录拒绝；修改模型或已验证分析结果不冒充原结果；已完成重跑不更新原完成时间。没有复用这些离线结果冒充真实模型支持或账单。

最终验证：Windows 新专项加既有 Provider 流控 **44 passed / 1 skipped**；Linux 同一专项 **44 passed / 1 skipped**；带本机 Chrome 的 Scene 相关后端联合 **406 passed / 18 skipped**（16 Linux 专用、1 Windows 符号链接权限、1 未授权真实契约）。首次测试 fixture 误传 `ensure_owner` 参数及误从 `models.scene_v1` 导入 `Project` 导致失败，已修正测试连接方式，日志保留；未放宽业务断言。

本轮只改变测试与文档，已逐字复核 source-v13 的 **422 个生产构建输入全部未变**，因此没有重建相同镜像、重跑前端或重启 QA Docker 栈。运行时输入锁、类型生成及 staged/unstaged diff 检查通过。证据在 `tmp/scene-live-contract-qa/verification.json`。对 WPS 只做了已知 COM ProgID 和两个标准安装目录的存在性检查，均不存在；这不是对所有便携/用户安装目录的穷举，也不是 WPS 实测。

尚未获得真实网关测试配置和计费授权，本轮真实调用 **0 次**；WPS 与代表性视觉阈值标定仍未完成。完整目标继续进行，不将新增测试入口或默认 skip 算作真实验收通过。
