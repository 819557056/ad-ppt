# Banana Slides 可编辑 PPTX 技术分析：从生成画面到忠实导出

> 分析对象：当前工作区的 Banana Slides、`tmp/ppt-master-main`、`tmp/NextCreator-main` 和 `tmp/OfficeCLI-main`。本文基于代码和流程文档静态核对；没有对用户这次下载的 PPTX 做 PowerPoint/WPS 实机截图对比，也没有运行三个参考项目的端到端导出。现象“结构化导出无原背景、文字与生成图不一致”可由当前实现直接解释。报告不把“PPTX 中有文本框”等同于“忠实还原原图”。

## 1. 结论

**这是两种产物的设计源不一致，不是背景图片偶然丢失。** Banana Slides 当前先让图像模型生成一张完整幻灯片图片；“结构化可编辑 PPTX”却只从数据库中保存的页面大纲取标题和要点，再套固定浅色卡片模板，完全不读取最终图片。图像模型可能调整措辞、增减文字、改变版式和背景，导出端没有记录这些最终可见元素的结构与坐标，因此不可能单靠 `python-pptx` 从大纲重现画面。

对已有生成图，应该走**图片逆向重建**：以当前图片版本为视觉真值，识别文字和区域、分层、修复去字背景、重建原生文字，然后做逐页视觉复核。对未来新生成项目，更稳的路线是**前向结构化生成**：先建立包含文本、坐标、背景和素材层的页面设计模型，再用同一模型渲染预览及导出 PPTX。`ppt-master-main` 值得借鉴“单一画面权威 + 有限映射 + 质量门”，`NextCreator-main` 提供轻量的去字背景与原生文本框参考，`OfficeCLI-main` 提供原生 OOXML 操作及渲染/检查参考；三者解决的是不同环节，**不能只接入其中一个工具就得到忠实可编辑还原**。

## 2. 现状与问题定位（已证实）

| 路径 | 输入与真实行为 | 可编辑性 | 与已生成图一致性 |
| --- | --- | --- | --- |
| 普通“导出为 PPTX” | 逐页读取 `generated_image_path`，每页铺一张全幅图片 | 页面图片可移动/替换，图内文字不可编辑 | 高，前提是选中版本及比例正确 |
| “结构化可编辑 PPTX” | 读取 `outline_content` 的标题/要点；没有要点时才拆 `description_content.text`；固定颜色、背景、卡片、字体和页脚 | 原生文本框与形状可编辑 | 低；原图与其最终文字根本没有进入导出输入 |
| “可编辑 PPTX（图片识别）” | 从生成图提取元素、clean background、图层和文字，再组装 PPTX | 取决于识别、修复及对象类别 | 目标是接近原图，但受识别质量和外部服务可用性限制 |

### 2.1 生成端的真值是图片，不是大纲

`ai_service.py` 将大纲、页面描述、参考模板和要求组成图像 prompt；`task_manager.py` 调用图像 provider，把最终图片保存为有版本记录的 `generated_image_path`。`Page` 将 `outline_content`、`description_content` 与 `generated_image_path` 分开存储，没有保存“画面实际文本 + bbox + 字体 + 层次 + 资源”的可编辑中间表示。前端预览可用缓存图，但导出应固定并读取原图版本，避免同时编辑/切换版本时错配。

### 2.2 结构化导出为何没有背景、内容不同

`plans_from_pages()` 取的是旧大纲，而非图中 OCR 文本；`create_structured_pptx()` 给每页画固定 `PAPER` 底色、蓝色顶条和白色卡片。整个模块没有引用 `generated_image_path`、没有 `add_picture()`，也没有图像区域分析、原背景恢复或坐标映射。前端提示文字实际上已经说明“版式为简洁模板，不复刻已生成图片”，但菜单名称“结构化可编辑 PPTX”仍容易被理解为“当前图可编辑化”。

当前 `inspect_pptx()` 只验 ZIP/OOXML 可打开、页数、文本框/形状/图片数量及是否越界；**它不能证明文字与原图一致、背景存在、遮挡正确或在 PowerPoint 实际渲染正确**。因此质量计数合格不代表用户所见问题被解决。

### 2.3 现有图片识别导出是另一条路，但仍有缺口

现有 `ImageEditabilityService.make_image_editable()` 已实现元素提取、clean background 和递归处理；`ExportService.create_editable_pptx_with_recursive_analysis()` 以 clean background 或原图为底，叠加识别出的元素。它是适合已有图片的基础，而非从零开始。此前用户碰到的 `EXPORT_AUTHENTICATION` 发生在“版面分析”，与结构化导出不读取图片是**两个独立问题**：修复 MinerU／百度 OCR 等认证或换可用识别实现，只会恢复图片识别路线，不会自动让大纲导出忠实原图。

需特别检查现有兜底：当 clean background 不存在时，代码会把**含有原文字的整页原图**放在最底层，然后再叠加可编辑文字；这可能产生重影，不应算作合格的忠实可编辑结果。缺少背景时应明确降级为“原图保真、不可编辑文字”，或者阻断“忠实可编辑”交付，而不是静默伪装成功。

### 2.4 关键代码证据

- Banana Slides：`backend/services/ai_service.py:976-1013`（图片 prompt），`backend/services/task_manager.py:363-424, 853-891`（生成与版本保存），`backend/models/page.py:21-24`（三类分离状态）。
- 导出入口：`backend/controllers/export_controller.py:142-210, 231-275, 442-563`（普通、结构化、图片识别三路），`frontend/src/pages/SlidePreview.tsx:31-34, 2355-2362`（菜单文案）。
- 导出实现：`backend/services/export_service.py:593-610, 1631-1688, 1778-1827`，`backend/services/image_editability/service.py:130-203`，`backend/services/structured_pptx_service.py:55-79, 112-164, 168-203`。

## 3. `ppt-master-main` 的真实技术路线

`ppt-master-main` 是一个带规范、工作流、脚本的 PPT 创作/转换工具包，不是一个可直接挂进 Banana Slides 的在线服务。其主生成路线与图片还原路线必须区分。

### 3.1 新 PPT 生成：设计稿先于 PPTX

1. 来源材料整理和页面设计规划；准备引用的图片/图标等资产。
2. 逐页创作**受约束的项目 SVG** 到 `svg_output/`。这不是任意 SVG；支持的元素、属性、坐标、字体、图像引用和 DrawingML 映射均有明确合同。页面最终可见的背景、文本、素材、形状都必须已在 SVG 里或被它引用，导出器不再根据大纲“补画一页”。
3. `svg_quality_checker.py` 做画布、语法、素材/字体、转换合同等检查，最终质量报告与作者源关联。`finalize_svg.py` 从 `svg_output/` 生成自包含的 `svg_final/` 视觉预览；**原生 PPTX 默认仍从 `svg_output/` 导出**，`svg_final/` 不是第二个编辑真值。
4. `svg_to_pptx.py` 调用转换包，借助 `python-pptx` 建基础包，并将支持的 SVG 文本、图像、形状等转换为原生 DrawingML，再处理媒体与 relationships。映射例子：`<text>` → 可编辑 `p:sp/p:txBody`，`<rect>/<path>` → 原生形状，`<image>` → `p:pic` 与媒体关系；图片不会被神奇地变成可编辑文字。
5. 打包后检查内部关系、结构、动效等；`pptx_delivery_check.py` 侧重包完整性、依赖/便携性、媒体和字体等交付问题，不能替代“与参考图片视觉相同”的评价。视觉审查另有浏览器渲染工具与人工复核。

`ppt-master-main` 文档中的 **structured** 主要指 Master/Layout/Slide 所有权和占位符等 PPTX 结构；它不是 Banana Slides 当前“从大纲自动套模板”的“结构化导出”，名称相同但语义不同。

### 3.2 已有整页图片 → PPTX：逆向重建专线

`image-to-pptx.md` 把**可见像素**定为输入真值：每张规范化页面图对应一页，先记录源图 hash、尺寸、区域 bbox、逐字文本、置信度、遮挡及层级观察；按内容类别选择原生文本、精确图形/数据图、原生形状、场景图层或 `manual_required`。它要求最小有用图层栈：去掉可编辑文字及独立对象后的 clean base、人物/前景等独立图层、准确原生文字。低置信文本、不可核对的数据图表不可编造。**禁止把完整截图作为唯一背景，再叠加少量文字冒充可编辑还原。**

这条流程目前在该仓库明确限定为 Codex + Quick，并依赖 Codex 的参考图编辑能力与逐层人工检查；不是 `openai-codex` Python SDK 一接入就能得到的通用 OCR/分层 API。它也没有宣称每张位图都能无损逆向成全部原生图元：照片/复杂插画可保持独立图片层，只有可核实的文字、形状、图表才转为对应原生对象。

### 3.3 可以借鉴与不宜照搬

| 能借鉴 | 在本项目的实现形式 | 边界 |
| --- | --- | --- |
| 单一画面权威 | 定义 `SlideScene`/受约束 SVG，预览与 PPTX 从同一份页面表示生成 | 不能继续先出任意整页位图、导出时再凭大纲猜布局 |
| 可编辑性分层声明 | 为每个元素记录 `native_text` / `native_shape` / `image_layer` / `manual_required` | 图片层可裁剪/移动，不等于像素内容可编辑 |
| 图片重建清单和 provenance | 保存源图版本/hash、bbox、OCR 原文、置信度、clean-base/图层路径与来源 | 识别不可信时阻断或显式降级，不能补造文本/数据 |
| 有限 SVG→DrawingML 映射 | 先做项目实际需要的文本、图片、矩形、线、裁剪与层序子集 | 不应承诺任意 SVG 无损转换；字体替换和复杂效果需复核 |
| 质量门与产物追踪 | 输入图 hash → IR 版本 → 预览 → PPTX → 质检报告 | 包结构通过不等于视觉通过；必须有渲染对比 |

不建议立即复制其全套 Strategist/Executor、Master/Layout、动效、round-trip、复杂 SVG 编译器与规则文档：维护范围远超当前需求，且其“图片还原”仍是强人工/模型流程，不是确定性转换服务。若以后复制 MIT 代码，需保留原许可与版权声明，并对接本项目的 AGPL、依赖和部署方式。其主转换依赖 `python-pptx`，复杂形状/字形处理还涉及 `skia-pathops`、`uharfbuzz` 等；不是只改一个导出函数。

**PPT Master 证据**：`tmp/ppt-master-main/docs/zh/technical-design.md:13-34, 122-140, 176-191`；`tmp/ppt-master-main/skills/ppt-master/workflows/profiles/image-to-pptx.md` §2-7；`tmp/ppt-master-main/docs/zh/powerpoint-svg-mapping.md:66-69, 111-149`；`tmp/ppt-master-main/skills/ppt-master/scripts/finalize_svg.py:5-15, 320-397`；`tmp/ppt-master-main/skills/ppt-master/scripts/svg_to_pptx/pptx_package/builder.py:7587-7603, 8475-8560`；`tmp/ppt-master-main/skills/ppt-master/scripts/visual_review.py:97-120, 158-223`；`tmp/ppt-master-main/skills/ppt-master/requirements.txt`。

### 3.4 `NextCreator-main`：轻量“去字背景 + 可编辑文字”

**已核对的生成链路。** 该项目是 React/Tauri 节点工作流。PPT 页面节点把大纲、页面描述、视觉模板图和补充图组合为 prompt，通过图像模型生成**含文字的完整页面图**，并保存原图及缩略图。组装节点有纯图片和“可编辑”两种导出：纯图片模式用 `pptxgenjs` 把整页图铺满；可编辑模式先由 Rust/Tauri 命令调用 Gemini 检测文字区域，再提取文字样式，随后按检测区域生成遮罩并修复背景，最终 `pptxgenjs` 输出“一张去字背景图片 + 若干原生文本框”。它还提供只含去字背景的单独导出；可编辑导出要求所有页面处理完成。

**可借鉴的实现。** 检测与修复是两个显式阶段；文字区域用归一化 `box_2d`/polygon 传输，转换为源图像素坐标，再按页面尺寸映射到 PPTX。批处理按页报告 `detecting`、`inpainting`、`completed`、`error`，便于失败隔离。背景修复器对遮罩边缘采样，按背景颜色变化选纯色填充或渐变填充，并对遮罩膨胀、羽化，适合平坦底色/渐变页的低成本 fast path。本项目可借鉴“先识别并清除原图文字，再放原生文字”的顺序，以及局部、可解释的修复策略；不必为此迁入 Tauri/Rust，可在现有 Python 图片识别服务里实现同等接口。

**能力边界。** 此方案只把**文字**变成文本框；背景仍是一张图片，图标、人物、表格和图表没有按对象拆层。`TextBox` 没有置信度、字体来源或源图 hash；导出时固定 `微软雅黑`，字号由检测值、坐标缩放和公式估算，不能保证字形、换行与原图一致。当前修复器只有纯色/渐变两类策略，文字盖在照片、纹理或复杂插画上时不能靠局部采样可靠恢复隐藏像素。Gemini 检测与样式提取仍依赖外部 API，组装节点当前还把检测模型固定为 `gemini-3-flash-preview`；不能把它当成“完全离线、无认证依赖”或无需适配即可换 OpenAI 的实现。代码没有导出 PPTX 后与原图逐页渲染比对的发布门。因此它是**可编辑文字的实用基线**，不是全部元素原生可编辑、像素级还原的证明。

**NextCreator 证据**：`tmp/NextCreator-main/src/components/nodes/PPTContentNode/usePPTContentExecution.ts:281-364, 402-455`；`tmp/NextCreator-main/src/components/nodes/PPTAssemblerNode/types.ts:6-69`；`tmp/NextCreator-main/src/components/nodes/PPTAssemblerNode/index.tsx:98-103, 363-372, 530-564`；`tmp/NextCreator-main/src/components/nodes/PPTAssemblerNode/pptBuilder.ts:18-75, 104-203, 232-289`；`tmp/NextCreator-main/src-tauri/src/text_removal/gemini_detector.rs:142-170, 179-207`；`tmp/NextCreator-main/src-tauri/src/text_removal/service.rs:315-452`；`tmp/NextCreator-main/src-tauri/src/text_removal/adaptive_inpainter.rs:13-71, 141-182`；`tmp/NextCreator-main/src-tauri/src/text_removal/batch_processor.rs:228-335`。

### 3.5 `OfficeCLI-main`：原生 OOXML 操作与“生成后看结果”

**已核对的实现。** OfficeCLI 是 .NET 10 命令行程序，PowerPoint 处理器直接使用 `DocumentFormat.OpenXml` 的 `PresentationDocument`、SlidePart、Shape、Picture 和媒体 relationships 等包结构。`create/add/set/get/query` 等命令通过路径选择 PPTX 对象；`batch` 以 JSON 执行修改，`dump` 把现有 PPTX 尽量变成可重放命令，并能报告部分不支持的元素；实现中仍有无法回放的特殊动画等边界，不能将 `dump → batch` 视为任意 PPTX 的无损往返。它能创建/修改原生文本、形状、图片、背景等，但输入必须已有这些对象/参数或由调用方提供精确坐标及资源。

**渲染与校验价值。** `view ... html/svg/screenshot` 从 PPTX 生成可视化结果。Windows 且安装 PowerPoint 时，`screenshot --render auto/native` 可通过 PowerPoint 自动化导出实际幻灯片 PNG；不可用时 `auto` 走内置 HTML 预览及浏览器截图，`native` 会报不可用。`validate` 使用 Open XML SDK/schema 与额外 OPC 预检检查包有效性，`view ... issues` 提供结构问题线索。由此可把 OfficeCLI 作为 Banana Slides **可选的 PPTX 后处理/质检工具**：例如针对既有原生 deck 定位并修改对象、输出包结构检查，再在目标机器渲染导出页供差异比对。其 HTML 预览与 PowerPoint 原生渲染并不等价；必须记录实际使用的 renderer。

**不能解决的部分。** OfficeCLI 没有从整页 PNG 自动检测文字、去字、分层或重建背景的流程。`dump` 处理的是**已存在的 PPTX 对象**，不能把 Banana Slides 的全幅图片解析成原生形状；`validate` 只说明包/模式是否可接受，不能判断文字是否与原图相同。即使引入 OfficeCLI，OCR、背景修复、图层清单和视觉对照仍由本项目负责。若接入 Python Web 服务，合理方式是把发布的 CLI 作为**隔离子进程**调用其 JSON 命令，并设置超时、输入/输出路径与版本固定；不是把 C# 代码直接塞进现有 `python-pptx` 链路。其原生渲染仅在 Windows 且有 PowerPoint 时可用，部署 .NET 10 与浏览器/Office 依赖需要单独评估。

**OfficeCLI 证据**：`tmp/OfficeCLI-main/src/officecli/officecli.csproj:3-24`；`tmp/OfficeCLI-main/src/officecli/Handlers/PowerPointHandler.cs:12-17, 137-154, 2355-2369`；`tmp/OfficeCLI-main/src/officecli/Handlers/Pptx/PowerPointHandler.Add.Slide.cs:13-82`；`tmp/OfficeCLI-main/src/officecli/Handlers/Pptx/PowerPointHandler.Add.Shape.cs:108-152`；`tmp/OfficeCLI-main/src/officecli/Handlers/Pptx/PowerPointHandler.Add.Media.cs:16-70`；`tmp/OfficeCLI-main/src/officecli/Handlers/Pptx/PptxBatchEmitter.cs:64-76, 501-523`；`tmp/OfficeCLI-main/src/officecli/CommandBuilder.View.cs:209-305`；`tmp/OfficeCLI-main/src/officecli/Core/PowerPointPngBackend.cs:11-43`；`tmp/OfficeCLI-main/src/officecli/Core/RawXmlHelper.cs:571-643`。

### 3.6 四项目横向对比与选型

| 项目 | 画面/数据真值 | 已有整页图转可编辑 | 原生对象输出 | 导出后验证 | 对 Banana Slides 的适用点 |
| --- | --- | --- | --- | --- | --- |
| Banana Slides 当前 | 生图图片用于预览；结构化出口另读旧大纲 | 图片识别链路已存在，但依赖识别/修复服务 | `python-pptx` 文本、形状、图片 | `inspect_pptx()` 包与对象计数 | 优先修复真值不一致及图片重建质量门 |
| PPT Master | 受约束 SVG 是新生成页面权威；图片还原以像素为真值 | 有严格的人工/模型重建 profile，不是通用一键服务 | SVG→DrawingML，含文本、形状、图片等 | SVG 合同、包审计、视觉审查分层 | 目标 IR、图层清单、可编辑级别与交付门 |
| NextCreator | 先生成含字整页图；逆向时原图是输入 | Gemini 找字 + 本地纯色/渐变修复 | `pptxgenjs`：去字背景图片 + 文本框 | 页面处理状态；未见原图/PPTX 渲染差异门 | 文字级可编辑的低复杂度 fast path，不适合复杂背景/对象级还原 |
| OfficeCLI | 已存在或调用方构造的 PPTX/OOXML | 无位图 OCR/分层能力 | Open XML SDK 原生对象增删改、JSON batch | OOXML/OPC 验证，HTML 或条件性 PowerPoint 原生截图 | 可选的对象后处理、包检查、真实客户端渲染适配 |

**技术选型结论。** 短期优先在现有 Python 逆向链路上吸收 NextCreator 的“检测—去字—文本框”闭环及简单背景 fast path；复杂页再走更强的分层/修复并明确人工复核。中期按 PPT Master 的原则建立单一页面 Scene 与可追溯导出。OfficeCLI 可在需要精细 OOXML 编辑或 Windows PowerPoint 实际截图时按需接入，不应作为图片识别服务的替代。四者的“可编辑”粒度不同，产品文案与质检必须显式标明“文字可编辑、对象可编辑、背景仍是图片”等级。

**集成约束**：NextCreator 的 `package.json`/`LICENSE` 标为 AGPL-3.0；OfficeCLI 的 `LICENSE`/`NOTICE` 为 Apache-2.0 并要求保留 NOTICE；PPT Master 为 MIT。借鉴架构与复制源码是不同决策，若直接移植代码或二进制，需保留相应声明、核对依赖与部署环境；本文不把许可证兼容性视为技术质量保证。

## 4. 建议目标架构：分成两条产品路线

### 路线 A：已生成图片的“忠实重建”

输入必须锁定 `PageImageVersion` 的原图文件、版本号、尺寸和 SHA-256，避免导出途中与用户预览图不一致。建议流水线：

```text
当前版本原图 → 页面/区域清单 → OCR/版面识别（逐字、bbox、置信度）
             → 分类（文字、形状、图表、照片、人物、装饰）
             → 去字与移除独立图层后的 clean base + 透明前景层
             → 原生文本/形状及准确图片层组装 → PPTX 实际渲染
             → 与原图逐页对比 + 文字核对 + 人工异常审核 → 发布或显式降级
```

建议把现有 `EditableImage` 扩展为持久化的重建清单，而不是只在一次异步任务里临时传递。每个区域至少记录：`source_version/hash`、原图坐标 bbox、多边形/遮挡、类型、逐字原文、OCR 置信度、字体/颜色/字号的估计来源、实现方式、输出资产/hash、z-order、校验状态。背景应是去掉所有准备独立化的文字/对象后、与原图像素尺寸严格对齐的 clean base；抠出对象需保留透明度与原位置。表格/图表若不能核实数值和关系，保留精确图层或标记人工处理，不能生成“看起来类似”的原生图表。

本路线可以复用现有 `ImageEditabilityService`、`PPTXBuilder`、版本系统和任务进度机制；优先补认证检测及可替代 OCR 提取器、clean-base 失败策略、持久化清单和视觉质检。可参考 NextCreator 加一条“平底色/渐变背景 + 文字”的轻量处理分支；纹理/照片页不可套用其简化修复。外部 MinerU／百度不可用时，可以接本地 OCR/版面模型，但“离线”只解决服务可用性，不自动解决识别、字体和背景修复准确率。没有可信识别结果时保留普通图片型导出，不能标成忠实可编辑。

### 路线 B：未来生成内容的“原生优先”

改变生成契约：模型先输出受 schema 约束的 `SlideScene`，其中含页面尺寸、**最终将呈现的逐字文本**、文本框/样式、基础背景、素材区域、层序、图表数据和每个对象的编辑级别。AI 生图仅负责背景、插画、人物或纹理资产，要求无文字，且按固定画布/区域生成；页面文字由本项目在预览与 PPTX 中同源绘制。用户改文案、切换图片版本或局部重绘时同步更新 Scene/资产引用；只有 Scene 与预览一致才允许标记“原生可编辑导出”。

```json
{
  "schema_version": 1,
  "canvas": {"width": 1920, "height": 1080},
  "layers": [
    {"id": "base", "kind": "image_layer", "asset": "base.png", "bbox": [0, 0, 1920, 1080]},
    {"id": "title", "kind": "native_text", "text": "最终可见标题", "bbox": [120, 90, 1400, 120], "font": "...", "z": 10}
  ],
  "preview_hash": "...",
  "asset_hashes": {"base.png": "..."}
}
```

这是**建议模型，不是当前已存在的数据结构**。首版可由 Python `python-pptx` 按 Scene 直接导出（本项目已有构建器）；当复杂路径、样式和浏览器预览一致性成为瓶颈，再考虑采用 PPT Master 式的“受约束 SVG → DrawingML”编译层。即便选择 SVG，也必须使它成为页面画面的唯一可见设计真值，不能只是大纲外面再套一层 SVG。

### 两条路线不能混同

旧图逆向需要 OCR、分割/inpaint 和不确定性管理；新图前向结构化可以避免 OCR，但必须**改变生图方式**，不能让图像模型先画含字整页，再宣称 Scene 文本就是最终像素文字。对同一个项目可按页混用，并在下载前逐页展示“原图保真/文字可编辑/背景可编辑层级/需人工复核”的状态。

## 5. 验收与迁移建议

### 分阶段落地

1. **P0：命名和诚实兜底。** 将当前大纲导出明确命名为“按大纲重新排版”，与“基于图片忠实重建”分开；保留原图 PPTX 作为保真下载。对 clean background 缺失、文字低置信、认证失败给出清楚的状态，不静默交付带重影的“可编辑”文件。
2. **P1：修现有逆向链路。** 在任务启动前检测识别/修复 provider 的可用性；失败早报准确阶段；保存清单与中间图层，绑定图片版本；完善替代提取器和每页失败隔离。优先解决用户已生成 PPT 的诉求。
3. **P2：加入真实视觉验收。** 将导出 PPTX 用目标渲染器（至少 LibreOffice；关键样本再用 PowerPoint；Windows 有安装 PowerPoint 时可试用 OfficeCLI `screenshot --render native`）渲染为图片；对齐原图尺寸，逐页做差异图、差异率/SSIM 等参考指标，并执行逐字文本比对、可编辑对象检查、背景/图层覆盖检查。差异阈值需以真实样本标定；自动指标只做筛查，不替代人工核对复杂页面。
4. **P3：新生成流程引入 Scene/受约束 SVG。** 从无字背景 + 原生文字的简化页面类型开始，再扩展图片裁剪、图表和复杂装饰。预览、PPTX 与测试均由同一设计源驱动；迁移时旧项目继续走逆向路线，不能用旧大纲假装升级成功。

### 验收维度（建议在导出质检报告中独立给出）

- **内容一致**：原图中每个可辨识字符串、数字、表格标签逐字核对；低置信区单列人工复核。不能只比大纲。
- **视觉一致**：逐页背景存在且位置正确；图层顺序、遮挡、裁剪、色彩和文字位置可对照原图；输出渲染图与原图留存差异图。
- **对象可编辑**：逐页统计可选择的文本框、形状、图片层与不可编辑区域；照片/复杂插画维持图片层是合理的，但要如实标注。
- **包与客户端有效**：OOXML 完整、relationships 无悬挂、无越界/乱码；PowerPoint 与目标客户端抽样打开、选中文字、移动图层，并复核字体替换。
- **可追溯**：每个 PPTX 记录来源图片版本/hash、Scene 或重建清单版本、引擎版本、告警和质检结果；重新导出能复现。

## 6. 最终建议

**不要把现有“结构化可编辑 PPTX”继续作为“当前图片的可编辑版”使用。** 已经生成的页面应优先修复并强化图片逆向路线；日后要同时获得稳定的视觉一致性和原生文字编辑能力，应改成“统一页面设计源驱动预览与导出”。PPT Master 可参考数据合同、图层拆分、SVG→DrawingML 映射和质量门；NextCreator 可参考文字级逆向闭环；OfficeCLI 可选作原生对象操作、包校验与目标客户端截图工具。仅替换导出库、接入 OfficeCLI、复制 NextCreator 的文本框方案或整包接入 PPT Master，均无法从一张已渲染位图自动恢复不存在的精确原生对象。

## 7. 后续 Web 编辑器设计（2026-10-02）

针对已确认的新需求，另行形成了 [Web PPT 编辑器产品与技术设计](./web-ppt-editor-design.md)。它以本文的**路线 B：前向结构化生成**为首版主线，覆盖 PPTX/图片风格参考、自然语言单页/多页生成、轻量对象画布、原生文字 PPTX 与带文字层 PDF，以及后续 Sub2API 菜单和身份集成。

该设计包含 `SlideScene` 合同、数据库 ER/表字段/索引、API、任务状态机与付费调用恢复、候选版本/并发控制、不可变导出快照、Linux/Docker 部署及验收用例。首版只保证标题、正文、独立标注为原生文字；图表、图标、插画先作为可替换图片对象，图表数据编辑列为后续备选。

本文第 5 节优先修复旧图逆向链路的顺序服务于“已有生成图片的忠实重建”，**不作为新 Web 编辑器的首版排期**；两者的目标与验收分开。新设计是待实施技术基线，不表示已有代码已经具备对象画布、Scene 模型、持久化新队列或安全 SSO。
