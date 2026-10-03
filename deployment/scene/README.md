# Scene 部署运行时锁

本目录实现设计 §15.1 的可复核依赖合同。依赖锁自身不是运行证明；2026-10-03 已另行完成 amd64 真实镜像构建、Compose、受限 renderer、PowerPoint 样本和新集群备份恢复，见下方“真实部署验证”。真实模型/WPS/arm64 等边界保持明确。

## 构建

从仓库根目录执行，先配置 `.env` 中 Compose 要求的密钥与连接信息：

```bash
python deployment/scene/verify_runtime.py verify
DOCKER_BUILDKIT=1 docker compose -f docker-compose.scene.yml build
# 启动前应先完成隔离测试/备份；不要直接升级已有业务数据。
docker compose -f docker-compose.scene.yml up -d
```

使用支持 Dockerfile-specific `.dockerignore` 的 BuildKit / Docker Compose v2。旧后端、前端 Dockerfile 保留不变；Scene Compose 使用 `backend/Dockerfile.scene`、`frontend/Dockerfile.scene` 和 `renderer/Dockerfile`。不要改回 legacy 构建目标。worker 不运行 HTTP 服务，因此显式关闭继承的 API `/health` 探针；它的可用性由 API readiness 的数据库心跳检查负责。

三个构建上下文均为独立白名单，拒绝 `.env`、`backend/instance/`、本地 DB、缓存、上传、`node_modules`、日志和 QA 临时目录。renderer 白名单明确包含固定字体和 `smoke.pptx`；后者在旧根 `.dockerignore` 中被 `*.pptx` 排除。根 ignore 也补充了私有状态排除，但不能替代 Scene 专用白名单。

## 锁定内容与失败行为

- `runtime.lock.json`：六个基础镜像的 index digest、amd64/arm64 manifest digest、APT 快照与 InRelease hash、依赖输入和字体/自检样本 hash。
- Python 3.12 / uv、Node 22、nginx、PostgreSQL 16.6、Playwright 1.57.0 镜像均用 digest 引用，不依赖可变 tag。amd64 已做独立 Linux wheel 安装验证；arm64 有镜像/依赖来源记录，未声称已实机运行。
- 后端执行 `uv sync --locked --no-dev --no-install-project --no-build`。缺锁、锁漂移或缺 wheel 都失败，不回退重新解析、不构建浮动 setuptools 依赖；运行时直接使用构建好的虚拟环境。
- 前端与 renderer 都执行 `npm ci --ignore-scripts`。renderer 独立最小 npm lock 只安装 Playwright / playwright-core（另有 Darwin 的 optional fsevents），不再安装整套前端依赖。安装路径仍是 `/app/frontend`，与现有渲染脚本解析路径一致。
- renderer 使用已含 Chromium 的官方固定镜像，不再执行 `playwright install`。构建时实际启动浏览器，校验 Playwright 1.57.0、revision 1200 和 Chromium 143.0.7499.4；随后以独立 UID 运行中文网页/PDF/文字测量和 LibreOffice 小样本转换。运行时仍按原有隔离规则再次自检；构建期通过不等于只读/断网/限额部署环境通过。
- renderer Python 包采用 hash lock，强制 `--require-hashes --only-binary=:all:`，只允许已记录的 Linux wheels。
- Debian / Ubuntu APT 使用 `20261002T000000Z` 快照，删除镜像自带的其他源和旧 index，再下载 index。保留官方 keyring 的 GPG 验证；`Check-Valid-Until: no` 只允许历史快照，不关闭签名验证。更新失败或 InRelease 集合/hash 不符立即终止，不退回最新源。
- 字体和 smoke PPTX 按原始字节计算 SHA-256；文本锁输入统一将 CRLF 规范化为 LF。Linux 构建/维护脚本另由 `.gitattributes` 固定 LF。

这锁定了依赖来源和安装选择，不承诺不同宿主、CPU、时间戳下 Docker image ID 或导出文件字节完全相同。

## 构建时实测清单和备份

API/worker、renderer 镜像生成 `/opt/scene/runtime-manifest.json`，记录锁指纹、CPU 架构、Python 包、自研编译/转换源码 hash、实际 dpkg 包版本；renderer 另记录实际 Node、LibreOffice 和 Chromium 版本。前端镜像的 `/opt/scene/runtime-manifest.txt` 记录锁指纹、Node/npm、实际 nginx 和 dpkg 版本。清单不读取环境变量、密钥、模型 URL 或业务数据。

原有维护窗口要求不变：先停止外部写入者，备份脚本暂停应用与 renderer，恢复只允许空数据库/空素材卷。

```bash
SCENE_MAINTENANCE_CONFIRMED=yes bash scripts/scene-backup.sh /safe/new-backup
SCENE_RESTORE_CONFIRM=restore-into-empty-stack bash scripts/scene-restore.sh /safe/new-backup
```

备份增加 `runtime/` 并纳入 `SHA256SUMS`：四个应用单元清单、完整运行时锁、实际 PostgreSQL image ID 和版本。采集时比较已有服务容器与维护临时容器，拒绝仅重新打 tag 而未重建旧容器的混合状态；Compose PostgreSQL 也必须使用固定 digest。`docker inspect` 仅取镜像字段，不导出可能包含密钥的完整配置。

恢复在任何数据库写入前逐项核对引擎与锁；引擎变化、架构不同、当前 checkout 与镜像锁不一致、缺清单都拒绝。旧备份没有引擎清单时不自动兼容，也没有 `ignore-runtime` 开关；需保留旧版本恢复环境，单独评审/执行迁移和重新验收。加密主密钥仍必须独立备份。

## 验证与升级

```bash
python -m pytest backend/tests/unit/test_scene_runtime_lock.py -q
# Linux shell/APT 哈希和假 Docker CLI 验证不等于真实 Docker 构建。
(cd deployment/scene/contextcheck && go test ./...)
```

Go 测试只用于开发验证，使用固定 `github.com/moby/patternmatcher` 及 go.sum，与 Docker 相同的 ignore 匹配器检查私有文件排除和必要输入纳入；Go 不进入应用镜像。

依赖升级必须一起评审并修改：镜像 digest、APT 快照/GPG index/hash、npm/uv/wheel 锁、浏览器版本、字体或 smoke 变更及 `runtime.lock.json` 对应 hash。验证器只检查，绝不自动重写锁来掩盖漂移。更新后重新构建、运行相关测试和正式隔离/视觉验收，再开始维护迁移；不要用 `latest`、放宽 hash、关闭 GPG 或启动时联网安装绕过失败。

早期独立 WSL 安装测试和离线维护测试不能替代部署验收；当前已追加下节的真实构建/运行证据。WPS、arm64、真实模型计费与模型出网仍未验证。


## 关联日志与运维指标

迁移到 `13e79b401ca8` 后，API 为每次 Scene HTTP 请求生成自己的 UUID，放入 JSON envelope 的 `request_id` 与 `X-Request-ID` 响应头；调用方同名请求头不被采信。签名文件响应仅附加头，不读取/解析私有文件内容。API/worker 的 `scene.telemetry` 和 renderer 的 `scene.renderer_job` 是单行 JSON，包含：

- `request_id`：本次请求或执行 attempt 的关联；`origin_request_id`：最初入队请求。手动 retry 保留原始 ID，新的 HTTP 请求 ID 写入 `retry_request_id`；自动 retry、图片子任务及本地 finalize 继承所属请求链。
- `project_id`、`page_id`、`snapshot_id`：适用时关联；`task_id`：有 group 时为 group UUID，否则为任务项 UUID；`item_id`：实际持久任务项 UUID。不是进程号，也不是模型 provider 的请求号。
- 固定的 `event/operation/stage/outcome/error_code/failure_source`，以及适用的 `duration_seconds/attempt_no/status/endpoint`。不记录输入内容、prompt、Key、Cookie、完整 URL、query、异常文本/traceback、外部路径或 provider request ID。非法关联 ID 不输出原值。

历史任务无法还原 HTTP 来源：迁移使用已有 task UUID 作为**合成** request_id，旧 attempt 继承它；旧排队时间留空，不捏造历史耗时。运行中的服务应先停止再迁移；不要同时运行不认识新字段的旧 worker。存在 task/attempt/指标证据时，迁移拒绝破坏性降级。

指标持久化在有界标签的 `scene_metrics` 聚合表，不存逐用户统计明细，也不通过公共 HTTP endpoint 暴露。管理员只读导出：

```bash
# 在已配置 DATABASE_URL / ASSET_STORE_ROOT 的 backend 目录运行；不会启动应用或执行迁移。
python -m ops.scene_metrics
python -m ops.scene_metrics --output /private/textfile/scene.prom
# 已运行的 Compose 可将结果发送到管理员 stdout；不向外开放指标端口。
docker compose -f docker-compose.scene.yml exec -T -w /app/backend backend /app/.venv/bin/python -m ops.scene_metrics
```

`--output` 的目录必须事先创建并限权，且在素材卷以外；给容器使用此选项时需由管理员显式配置独立输出挂载。输出采用同目录临时文件、fsync 和原子替换，可交给 node_exporter textfile collector；不要用 `> scene.prom` 直接覆盖有效快照。任何查询/扫描/配置/写入失败都退出非零、只给固定脱敏诊断，旧 `.prom` 保持不变。因此必须监控任务退出状态和 `node_textfile_mtime_seconds` 的新鲜度，不能只看旧文件中的 `scene_metrics_scrape_success 1`。资产扫描会短暂持有素材卷锁，建议低频执行（例如 60 秒），避免同时进行离线备份/清理维护。

输出包含 `scene_http_requests_total`、`scene_task_transitions_total`（成功、失败、unknown、取消分开）、`scene_retries_total`（手动/自动/过期租约恢复分开）、`scene_version_conflicts_total`、`scene_render_failures_total`，以及阶段/排队耗时 histogram、当前任务状态 gauge、实际素材文件 bytes/files（包括 orphan）和所在磁盘 free/used/total。等待时间从当前阶段入队且到达 next_run_at 后计时，不把退避或历史未知时间算成排队。素材字节数不包括 DB、日志和 renderer 作业卷；磁盘 gauge 对应素材根所在文件系统，不是所有卷总量。没有 owner/project/request 等高基数标签。

这些是 **best-effort 运维指标，不是计费账本或 exactly-once 证明**。状态日志只在业务 commit 后发布；rollback 不发“成功”，幂等重放不再记 retry。指标在业务锁释放后独立短事务 atomic upsert；PostgreSQL lock/statement timeout 为 250/500 ms，SQLite 临时 busy_timeout 为 250 ms 并恢复原设置。观测写入失败只丢弃本批，不能回滚业务、重发模型调用；进程崩溃或日志故障也可能漏计。收费核查应使用持久 attempt/dispatch/usage 和上游账单。

Scene 前端镜像改用 `frontend/nginx.scene.conf`，代理 JSON access log 只保留 method、无 query 的 URI、status、时间和上游 request ID；旧 nginx 配置不变。Werkzeug 也过滤 Scene 原始 access 行。Nginx 内建 error formatter 会带原始 request URI，无法用 log_format 安全改写，因此此 Scene 配置将该 error log 写到 `/dev/null`；运行期用脱敏 access 状态及 API/worker 关联事件排查，配置错误用 `nginx -t` 离线检查。这个取舍会失去 Nginx 详细错误描述，不应悄悄开启含原始签名 URL 的 error/debug 日志。外层 TLS 代理和日志采集器须执行同样的脱敏策略；本配置不能替它们保证安全。

新增 Nginx 配置及 Dockerfile/ignore 已纳入输入锁，renderer 日志代码纳入实测引擎源码清单。可选真实代理测试：在 Linux 设置 `SCENE_TEST_NGINX_PATH` 后运行 `python -m pytest backend/tests/integration/test_scene_proxy_logging.py -q`。该测试只启动自己的回环监听与临时配置，不改系统服务；未设置时跳过。当前验证使用从锁定 amd64 镜像中按 manifest/layer SHA-256 核验并提取的 nginx 1.28.0，连接的是 WSL 系统库，因此仍不是完整镜像构建/运行验收。


## 真实部署验证（2026-10-03）

在专用 WSL Ubuntu 24.04 amd64、Docker 28.5.2 / Compose 2.40.3 / Buildx 0.29.1 环境构建并启动原 Compose 服务定义；没有把开发模式或 host-network renderer 当成正式隔离验收。构建期使用 host network，运行期 renderer 为 `network_mode: none`、只读 rootfs、`no-new-privileges`、`cap_drop: ALL` 加 CHOWN/DAC_OVERRIDE/KILL/SETGID/SETUID、3 GiB 内存与 128 PID 限额；逐作业不重用 UID 保持启用。未启用宿主 NAT/转发，内部 DNS/服务通信和回环前端可用，**模型出网未测**。

实测 image ID（仅本轮产物标识，不承诺重建字节相同）：

| 镜像 | SHA-256 image ID |
| --- | --- |
| backend / worker | `d77f88ce015f719c657ec371543d2dfd43aed71bd659f33aa0d4f7e926a02278` |
| frontend | `435512a4d23831534682920a366f970325598db1e83312aa77555c724d647a47` |
| renderer | `3971ea7f809a95e39fe03b269cd3086c899cc9208c461105b10cd3efbe6905f1` |

运行时输入锁指纹为 `232a6e77a73f2100da2d1153d8557021602b6861116fcc6f8fbca7e650430bfb`。PostgreSQL 仍使用原固定 16.6 digest。实际构建解决了 renderer 无 FOWNER 的 chmod/chown 顺序问题；backend Docker 禁用 debugger/reloader；独立 renderer 模式不要求 backend 本地 JS；模板资产先 flush 以满足 PostgreSQL FK；Werkzeug 彩色请求行先去 ANSI 再脱敏，没有放宽隔离或日志约束。

- 源栈和恢复栈 readiness 全通过；固定 PostgreSQL 16.6 专项 **19 项**、renderer 正式限制合同 **2 项**通过。指定 33 页 PPTX 经真实 worker/LibreOffice 成功转换；重点页已复核。
- 真实 Chromium UI 创建/快速输入/保存刷新/撤销/模板导入通过；PPTX/PDF 均经真实渲染和报告确认。PowerPoint 16.0.20430.20092 原生文字与图片编辑往返通过；LibreOffice PPTX 的行距/基线差异已记录，视觉阈值未标定。
- 原维护脚本实际备份再恢复至**不同数据库集群和全新空卷**，审计一致：143 资产、19 Scene revision、1 snapshot、2 已审核 export、1 条假凭据可解密。恢复后再验证历史/当前 Scene、导出与 33 张参考图 hash。未生成真实模型 plan/attempt，不能扩展为付费故障恢复证明。
- API、worker、renderer 与前端日志中未发现 QA secrets 或签名 query；这是该样本的实测结果，不代替外部 TLS 代理的脱敏责任。

可选容器合同测试为 `backend/tests/integration/test_scene_container_runtime.py`，需要在独立 Linux QA 环境显式设置 `SCENE_TEST_RENDERER_IMAGE`、`SCENE_TEST_DOCKER_DAEMON_ID`、`DOCKER_HOST` 并以 root 执行，以验证 worker 的真实 UID 目录移交；不默认连接 daemon、不拉镜像、不挂 Docker socket、不复用现有卷。PostgreSQL 与 Nginx 测试入口及完整过程见 `docs/zh/web-ppt-editor-implementation.md`。

证据保存在本机忽略目录 `tmp/scene-docker-qa/verification.json` 与产物子目录；测试栈、专用 daemon 和临时构建 relay 已关闭，专用数据/镜像/备份保留。没有读取生产 `.env` 或执行真实付费调用。部署升级仍需针对自己的网络、凭据、客户端和数据重新验收，不复制 QA 假凭据作生产配置。

> 后续源码验证说明：大纲候选/原规划版本/事实来源合同已继续修改，详见实现文档最新一节。本页记录的实测 image ID 属于此前快照；部署最新代码必须重新 build 和验证，不能仅复用这些 QA tag。运行时依赖锁指纹未变不代表业务源码未变。


### TextLayout 协议升级与部署边界（2026-10-03）

当前 worker 的 PPTX/PDF 都强制经过 TextLayout v1；同步升级 backend/worker/renderer/frontend，不能把新 worker 与旧 renderer 混用。renderer 心跳新增实际 `text_layout_identity`，旧协议或字体不一致不通过 readiness。布局含 code point 行区间、硬/软换行、字号、行框、基线和 CDP 实际字体；不可编辑布局与指纹存入私有导出报告。worker 只保留按 owner/project 隔离的 64 项/32 MiB、300 秒内存 LRU，不新增持久卷或独立缓存备份。

新增锁定 `regex==2025.11.3` 验证字素边界，更新引擎源码清单和审查输入摘要；当前运行时锁指纹为 `fa46ccde2013164dd3a15b0ec2242b1a8b5e23778bce21820ab4ecee789b0f24`。**必须重建新镜像，不可复用上节旧 QA tag**。已有已验收备份继续保留原镜像/旧锁；不要用新锁覆写旧归档来绕过恢复校验。

本轮真实只读/断网/独立 UID renderer 合同通过，新增布局缓存→PPTX/PDF 与 PowerPoint 原生编辑往返通过。此次 renderer 是基于旧已验证依赖的离线 QA 派生镜像，不等于三个原始 Dockerfile 和完整 Compose 已重新验收。证据与详细结果见实现文档末节和本机 `tmp/scene-layout-qa/`；正式部署、模型出网/计费、WPS 与视觉阈值仍待验证。当前视觉审核不自动放行；Office 粗体和 Chromium 合成粗体仍可能不同。
