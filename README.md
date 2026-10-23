# Readori Source Validator for Windows

这是一个可脱离 Readori iOS 工程运行的书源验证工具，包含命令行验证器、Windows 图形界面、静态书源审计工具和 Windows 打包脚本。

## 功能

- 按四阶段流水线验证：自动去重 → 快速扫描（搜索 / 发现入口）→ 完整链路（详情、目录、正文）→ 稳定性复测。
- 去重分三层执行：规范化站点 URL（合并协议、大小写、端口、路径和查询顺序差异）→ 规则指纹（保留同站点的不同有效规则）→ 完整验证后的书名/作者聚合（仅合并同一站点解析到同一本书的变体，避免误删其他站点覆盖）。
- 快速扫描默认每源 8 秒硬截止（可用 `--quick-timeout` 调整到 5–10 秒）；完整验证和复测也有独立硬超时，坏源不会拖死整批任务。
- 每个阶段使用独立并发线程池，阶段之间串行传递结果，避免 1000+ 书源同时进行深链路请求。
- 快速扫描会缓存候选书 URL、规则变量和 Cookie，完整验证复用缓存避免重复搜索；稳定性复测重新搜索以发现临时失效。
- 支持并发、复测轮次、失败原因、阶段明细和通过书源 JSON 输出。
- 兼容 Legado/Readori 常见 CSS、XPath、JSONPath、内嵌 JavaScript、动态请求、Cookie、压缩包和部分 WebView/付费源识别。
- GUI 支持选择 JSON 文件或目录、实时进度、取消任务、打开报告目录。
- 按 App 版本判定（`--app-version 1.1.1|1.1.2|both`，默认 1.1.1），见下文“App 版本档案”。
- `static-tools/capture.py` 提供静态审计、修复、合并和书源生成辅助，不代替联网验证。

## 快速运行（已有 Python）

1. 安装 Python 3.12 或更高版本。
2. 双击 `run_gui.vbs` 打开无控制台图形界面；它会首次自动创建 `.venv` 并安装运行依赖。
3. 也可以先双击 `install_dependencies.ps1`，再双击 `run_gui.vbs`。
4. 选择书源 JSON 文件或目录，设置并发数和超时，点击“开始验证”。

命令行运行示例：

```powershell
python .\source_validator_cli.py `
  --input .\sources.json `
  --report-path .\output\validation_report.json `
  --validated-output .\output\validated_sources.json `
  --validated-output-full .\output\validated_sources_full.json `
  --pipeline staged --quick-timeout 8 `
  --rounds 1 --min-pass-rounds 1 --workers 20 `
  --source-timeout 30 --idle-timeout 180 --no-mirror `
  --app-version 1.1.1
```

## App 版本档案（1.1.1 / 1.1.2 / both）

验证器回答的是“这条书源在某个 Readori App 版本里能不能用”。App 1.1.1（当前公开版）与 1.1.2（下一版）在四处行为不同，验证器按所选档案模拟：

| 差异 | 1.1.1 | 1.1.2 |
| --- | --- | --- |
| `@js:` 开头的 searchUrl | 请求脚本里第一个 http(s) 字面量并丢弃脚本（取不到字面量才执行脚本） | 执行脚本，返回值才是搜索地址 |
| String 字段写成 JSON 数组 / 对象（exploreUrl、loginUi、规则子字段等） | 整个字段丢弃（视为不存在） | 存成紧凑 JSON 文本 |
| CryptoJS | 无条件装一个简化的假对象，会遮挡书源自带的库；`AES.encrypt` 不存在 | 书源引用且未自带时才装 crypto-js 4.1 兼容实现 |
| 批量验证中被拦下的登录 / 验证交互 | 记为失效 | 记为“需要登录/验证”，不算失效 |

三种模式：

- `--app-version 1.1.1`（**默认**）：按当前公开版判定。通过的源在 1.1.1 里确实可用；输出文件路径和含义与以前相同。
- `--app-version 1.1.2`：按下一版判定。输出路径不变，内容为 1.1.2 通过集。
- `--app-version both`：两版对比。1.1.1 先跑全部书源，只有“档案敏感”的源（静态特征命中上表四项之一）才在 1.1.2 下重跑，其余直接复用 1.1.1 的结果。`--pipeline legacy` 不能与 `both` 同用。

GUI 里的“App 版本”下拉框对应这三种模式（1.1.1（当前公开版）/ 1.1.2（下一版）/ 两版对比），默认 1.1.1。服务器任务用配置项 `config.app_version`（`1.1.1` 或 `1.1.2`，默认 1.1.1，Cloudflare 控制台有同名下拉框）；服务器不支持 `both`，想对比请提交两个任务。

输出文件：

- `--validated-output` 与 `--validated-output-full`：`both` 模式下仍是 1.1.1 通过集，默认消费方不受影响。
- `both` 模式额外写 `<文件名>.app-1.1.2<后缀>`（例如 `validated_sources.app-1.1.2.json` 和 `validated_sources_full.app-1.1.2.json`），内容为 1.1.2 通过集。
- 报告（`--report-path`）只增不减：每条结果带 `appVersion`、`appVerdict`（`pass`/`invalid`/`needsInteraction`/`timeout`），`both` 模式另有兼容性标签（如“两版均可用”“仅 1.1.2 可用”“1.1.1 失效、1.1.2 需要登录/验证”）和 `passedSourcesByAppVersion`。

分组与标签（写进书源的 `bookSourceGroup` / `customTag`）：

- 通过源的 `customTag` 为 `✅ 完整验证通过（书籍+详情+目录+正文）`。App 的 `searchValidationTier` / `healthStatus` 只认含“完整验证通过”的标签（排序权重 4，健康状态 healthy）；旧标签 `✅ 书籍+详情+目录+正文通过` 在 App 里不被识别。
- `bookSourceGroup` 为验证日期 `MMDD`（洛杉矶时区）。1.1.2 档案下，档案敏感且未在 1.1.1 通过的源会在 `MMDD` 后追加分组 `需1.1.2`：把 1.1.2 的包导入 1.1.1 时可以按这个分组筛掉；1.1.2 正式上架后可批量删除该分组。分组不含“失效”，不影响健康判定。

已知偏差（验证器与 App 不完全一致的地方）：

- 本机验证 JS 默认走 Node；没有安装 QuickJS 时 QuickJS 路径只能靠单元测试和 CI 覆盖。
- DES / 3DES 的 CTR 模式（CommonCrypto 支持）在验证器里返回 `!unsupported mode`：加密抛错、解密得空。AES 全部模式、DES / 3DES 的 CBC / ECB / CFB / OFB 与 App 一致。
- `both` 模式靠静态特征判断是否重跑。动态拉取的脚本只有写成 `eval(java.ajax/get/cacheFile …)` 或 `java.importScript(` 才会被识别为 CryptoJS 敏感，其他动态代码可能漏判；需要完全确定时请分别用 `1.1.1` 和 `1.1.2` 各跑一遍。
- 静态预判（脚本里出现 `getVerificationCode(` / `openUrl(`，或 loginCheckJs 里出现 `startBrowserAwait(`）在 1.1.1 档案下仍会提前判失败，只会偏保守（误判失败，不会误判通过）；1.1.2 档案（R157 起）不再从规则文本下静态结论，交互调用在 App 里是运行时才触发的，由实际执行决定。`openUrl` 在 App 里不被记录，所以 1.1.2 下也记失效；`getVerificationCode` 与 loginCheckJs 里的 `startBrowserAwait` 在 1.1.2 下记“需要登录/验证”。
- 切到 1.1.1 档案后，一部分以前“通过”的源会变成失败，这是预期结果：例如 `@js:` 带字面量的源（已发布通过包里有 5 个），1.1.1 实际会请求字面量、丢掉 POST body；依赖 `startBrowserAwait` 的源，批量验证没有人手完成验证，在 1.1.1 里记失效。
- 规则对象被存成 JSON 字符串（`"ruleSearch":"{...}"`）时，两档案都按 App 的规则解开，再套简写；有这种写法的源结果可能与旧版验证器不同。
- 服务器版不支持 `both`；任务配置存在已有的 `config_json` 里，不改 SQLite 表结构，旧任务没有 `app_version` 时按默认版本处理。

默认版本是 1.1.1；1.1.2 正式上架当天才把 `validator/app_profiles.py` 的 `DEFAULT_APP_VERSION` 改成 1.1.2（同步守卫），不要提前改。

## 判定规则（与 App 对齐）

验证结论以 Readori App 的实际行为为准：**非空规则的结果原样使用**。

- 正文 / 目录 / 详情 / 搜索的兜底与启发式只在对应规则为空时运行；规则写了但没命中，该阶段就失败，不再靠整页文本、链接猜测、JSON 键猜测或书源名救回。
- 请求只发一次，使用书源自带 Header；不会换默认浏览器 Header 重试。空响应时「无 Cookie 新 Session」重试只用于规则为空且开启 cookie jar 的源；`enabledCookieJar=false` 不保存 Set-Cookie；每个书源独立 Session。
- 文字书源正文规则为空判失败（App 会把章节网址当正文显示）；音频 / 图片书源除外。replaceRegex 或 `##` 把正文清空判失败。
- 执行 `ruleSearch.init`（与 App 一致）；无法识别的规则不再按原文返回。
- 网络错误不再吞掉：failureReason 为 `network error: dns / tls / timed out / connection refused / connection reset / too many redirects / http NNN`，`detail.networkError` 含阶段、类别、URL、状态码。
- 最终正文门槛（去空白后 30 字 / 信号 15）比 App 的「非空即可」略严，用来排除「请登录」之类的占位页。
- R149 对齐（联网重验发现「验证器比 App 严」的误判，括号内为 App 依据）：
  - 规则语义：`class.a b c` 与整个 class 属性整串比较（不是多 token 交集，`RuleEvaluator.swift:1005-1010`）；CSS 步骤的 `select` 包含上下文元素自身（`:1034-1041`）；`ul.xbk` 这类「裸 JSONPath 形」规则在 HTML 内容上先按 JSoup 选择器取（取不到才回退 JSONPath，`RuleType.swift:911-925`）；选择器含 `tbody` 而页面没有 `<tbody>` 时按 HTML5 补隐式 `tbody`（只在规则出现 `tbody` 时做，不换整页解析器）；属性值里无分号的 `&section_slot` 之类不再被解码成 `§ion_slot`（Jsoup 属性规则，文本节点不变）。
  - 详情阶段：不再因书名与目录链接都解析不出而失败（App 目录链接回退书籍链接，`BookSourceEngine.swift:9123-9150`，书名未解析只是信息项）；请求 / 脚本异常、空响应、HTTP 错误页仍失败。已删除 App 没有的「登录页标题」详情判失败。
  - 发现：`exploreUrl` 先按换行、再按 `&&` 拆分；不含 `::` 的段是仅标题的分组头，不当作 URL；`{{...}}` 动作不当分类；`@js:` / `<js>` 分类脚本先执行，再取其输出里第一个有链接的分类（脚本报错 = `发现规则为空` 信息项，不判失败）；发现分支不再有 App 没有的「落地页」守卫（搜索分支保留）。
  - 去掉首页探测：快速扫描与完整验证都不再单独请求站点首页（App 没有这一步）；它曾占用 8 秒快速扫描配额，也可能让搜索入口正常的源被判 `connectivity failed`。站点是否可达由搜索 / 发现请求本身判定。
- R150 对齐（括号内为 App 依据）：
  - 规则里的 `{{js}}` 模板：含 `{{…}}` 的规则是「模板」，结果就是字面量与各 `{{…}}` 求值后的整串，不再交给 CSS / XPath / JSONPath 引擎（`RuleType.swift:533-536`、`RuleEvaluator.swift:665-722`）。`{{…}}` 按第一个 `}}` 结束（正则 `\{\{[\w\W]*?\}\}`，`RuleType.swift:413-416`，不是括号配对），内部可含换行、单个 `}`（模板字面量里的 `{"headers": {...} }`）、`java.put(...);"url"` 多语句（取最后一个值）；`{{…}}` 内 JS 的 `result` 是规则输入。`tocUrl` / `bookUrl` / `nextTocUrl` / `nextContentUrl` / `chapterUrl` 求值后的 `url,{method, body, headers}` 选项会带到各自的请求里（`nextTocUrl` / `nextContentUrl` 的后续页此前丢弃选项）。
  - 发现请求：分类 URL 与搜索一样走完整 AnalyzeUrl 模板（`BookSourceEngine+Flagship.swift:149-150`，关键词为空、页码 1），`url,{method, body, headers, charset}` 选项、`{{js}}` / `{{page}}` 都会应用；此前只对未解析的字符串发 GET。
  - 详情错误 JSON：按 App `isLikelyDetailErrorJSON`（`BookSourceEngine.swift:1598-1636`）判定——`data` / `result` / `book` / `novel` / `info` / `item` 任一非空即不是错误；`success: false` 是错误；否则必须「明确失败文案（认证失败 / 未授权 / 未登录 / 请先登录 / token失效 / token过期 / unauthorized / forbidden / not logged in / login required / 参数错误 / 请求失败 / 请求参数错误）」与「不在 0 / 1 / 200 / ok / success 内的错误码」同时出现才算。只有错误码、或只有 `error` / `login` / `token` 之类字样不再判错。JSON 详情响应只要命中就失败（App 在规则执行前抛错），不再要求书名与目录链接同时为空；带内嵌 JSON 的 HTML 页面不算 JSON 响应。已删除无人调用的 `is_likely_detail_login_page` 与 `CONNECT_TIMEOUT`。
- R155 对齐（Legado 实时对照里「Legado 通过、验证器失败」的规则类差距；括号内为 Legado / App 依据）：搜索落地页守卫只在响应地址不同于请求地址时才生效（`BookSourceEngine.swift:246-262`）；`nextTocUrl` 首页返回多条时按批次一次抓取、子页不再求值，只有一条时顺序追链且每页只取第一条（`BookChapterList.kt:77-104`）；`@get:{key}` 在 `@js:` / `<js>` 段、JS 产出的章节 / 正文地址里也展开（`AnalyzeRule.makeUpRule`）；裸选择器末尾 `!N[:M]` 走 `ElementsSingle` 排除语义；`java.getElements` / `getElement` 接受 Legado 规则（`tag.` / `id.` / `class.` / `text.` / `children` / `@@` / `.N`），`:lt(n)` / `:gt(n)` / `:eq(n)` 与 `[attr=原样值]` 按 Jsoup 解析；有 tocUrl 规则时目录地址不再过分类 / 单章启发式（`BookSourceEngine.swift:9120-9150`）；详情页里 name / kind / tocUrl 之外的字段（含 `@put`）也会求值；`let headers` / `let variables` 在书源脚本里合法；`##` 里的 `.` 不匹配 `\r \u0085 \u2028 \u2029`（Java / ICU）；`outerHtml` 属性一律双引号并转义 `&quot;` / `&amp;`；JS 段里的 `src` 是规则内容而不是上一段结果。仅 App 档案 1.1.2 生效：列表规则尾部 `@put:{…}` 先对整份内容执行再按基础规则取列表；单个前导 `.` 的 JSON 路径按 `$..` 深度扫描（1.1.1 保持原行为）。
- R156 对齐（rrs 批次的验证器侧模拟缺口，两个档案都生效）：“全部章节”链接 / 分页下拉的猜测与 App 一致（只在没有 `nextTocUrl` 规则时；有 `chapterList` 规则也照猜；先看分页下拉与 15 章以上的分页列表，都没有候选才跳全部章节页）；跳转页没有任何章节时回到首页（两档案一致），跳转页请求失败时 1.1.2 回到首页、1.1.1 与旧 App 一样目录失败；批量验证的目录阶段像 App 一样只看首个非空页并在收集到 16 章后停止（`BookSourceValidator` 目录阶段 `fetchFirstTocPage(chapterLimit: 16)`，`READORI_VALIDATOR_FULL_TOC=1` 恢复完整遍历，调试引擎与测试默认完整遍历），搜索候选只取第一个可接受结果（`maximumResults: 1`）；`node_binary_path` 缓存；`data:` 载体保留类型选项；`exploreUrl` JSON 里的字面换行按 Gson 宽松解析；`//@href` 之类只含属性的 XPath、`[attr~=regex]`、`@Json:{$.a}|{$.b}`、`@js:code##regex##repl`、`a!1@text`、列表项里以单个 `/` 开头的 XPath、`_html` 容器、JSONP 包装、`{{expr}}` 为空时按空串、宽松的 headers 选项、正文规则非空时任何非空白文本都算通过（近似空正文在 details 里标 `contentNearEmpty`）、`startBrowser` 的 `if/else` 块守卫按去字符串 / 注释后的括号栈判断；去重指纹加入 `enabled`。仅 App 档案 1.1.2 生效（对应 Swift 包 E1，新增 `AppProfile` 开关）：规则非空时不走 `.txt` 单章快捷路径；JSON / XPath 段未命中仍执行后续 JS 段（按 App `segmentYieldsEmptyStringForNextJavaScript` 的分类：JSON / XPath 段总是继续；`@css:`、裸属性与 CSS 形态的段只在列表求值里继续，getString 字段（章节名、更新时间、isVolume / isVip / isPay、作者、简介等）里不继续；正则 / 文本 / 模板段从不继续）；列表项字段按 Legado `BookList.kt` 顺序求值；`java.getElement` 返回列表；章节地址规则为空时用目录页地址；JS 里字符串之外的零宽字符（Cf）被剥掉；搜索 / 探索的 404 / 410 与纯 JS 规则的 4xx 响应体交给规则（401 / 403 / 407 / 408 / 429、全部 5xx 与 Cloudflare 等反爬页仍算失败）；POST body 里的 `<,&page=…>` 分页片段按页展开。`legado_reference/compare_parity.py` 新增两类“Legado 通过、Readori 失败”的有意差异：`legadoInsecureTls`（Readori 因证书校验失败）与 `disabled`（`enabled=false`，Legado 忽略该标志），它们单独列行，并从头条相对通过率的分子分母里剔除。
- R157 对齐（余下的 Legado 通过、验证器失败的规则类缺口）。两个档案都生效（验证器自己的仿真缺口）：`<js>` / `@js:` 先于 `&&` / `||` / `%%` 切分（App `RuleType.parseUncached` 与 Legado `splitSourceRule` 一致），`||` 优先级留在选择器段内；列表规则里的 `sel[a:b]` / `[!a:b]` / `[a,b]` 索引范围；`randomUUID` 返回字符串、`java.put` 存列表时按文本；引擎选择时 `jsLib` 与书源注释里的加密标记也计入 Node 路由；JSON 请求体按结构化体补 `Content-Type`（`NetworkManager.swift` `bodyLooksStructured`）、POST 表单体按 Legado `AnalyzeUrl.encodeParams` 逐字段编码；`unescapeHtml4` 只解码带 `;` 的实体（按 Legado 的 commons-text；App 的 `ContentCleaner.decodeHTMLEntities` 只有一张较小的命名实体表，表外实体 Legado 与验证器会解码、App 不会）；探索列表规则为空时预检回退到搜索规则（`BookSourceEngine+Flagship.swift` `effectiveExploreRule`，Accept 头保持不变）；`data:;base64,…` 的 tocUrl 载体通过合理性检查；`{{$.id}}` 这类纯占位符规则；`//h2/a//@href` 这类 XPath 属性；mowan / 发现链路里搜索作者在详情作者为空时保留（`BookInfo.kt:76-78`）；Node 重放只在可能重放的求值里冻结时钟（`Date()` 返回冻结时刻的字符串）并在拿到有效值后停止；非文本 / 非 UTF-8 响应的原始字节通过 `bodyBase64` 夹具字段传给 `bodyAsBytes`，普通 GET 的 `bodyAsBytes` 单独取字节；HTML 字符串结果带 App 的 `String.prototype.select / text / attr / html / outerHtml / ownText`；章节上下文绑定 `title`（`AnalyzeRule.kt:784`）；`org.jsoup.Jsoup.connect` 两个档案都有。仅 App 档案 1.1.2 生效（对应 Swift 包 E1，新增 `AppProfile` 开关，1.1.1 保持旧 App 行为）：搜索 / 探索项的 `kind`、`lastChapter`、`intro`、`coverUrl` 在 bookUrl 规则求值前可作 `book.*` 使用，详情页的 `lastChapter`、`intro`、`coverUrl` 在 tocUrl 规则前同样绑定（`BookList.kt:220-281`、`BookInfo.kt:86-134`；不写入变量表）；目录项 `@put` / `java.put` 的变量延续到正文阶段（`BookChapter.putVariable`）；JS 章节地址规则返回空白时不再回退到字面模板（`BookChapterList.kt:230-241`，条件与 Swift `ruleContainsJavaScriptSegment` 一致）；列表规则里第一段之后的 JS 段对整份当前列表只运行一次（`result` 是各项组成的数组：JSON 模式下是对象，HTML 项是 outerHTML 字符串，带 `toString` / `first` / `last` / `text` / `attr` / `select`），返回值成为新列表（上一段为空列表时 JS 段照样运行；返回 `undefined` / `null` / `""`、空数组或脚本异常得空列表，不回退到逐项求值或绑定的列表；数组里的 `undefined` / `null` 元素跳过；只有 `{}` / `[]` 容器项会被解析回对象；JSON 模式下 `result.toString()` 是 `[` + 各项 JSON 以 `,` 连接 + `]`、每个对象的 `toString` 是其原始 JSON，HTML 模式下是 outerHTML 以换行连接，并带 `html` / `outerHtml` / `text` / `eachText` / `eq` / `first` / `last` / `attr` / `select` / `hasClass` / `remove`；跟在 JS 段之后的每一段都对整份当前列表求值（JSON 模式对列表的 JSON 数组文本，HTML 模式对各项 outerHTML 的拼接；没有经过 JS 的选择器串联仍逐项；但 JS 段返回的是字符串时，该原始字符串作为下一段的内容，返回数组才是列表——`<js>…ajax(…)</js>$.chapter_list`）；`AnalyzeRule.getElements`；1.1.1 的 App `.combine` 对上一段的每一项各运行一次，`result` 是该项字符串），`java.getElements` 的 JSON 模式判定同 `SourceRule.init`（显式 `@CSS:` / `@@` / `@XPath:` 与 `/` 开头的规则不是 JSON 模式），`parents()` 在没有 Document 根的元素上返回空，章节地址规则含 JS 时整段字面模板展开分支都不执行，`@js:` / `<js>` 列表规则末尾的 `##` 只切掉不替换；`java.getString(规则, 布尔)` 的布尔是 unescape 开关（`AnalyzeRule.kt:256`；1.1.1 的 App 忽略它、总是 unescape，Node 引擎同样遵守）；校验正文样本先对前 16 章按 URL 去重、只留每个 URL 的最后一章（App `keepLastChapterPerURL`、Legado `BookChapterList.kt:119-128`，倒序目录留第一个），再过滤卷、取第一章——在窗口里重复出现的「链接规则为空而取到 baseUrl」占位章因此不再是样本（1.1.1 App 不去重、取 `chapters.first`）；Jsoup 垫片补 `hasClass` / `first` / `last` / `parents` / `contains` 与 `org.jsoup.*` / `Packages.org.jsoup.*` 别名；`for..in` 只遍历列表下标；JSON 模式的 `java.getElements` 返回对象；`@js:` / `<js>` 规则末尾的 `##正则##替换` 作用于脚本结果（`AnalyzeRule.kt:690-703`；切分与 Swift `legadoSplitJavaScriptReplace` 逐字符一致：忽略字符串 / 模板（含 `${}` 嵌套）/ 正则字面量与注释里的 `##`）；卷章节正文为空仍算通过（`CheckSourceService.kt:211-215`、`BookContent.kt:149`）；不再从规则文本静态判定“需要交互”。
- R158 对齐（rrs 批次余下的规则类缺口）。两个档案都生效（验证器自己的仿真缺口，App 已是这样）：JS 以不执行的 `if` / `for` / `switch` / `try` 结尾且 `result` 未变、脚本没抛异常时，取未变的 `result`（Rhino 的 ES5 完成值；App `JSEngine` 对空脚本值回退全局 `result`，抛异常的脚本仍保留 `!= input` 守卫，只写 `{{java.get("k")}}` 这种返回空串的脚本仍得空）；searchUrl 以 `<js>…</js>` 开头、后面跟着 URL 文本时 JS 值被后面的文本取代、`{{key}}` 之后再求值（Legado `AnalyzeUrl.analyzeJs`，App `legadoAnalyzeUrlRule`）；`Array.prototype.toArray` 不可枚举（两个档案；HEAD 的 App 已是 `_readoriDefineHidden`）；jsLib 顶层 `let d` 与规则脚本 `var d` 不再冲突（函数作用域直接 eval，对应 App 的 `hoistTopLevelLetConstToVar`）；又长又几乎全是非 ASCII 字符、没有字面 `java.` 的混淆脚本走 Node（lcread 的 `HMacBase64` 防篡改）；搜索项的 `kind` / `lastChapter` / `intro` / `coverUrl` 经 `__book_*` 保留键与快速扫描种子的 `bookState` 带到详情 / 目录阶段（只有条目运行时带 `book_kind` 的 1.1.2 上才有可见效果，1.1.1 上等于无变化）；搜索 / 探索列表为空且没有 `bookUrlPattern` 时按 `ruleBookInfo` 把响应当详情页（Legado `BookList.getInfoItem`，App `extractSearchBookFromDetailRules`，详情规则里 `@put` 的变量一并带走）；`ruleSearch` / `ruleBookInfo` 的 `init` 是单个 `<js>` / `@js:` 时取脚本返回的整串（App `evaluateToArray` 对字符串结果返回 `[s]`），不再被切成第一行；Jsoup 文本规范化只折叠空格 / `	` / `
` / `` / `` / U+00A0（不含全角空格 U+3000 等，段首的全角缩进保留；`text()` 忽略注释；`:matchesOwn` 用 `ownText()`：直接文本节点无分隔符相连）；`@` 链的第一段 / 中间段永远是 Jsoup `select`（`title@ownText` 取 `<title>`，只有最后一段才可能是结果属性名，`div@title` 是 title 属性，单独一个 `title` 是当前元素的属性；App `RuleType.isSelectorLikeSegment` 只问最后一段，`parseSingle` 把其余规则全交给 AnalyzeByJSoup）；`java.ajax` 的选项接受宽松 JSON（裸键、单引号、尾逗号）；Jsoup `:matches(regex)` / `:matchesOwn(regex)`（SwiftSoup 2.9.6 `QueryParser` 支持，两个档案一致；`:matches` 对 `text()`，`:matchesOwn` 对 `ownText()`，空白折叠、块级元素间补空格、忽略注释）；`java.setContent` 之后无参的 `java.getString` / `getStringList` / `getElements` 读新内容；QuickJS 重放预算 6 秒→15 秒、Node 子进程超时 4 秒→15 秒（都是 App 批量验证的单源总超时 `bulkTimeoutCeiling`，并仍被单源剩余预算截断；做对拍 / 复验时请让校验的单源超时也与 App 一致：书源 ≥100 个时 15 秒、更少时 20 秒，见 `BookSourceValidator.swift` 1123-1133，这里只做说明，不改工作流默认值）。仅 App 档案 1.1.2 生效（对应 Swift 包 E1，新增 `AppProfile` 开关）：`java.getElement("$.x")` 在 JSON 模式返回整个值（`js_get_element_json_whole_value`；`getObject` = `ctx.read(rule)`：数组路径是整个数组，不定路径是全部命中组成的数组，不再取第一行；1.1.1 仍是旧的空元素行）；目录样本在 16 章窗口之后对同一页剩余行只求 chapterUrl 规则并把它们当作“更后面的重复”（`content_sample_dedups_whole_toc_page`；Legado `BookChapterList.kt:114-125`，App `collectWindowOverflowChapterURLs`：chapterUrl 规则含 JS 或内联脚本 `{{java…}}`、没有 chapterList 规则、倒序目录时不扫描，不发请求；最多扫描前 5000 行、3 秒（每 64 行检查一次），超限时保留已收集的集合；跳过冗余 JSON digest 行、含 `option@` / `@put` / `@get` 的 url、不可用的页面 url，名称规则不含 JS 时跳过空标题行，卷行 / 音频行没有 url 时跳过）；整页窗口行都在后面重复时按“没有可用章节”失败；裸 `"RSA"` 公钥解密 PKCS1 失败时改按 NoPadding 并丢弃前导 0（`rsa_bare_public_decrypt_no_padding`；hutool / `JsEncodeUtils.kt:77`）；jsLib 与规则脚本里函数体最外层与形参同名的 `let` / `const` 关键字换成 `var`（等价于赋值，同条声明的其他声明符仍是函数局部变量；`js_param_redeclare_relaxed`）与 `Packages.cn.hutool.core.codec.Base64`（`js_hutool_base64_shim`）；`Packages.io.legado.app.help.http.StrResponse(url 或响应对象, body)` 与 JSON 对象 `result.get(key)`（`js_str_response_shim`、`js_json_object_result_get`）；`java.setContent` 只换隐式内容、不改 `result` / `src`（`set_content_keeps_result_binding`，`AnalyzeRule.kt:84-96`）；JSONPath `$..[?(filter)]` 深度扫描返回所有通过过滤的对象（`jsonpath_deep_scan_filter`）；搜索响应地址与请求只差 URL 片段或百分号编码时不算重定向（`redirect_guard_ignores_url_fragment`）。
- R159 对齐（rrs 与完整包余下的规则类缺口）。仅 App 档案 1.1.2 生效（对应 Swift 包 E1-JS / E1-RULE，新增 `AppProfile` 开关，1.1.1 行为不变）：URL 选项 `{"js": …}` 里的 `java.url` 是正在构造的绝对请求 URL、`java.headerMap` 是可写的请求头表（`put` / `putAll` / `get` / `remove` / `containsKey`，头名不区分大小写、`put` 覆盖原名并返回旧值、`remove` 返回被删的值，与 App `_readoriHeaderMap` 一致；写入进入这次请求的 headers，被 `remove` 的源头也从最终请求里删掉；脚本抛错时不残留中间变量；Legado `AnalyzeUrl.kt:218`、`:246-249`、`:348-360`；ganggou 的 `java.headerMap.put('Cookie', …)`；`js_option_java_url_header_map`）；Java 字符串方法 `"abc".length()` 返回长度，属性 `.length` 不变（与 App `rewritingJavaStringLengthCalls` 同法：代码里的成员调用 `.length()` 改写为 `.__readoriJavaLength()`，字符串 / 注释 / 正则字面量不动、模板字符串 `${}` 里的代码照改，jsLib 与被 `eval` 的 `bookSourceComment` 同样处理；`Object.prototype` 上不可枚举的 `__readoriJavaLength` 先调对象自己的 `length()` 方法（文件代理、jsLib 栈 / 队列类），字符串返回长度，其余照旧抛错，QuickJS 与 node 路径都安装；mysignal；`js_string_length_method`）；`Elements.remove(int)` 按 Java `List.remove(int)` 把该下标的元素移出列表并返回它（越界抛异常，DOM 不动），无参 `remove()` 仍是 DOM 移除，另有 `subList(a, b).clear()`（下标按 App 夹取；`subList` 与这些辅助方法同样只在该开关下存在）（xs8；`js_elements_remove_by_index`）；原始字符串以 `<?xml` 开头（不区分大小写，不去空白 / BOM，Legado `AnalyzeByJSoup.kt:31`）的内容给 CSS / Jsoup 规则用 XML 解析，RSS 的 `<link>url</link>` 保留文本；从 XML 文档取出的列表项仍是纯 outerHtml 字符串（JS / 正则的 `result` 看到的就是它，不注入声明），验证器另行记住这些字符串（每个书源开始时清空，单项超过 256 KB 或超过 4096 项不登记，同 App 的上限），字段规则再解析它们时继续按 XML 处理（Legado `AnalyzeByJSoup.kt:25-38`；asmregg；`xml_prefix_uses_xml_parser`）；XPath 谓词里的 JsoupXpath 字符串运算符 `^=` `$=` `*=` `~=` `!~`（两侧取字符串值，空节点集为空串，所以 yamibo 的 `li[class^=list]` 匹配全部 `li`；`~=` / `!~` 用 EXSLT `re:test`，正则非法时为假而不抛错；只在运算符位置识别 `*=`，`[*='x']` 仍是通配符，`@*='x'` 不产生非法 XPath；`xpath_string_operators`）；目录里原始（绝对化之前）url 以标题开头的卷行不作为正文样本（Legado `isVolume && url.startsWith(title)`；wenxue88；`toc_volume_raw_url_title_prefix_filter`）；`_readoriJsoupElement` 的 `_parent` 不可枚举（沿用 `js_list_helpers_non_enumerable`，否则 98 个元素的列表 JSON 序列化把整张父表重复一遍，超过 2 秒 / 32 MB 而被当成空串）。两个档案都生效（验证器自己的仿真缺口）：正文翻页在遇到下一章 URL 时停止（比较用 App `normalizedBookCandidateURLForComparison`：去 URL 选项、去片段、去末尾 `/`、转小写；规则给出的 `nextContentUrl` 只有恰好一条时才按边界处理；Legado `BookContent.kt:50-55` / `:87-91`，`CheckSourceService.kt:218` 传 `toc[1].url ?: toc.first().url`，App `isNextChapterContentBoundary`；rwsk2 的 `166_1.html` → `166_2.html` 同数字主干，旧的主干启发式看不出），并把 `nextChapterUrl` 绑定为 JS 全局（Legado `AnalyzeRule.kt:786`，App `JSEngine.swift:2826`）；单次请求超时 `REQUEST_TIMEOUT` 保持 20 秒；发现阶段失败时只记录发现开始之后的传输层网络错误（tls / 超时 / dns / 连接被拒 / 连接重置 / 连接失败 / 重定向过多，不含 `http 404` 这类探测，ndlib 的发现 GET 先 404 再 POST），kanman 的 TLS 过期证书因此归为 `network error: tls`（对拍里是 legadoInsecureTls），失效分组仍是「发现失效」；报告条目带 `bookSourceType`（解析对非数字 / 异常取值安全地当作 0；freexiaoshuo 这类 file 源在对拍里始终归 unsupportedSourceType）；搜索 / 发现返回零结果且 HTML 页面同时是拦截 / 错误页（挑战标记、http 错误页信号或 404 类标记）和含反爬 / 限频关键词（`系统限制的搜索`、`搜索时间间隔`、`请求过于频繁`、`请稍后再试` 等，词表与 App `containsSearchBlockingPageSignals` / `containsAntiBotOrRateLimitSignals` 相同，无额外词、无页面大小限制；`BookSourceEngine.swift:867-876`）时失败原因为 `network error: rate limited`，不重试，状态按关键词重置；已按浏览器挑战归类的 http 错误页仍走挑战路径；`java.t2s` / `java.s2t` 一次正则替换代替逐字符拼接（czbooks 262 KB 页面 4 秒 → 0.06 秒，超过 QuickJS 的 2 秒限制时脚本被中断、结果被当成空串），输出与旧的逐字符映射逐字节相同；xs8 目录：`[attr~=regex]` 的取值按括号深度、反斜杠和引号扫描（`a[href~=chapter[/_-]?list]`），soupsieve 不支持的复杂 `:has(div + div)` 参数包进 `:is()`。

## 生成独立 EXE

在联网的 Windows 电脑上运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1
```

脚本会创建本目录 `.venv`、安装依赖和 PyInstaller，生成：

- `dist/ReadoriSourceValidator.exe`：图形界面；
- `dist/ReadoriSourceValidatorCLI.exe`：命令行版本。

两个 EXE 需要放在同一目录。双击 `ReadoriSourceValidator.exe` 不会显示 CMD 窗口。首次构建需要下载 Python 包；没有网络时可先使用已有虚拟环境运行源码版。

## GitHub Actions 自动构建与 Releases

仓库工作流 `.github/workflows/build-windows-source-validator.yml` 仅支持 Actions 页面手动运行，不会因分支推送、Pull Request 或标签推送自动执行；每次运行都会编译 Windows x64 GUI/CLI EXE。勾选 `publish_release` 后，ZIP 和 `SHA256SUMS.txt` 会上传到 GitHub Release；Release 资产不会按 30 天自动过期，只有手动删除 Release 或资产后才会消失。单独的 Actions 构建附件仍受仓库的 Artifact 保留策略管理。

发布示例：

```powershell
git tag source-validator-v1.0.0
git push origin source-validator-v1.0.0
```

然后在 GitHub Actions 中选择 `Build Windows Source Validator` 并运行。`publish_release` 默认开启：选择已有的 `source-validator-v*` 标签会更新该 Release；从分支运行且不填写 `release_tag` 时，会自动生成 `source-validator-v0.0.<运行编号>` 并发布到当前提交。填写 `release_tag` 可指定一个已有或待创建的 `source-validator-v*` 标签；仅需要构建附件时才取消勾选 `publish_release`。

Release ZIP 内含 `ReadoriSourceValidator.exe`、`ReadoriSourceValidatorCLI.exe`、README 和运行依赖清单；不包含书源数据、Cookie 或其他用户文件。

## 服务器验证服务

服务器版位于 `server/`，网页只负责提交任务和读取进度，实际请求与 Legado 规则解析在后台 worker 执行。它使用 SQLite WAL 保存去重结果、四阶段检查点、事件日志和可恢复任务，并提供每域名并发限制、瞬时网络失败重试及设备兼容性门槛；Cloudflare 控制台要求填写访问令牌（见下文“安全与部署”），服务器 API key 只保存在 Worker secret 里：

```powershell
python -m pip install -r .\server\requirements.txt
$env:READORI_VALIDATOR_API_KEY = "change-this-key"
python -m server.source_validator_server --host 0.0.0.0 --port 8787
```

接口顺序为 `POST /v1/jobs` 或 `/v1/jobs/upload`、轮询 `/v1/jobs/{id}` 与 `/events`、按需 `/cancel`/`/resume`，最后从 `/result` 下载仅通过搜索→详情→目录→正文及设备门槛的书源。配置项、反向代理/TLS、部署隔离和抽样真机复测要求见 [`server/README.md`](server/README.md)。

### Cloudflare Pages/Worker + AMD Micro server-only

`cloudflare/` 提供静态控制台和到 AMD Micro FastAPI 的 HTTPS 反向代理。Worker 不再绑定或调用 D1、R2、Queue；任务状态、输入、结果和检查点全部保存在 AMD Micro 的 SQLite/文件系统。AMD Micro 直接运行 `python -m server.source_validator_server`，画像固定 `workers=1`、每域名并发 1、最多两轮稳定性复测，避免 1GB 内存被浏览器参数压垮。部署、服务器 token、TLS、systemd 和公网限流见 [`cloudflare/README.md`](cloudflare/README.md)。

专用部署仓库 `readori/test-env-setup` 的 `deploy-readori-source-validator-cloudflare.yml` 仅手动触发，并从 `readori/readori-CheckSources` 拉取源代码后执行 Cloudflare dry-run/部署；服务器端可直接运行 [`server/install_amd_micro.sh`](server/install_amd_micro.sh) 完成 AMD Micro 的一键安装配置。

## 参数建议

- 最快首轮筛选：`--quick-timeout 8 --rounds 1 --min-pass-rounds 1 --source-timeout 30`。
- 稳定复测：只对快速扫描和首次完整验证均通过的源运行 `--rounds 3 --min-pass-rounds 2`；后续复测会重新执行搜索、详情、目录、正文。每轮只复测上一轮通过的源，所以 `--min-pass-rounds` 小于 `--rounds` 时的含义是“连续通过前 N 轮即可交付，之后的复测失败不再扣除”；中途失败的源不会再复测，也不会因为之后“恢复”而被计入。
- 搜索关键词与 App 一致（R147）：只搜一个词，`ruleSearch.checkKeyWord` 非空白时原样使用，否则用 App 默认词“我的”；不会再轮换其他关键词。搜索通过后，只要 `exploreUrl` 有分类链接，发现页也会按 App 的 `发现失效` 规则检查（发现页没有书，整个书源判失败，失败原因 `explore failed`）；没有搜索、只有发现的源由发现页单独判定；分类列表里没有任何链接只是 App 的信息标签 `发现规则为空`，不算失败。
- 发现候选的深度校验（R151，对齐 App `validateCandidate`）：搜索和发现都给出书时，发现页第一本书也要走详情 → 首个目录页 → 首章正文（与搜索候选同一套逻辑），任一失败整个书源判失败：详情失败 `explore detail failed`（脚本错误 `js失效`，其余 `网站失效`）、目录为空 `explore toc failed`（`发现目录失效`）、正文为空 `explore content failed`（`发现正文失效`）；明细 `stages` 仍描述搜索候选，发现候选结果在 `exploreCandidate`（`stages` / `failureReason` / `detailBookName` / `firstChapterTitle` / `contentPreviewChars`）。发现书与搜索书是同一本（URL 部分去空白、不分大小写、忽略 `,{options}`）时复用搜索结果，`exploreCandidate.reused` 为 true，不再多发请求。每个同时有搜索和发现的书源因此多约 3 个请求。
- 发现链接构建完仍是 `@js:` / `<js>…</js>` 时（App `explore()` 的 `isJsExplore`）本地执行脚本、不发网络请求，脚本输出当作发现页内容；脚本抛错判 `js失效`。发现页内容是 JSONP（`callback({…})`）时先解包再解析书单。
- 同站同书的多个通过变体聚合时，优先保留各阶段都真实通过、首章正文更完整、目录章节更多的变体（再按原有评分与响应时间）。
- 无进度超时（idle timeout）后仍在运行的线程不会被强杀；它们计入后续阶段的并发预算（下一阶段会先等待一小段时间，再按剩余名额启动 worker），报告 `summary.abandonedWorkers` / `abandonedWorkersStillRunning` 记录被放弃的 worker 数。
- 判定路径上被吞掉的异常（请求重试、规则求值、loginCheckJs 结果、CSS / XPath / JSONPath 解析等）会写入明细 `debugErrors`（最多 20 条，每条截断），只用于诊断，不改变判定。
- `--pipeline legacy` 可临时回退到旧版完整轮次调度，便于结果对比（只能配合单个 App 版本，不能配合 `both`）。
- `--app-version` 默认 1.1.1（当前公开版）；准备发布 1.1.2 前用 `both` 看哪些源两版表现不同。
- `--workers` 不宜盲目增大；建议先使用 8–20，并观察目标站点是否限流。
- `--source-timeout 0` 表示不限制，不建议对大批源使用。

报告中的结果应区分：通过、规则/网络失败、超时、需要交互、WebView/付费内容和环境错误。验证码、登录、Cloudflare、付费章节等需要人工操作的来源不会被工具伪报为通过。

## 依赖和安全边界

运行依赖见 `requirements-validate-sources.txt`。QuickJS、Node.js、py7zr、rarfile 和 cryptography 用于增强兼容性；缺失时对应能力会降级或标记失败。RAR 解包通常还需要系统安装 7-Zip/UnRAR。

书源内嵌 JavaScript 属于不可信输入。服务器部署时应放在隔离容器、限制 CPU/内存/网络和文件访问，禁止把 Cookie、API Key 等秘密写入报告或日志。

### 安全与部署（R145a）

- Cloudflare Worker 对除 `/api/healthz` 以外的所有 `/api/*` 接口要求 `Authorization: Bearer <READORI_VALIDATOR_ACCESS_TOKEN>`；只有鉴权通过才会给转发请求注入服务器 token。Worker 未配置该 secret 时返回 503（fail closed）。上线前必须先 `wrangler secret put READORI_VALIDATOR_ACCESS_TOKEN`，并在 GitHub 的 production 环境配置同名 secret（手动 workflow 才会部署，见 `cloudflare/README.md`）。
- 服务器 `input_path` 永远限定在 `READORI_VALIDATOR_INPUT_ROOT`；未设置时为服务数据目录（SQLite 所在目录）下的 `inputs/` 子目录，`..` 与符号链接都会按真实位置判定。
- 服务器拒绝首个请求地址落在私网 / 回环 / 链路本地 / 云元数据地址的书源（解析 DNS 后检查所有地址）；本地开发需要访问 127.0.0.1 夹具时设置 `READORI_VALIDATOR_ALLOW_PRIVATE_NETWORK=1`。`validator/net_guard.py` 是可复用的纯函数模块（`is_public_address` / `check_url_allowed`）。验证引擎自己发出的请求见下一小节（R145b）。
- 设备兼容性门槛 `detail_is_device_compatible` 只在服务器的 `device` profile 下生效；命令行与 GUI 没有这道门槛，三条路径对同一书源的结果可能不同。
- 书源 JS 在 Node `vm` 中执行；R145b 起外层加了 Node 权限模型等多道防线（见下一小节），但 `vm` 本身仍不是安全边界，服务器部署仍应叠加隔离（低权限用户、systemd 限制、无内网出口）。

### 引擎网络过滤与 Node 沙箱（R145b）

- 引擎内所有出站请求（`fetch_text` / `fetch_bytes`、JS `java.ajax` / `connect` / `get` / `post` / `webView` 等经 Python 桥发出的请求、JS 请求日志重放）都经过 `net_guard.GuardedSession`：请求发出前检查目标，**重定向的每一跳**都重新检查（requests 每一跳都会重新进入适配器），被拒绝时不发请求，`failureReason` 为 `network error: blocked: non-public address`，`detail.networkError.category` 为 `blocked: non-public address`。连接直接拨向「检查时解析出的、已验证为公网」的地址（Host 头 / TLS 主机名不变），因此检查与连接之间没有再次解析的窗口（DNS 重绑定）；经 HTTP 代理访问时由代理解析，只能检查解析结果。
- 策略：命令行 / GUI 默认**允许**私网（本机运行时书源可能在局域网，App 同样能访问）；`--block-private-network` 禁止。服务器每个任务都在 `network_policy_scope(False)` 内运行，进程默认也设为禁止，只有 `READORI_VALIDATOR_ALLOW_PRIVATE_NETWORK=1` 才放开。代码里用 `validator.set_network_policy(bool)`（进程默认）或 `network_policy_scope(bool)`（上下文；新线程需 `copy_context().run` 绑定）。
- Node 子进程：环境变量只保留 `PATH`、`SYSTEMROOT` 等启动必需项（不继承服务器 token / API key，`NODE_OPTIONS` 也不继承）；Node >= 23.5（本机 v24 已验证）加 `--permission` 且不授予 fs 写 / `child_process` / worker / addon，fs 读只开放运行器脚本所在的私有临时目录；旧版本 Node 不支持时降级并向 stderr 警告一次，仅剩环境变量最小化与运行器内的加固。
- 运行器在执行书源代码前去掉宿主域的逃逸入口：宿主 `Function.prototype.constructor`（`constructor.constructor('return process')()`）、`process`、`require`、`console`、`fetch`、`WebSocket`、`Error.prepareStackTrace` 等。Node 权限模型不限制网络，所以这一步同时堵住逃逸后直连内网。
- 残余风险：`vm` 与宿主共用同一进程与 V8 堆，未知的 V8 / Node 逃逸仍可能执行任意 JS（但无 fs 写 / 子进程 / 环境变量，网络入口已移除）；Node 子进程仍可使用本机 CPU / 内存（每次有超时）；`--block-private-network` 之外的 DNS 解析发生在本机解析器，需要出口防火墙作为最后一道防线。

## 书源工作台（R146）

单个书源的编辑、单步调试、修复建议和修复前后对比。批量验证的参数和行为不变；工作台有三个入口，共用同一套引擎（`validator/debug_engine.py` 等），调试走的链路与批量验证一致，只是开着跟踪。

### 命令行子命令

第一个参数是子命令时进入工作台；否则仍是批量验证（批量参数都以 `--` 开头）。`<file>` 是书源 JSON / JSON5；文件里有多条书源时用 `--source URL|名称`（先精确 URL → 规范化 URL → 精确名称 → 名称子串，歧义会列出候选并退出 2）或 `--index N`（从 0 开始）。`--app-version 1.1.1|1.1.2`（默认 1.1.1）；`--jsonl` 每行输出一个 JSON 事件；`--json` 结束时输出摘要 JSON（单独使用时日志改走 stderr）；`--block-private-network` 拒绝私网地址（默认允许，方便调试本机站点）。

| 子命令 | 用途 | 示例 |
|---|---|---|
| `debug` | Legado / App 风格单步调试；`--key` 缺省 = 完整校验，`关键词` / `标题::发现URL` / `详情URL` / `++目录URL` / `--正文URL`；`--step`（终端里在选书 / 选章处暂停）、`--pick-book N`、`--pick-chapter N`、`--timeout 60`、`--save-raw DIR` | `source_validator_cli.py debug sources.json --source 笔趣阁 --key "++https://x/book/1/"` |
| `test-rule` | 在保存的响应上重放一条规则：`--field ruleSearch.name`、`--rule R`（缺省用书源里的）、`--body-file F`（`-` = stdin）或 `--raw DIR [--raw-id N]`（`debug --save-raw` 的目录）、`--base-url`、`--item N`、`--online`（允许规则内 JS 联网，默认离线） | `source_validator_cli.py test-rule sources.json --field ruleToc.chapterList --raw raw/` |
| `fix` | 调试 → 建议 → 预览 → 应用 → 导出：`--apply F5,F7` / `--apply-safe`（全部离线且预览通过的）、`--online-preview`、`--revalidate`、`--keep-source-url`、`--out`、`--report`、`--dry-run`（不写任何文件） | `source_validator_cli.py fix sources.json --source 笔趣阁 --apply-safe --revalidate --out fixed.json --report fix-report.json` |
| `validate-one` | 按批量流水线（快速扫描 → 完整验证 → 稳定复测）校验一个书源：`--quick-timeout 8 --source-timeout 60 --rounds 1`、`--trace`（打印事件）、`--app-version both`（两版各跑一次并给出兼容结论） | `source_validator_cli.py validate-one sources.json --index 3 --trace` |
| `export` | 导出书源；`--from-report fix-report.json` 把报告里的改动应用到对应书源（按 `originalBookSourceUrl` 匹配），`--only-changed`、`--only-passed`、`--keep-source-url`、`--out` | `source_validator_cli.py export sources.json --from-report fix-report.json --only-changed --out fixed.json` |
| `rpc` | stdin / stdout JSON-lines 服务，仅供桌面工作台使用 | |

退出码：0 通过 / 规则有结果；1 书源失效或规则结果为空；2 用法或输入错误；3 需要登录 / 验证；4 超时；130 Ctrl+C。

- `fix` 只导出被选中的那一条书源（`<输入>.fixed.json`，或 `--out`）；修复报告 `fix-report.json`（`readori.fix-report/1`）记录每处改动的路径、旧值、新值、原因和修复前后结论。修复过的书源会写入 `// Fixed: <洛杉矶时间> <建议代码>: <原因>` 注释和 `lastUpdateTime`；不会写验证标签。
- 域名迁移（F1）默认连 `bookSourceUrl` 一起改（它是书源在 App 里的主键，导入后会成为新书源、旧源仍保留；报告里的 `originalBookSourceUrl` 记着旧值）。不想改主键就加 `--keep-source-url`。
- `--apply` 指定的建议只有在预览通过（或联网类建议未被预览否决）时才会应用；被拒绝的会列出原因。
- 日志和时间一律使用洛杉矶时区（夏令时由系统时区库处理）。

### 桌面工作台

批量验证器窗口的「书源工作台…」按钮会打开工作台窗口（单例；批量验证运行时也能用）。左：书源列表（名称 / 上次结论 / 耗时，可过滤，导入 / 导出已修改）；中：按 Legado 编辑器分组的字段（基本 / 搜索 / 发现 / 详情 / 目录 / 正文 / JSON），每个规则字段有提示文案和「▶」单字段测试，工具条有撤销 / 重做 / 还原；右：日志、原始响应、规则测试、建议（预览 / 应用）、差异、对比、帮助。顶栏是调试 key 输入和芯片（校验关键字 / 系统 / 发现 / 详情 / ++目录 / --正文）、调试 / 单步 / 停止、校验此源、修复前后对比、App 版本。

工作台自己不加载校验器：它启动一个常驻子进程 `ReadoriSourceValidatorCLI.exe rpc`（开发时是 `python -u source_validator_cli.py rpc`），UI 线程只写请求，后台线程读响应并经 `after()` 回到 UI 线程。「停止」先发取消请求，3 秒内没有结束就强制结束并重启子进程（已编辑的书源会自动恢复）。`python workbench_gui.py --self-test` 可在没有窗口的情况下检查控件树。

## Legado 实时对照（R153）

目标：量化「相对 Legado 的书源可用率」。对同一份书源包，在同一时段联网运行 **真正的 Legado 引擎** 与本目录的 Python 验证器（App 语义，`--app-version 1.1.1`），输出对照报告：

```text
relativeRate = |Legado 通过（非空转）且 Readori 通过| / (|Legado 通过（非空转）| - |有意差异类|)        （目标 >= 95%；有意差异类 = `legadoInsecureTls` + `disabled` + `unsupportedSourceType`，见下）
```

目录 `legado_reference/`（随本目录一并镜像到 `readori-CheckSources`）：

| 文件 | 作用 |
|---|---|
| `kotlin/io/legado/app/readori/LegadoLiveSourceCheckTest.kt` | Robolectric JVM 测试：逐行移植 Legado `CheckSourceService.checkSource / doCheckSource / checkBook`（搜索 → 发现 → 详情 → 目录 → 正文，全部开关打开，关键字 `source.getCheckKeyword(CheckSource.keyword)`，默认超时 `CheckSource.timeout` = 180 s）；引擎调用（`WebBook.*Await`、书源分组辅助方法、`exploreKinds`）全部是未修改的 Legado 代码。成功判据与 Legado 相同：没有异常，且没有任何「xx失效 / 校验超时」分组 |
| `legado-live.init.gradle` | Gradle init 脚本：把 Robolectric 和上面的 Kotlin 目录加进 Legado `:app` 的单元测试源集，传系统属性，测试 JVM 堆 4g，关闭测试缓存，输出 stdout；**不修改 Legado 检出目录** |
| `run_legado_live_reference.py` | 包装脚本（Linux / Windows）：只在 `local.properties` 不存在时用 `ANDROID_HOME` / `ANDROID_SDK_ROOT` 写 `sdk.dir`（已存在则不读不改），运行 `gradlew --no-daemon -I legado-live.init.gradle :app:testAppReleaseUnitTest --tests io.legado.app.readori.LegadoLiveSourceCheckTest`，校验报告 JSON；`--dry-run` 只打印命令 |
| `prepare_sources.py` | 规范化书源包（数组 / `sources`、`bookSources`、`list`、`data` 包装 / 单对象；去 BOM；按 `bookSourceUrl` 去重；`--offset` / `--limit` 只在这里切一次），保证两个引擎看到完全相同的书源 |
| `compare_parity.py` | 读 Legado 报告和验证器报告，输出 `parity.json` / `parity.md`；`--sources`（缺省取 Legado 报告同目录的 `sources.json`）用验证器自己的 `group_sources` 还原分组，每个书源继承所在组代表的 Readori 结论 |
| `samples/smoke.json` | 8 个书源的冒烟样本（取自 2026-10-18 重验的通过集合，不含需登录 / 成人站点） |

### 本机运行（需要 Android SDK + JDK 17）

```bash
python legado_reference/prepare_sources.py --input sources.json --output work/sources.json
python legado_reference/run_legado_live_reference.py --legado-root /path/to/legado \
    --sources work/sources.json --output work/legado_report.json --concurrency 8
python validator/validate_source_packages.py --input work/sources.json --app-version 1.1.1 \
    --pipeline staged --report-path work/report.json --report-only --no-mirror
python legado_reference/compare_parity.py --legado work/legado_report.json \
    --readori work/report.json --output-dir work
```

要点：Legado 检出需是 `readori/legado`（`io.legado.app` 包，AGP 8.13 / Kotlin 2.3 / JDK 17）；Gradle 首次要下载依赖和 Robolectric 的 android-all jar；`--timeout N` 改每源超时秒数，`--build-root DIR` 把构建输出放到检出目录之外，`--variant AppDebug` 改测试变体（默认 `AppRelease`，避免 debug 日志把整页响应写进 Robolectric 的内存日志），`--no-android-resources` 是应用启动异常时的备用开关。包装脚本退出码：0 报告已生成；1 Gradle 失败（可能留有部分报告）；2 用法 / 环境错误；3 报告缺失。Legado 报告每 25 个源增量写一次（`partial: true`），整个任务超时也会保留已完成部分。

### GitHub Actions

工作流 `Readori Legado Reference`（文件 `docs/公开仓专用ci/readori-legado-reference.yml`，放进公开仓 `readori/test-env-setup` 的 `.github/workflows/`），仅手动触发，不需要任何 secret：检出 `readori/legado`（`legado_ref`）与 `readori/readori-CheckSources`（`main`），JDK 17（Temurin），ubuntu-latest 预装的 Android SDK，`prepare_sources.py` 规范化（`sources_url` 为 https 下载，否则用镜像仓内的 `sources_path`，默认冒烟样本），Legado 实时运行与验证器并行，`compare_parity.py` 出报告并追加到 `$GITHUB_STEP_SUMMARY`；`legado_report.json`、`report.json`、`parity.json`、`parity.md`、日志与 Gradle 测试报告作为 artifact 上传（`if: always()` 语义，14 天）。输入只经 `env:` 传入 shell 并先校验。

### 如何读 `parity.md`

- 顶部 **Relative rate (non-vacuous)** 是头条数字，目标 >= 95%，分子分母都不含「空转通过」，分母另外减去有意差异类（`legadoInsecureTls`：Readori 因证书校验失败；`disabled`：`enabled=false`，Legado 忽略该标志；`unsupportedSourceType`：`file` 类型书源（`bookSourceType=3`，验证器标「download-only source」）和正文规则为空的书源（标「content rule empty」），暂不支持；三者单独成行，`parity.json` 的 `counts.legadoOnlyExcludedClasses` 分别计数）：没有搜索地址且没有发现地址时，Legado 的 `doCheckSource` 什么都不检查就算通过，这类源不能当作 Legado 可用的证据。含空转的旧口径保留在表格 `Relative rate including vacuous Legado passes` 一行（`parity.json` 的 `relativeRateInclVacuous`）；`Relative rate excluding Readori timeout / network / missing` 是在头条口径上再排除站点抖动。
- 分组映射：验证器按「规范站点 + 规则指纹」去重，每组只报告一个代表（可能是和 Legado 所用不同的 URL 写法，例如 `#🎃` / `#♤yc` / 末尾 `/` / `http` 与 `https` 的差别）。`--sources` 给出准备好的书源包后，每个书源都映射到恰好一个 Readori 结论（`counts.readoriMapping`：`exact` / `group` / `relaxed` / `skipped` / `missing`）；验证器没有出报告项的组记为 `skipped:<原因>`（`validator-limit` = 验证器带了 `--limit`，`not-validated` = 报告里没有该组），不再计入 `missing`，也不算站点抖动。没有书源包时退回 URL / 宽松 URL 匹配，`parity.md` 顶部会有 WARNING。
- **Legado pass, Readori fail** 是工作清单：`rule` / `needsInteraction` 是引擎差距，优先处理；`network` / `timeout` / `missing` 多半是站点抖动，先重跑确认。`Legado stages` 形如 `S:ok E:- I:ok T:ok C:ok`（搜索 / 发现 / 详情 / 目录 / 正文；`-` = 未运行，`x` = 失败）。
- **Readori pass, Legado fail**：Legado 一侧的 `failureGroup`（`js失效` / `网站失效` / `校验超时`）和 `harnessSuspect`（`webview`、`android-resource`、`jvm-linkage`、`robolectric`）说明失败是书源本身还是 JVM 测试环境限制；声明了 WebView 的源在 Robolectric 里无法真正加载页面，会按超时算 Legado 失败。
- 报告里的书源名 / 错误文本来自不可信数据，已转义和截断，仅供阅读。

## 与 iOS 验证器的关系

本目录中的 Python 核心是服务器/Windows 侧验证实现；iOS 的 Swift 验证器仍负责 App 内的运行时语义和 UI。两者应通过固定 JSON/响应夹具做差异回归，不应直接复制 Swift 文件到 Windows。

## 当前限制

- 纯浏览器页面不能可靠地直接执行完整验证；浏览器应作为服务器任务控制台。
- 必须登录、验证码、WebView 或付费内容的来源只能得到“需要交互”状态。
- 本发行目录提供完整源码和构建脚本；实际在线验证结果取决于运行电脑的网络、DNS、TLS、IP 地域和目标站点状态。
